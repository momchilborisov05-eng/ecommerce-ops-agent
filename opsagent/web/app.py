"""
Dashboard and JSON API (FastAPI).

    GET  /api/overview                    KPIs, per-workflow stats, recent runs
    GET  /api/workflows                   workflow definitions and their tool policies
    POST /api/workflows/{name}/run        run a workflow now
    GET  /api/runs                        run list (filter by workflow / status)
    GET  /api/runs/{id}                   one run with its step-by-step trace
    GET  /api/approvals                   approval queue (default: pending)
    POST /api/approvals/{id}/approve      execute the action / resolve the review
    POST /api/approvals/{id}/reject
    GET  /api/integrations                integration call log (Slack, ClickUp, Gmail)
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..approvals import ApprovalError
from ..guardrails import redact
from ..llm import LLMError
from ..runtime import Runtime
from ..workflows import WORKFLOWS

STATIC = Path(__file__).with_name("static")


class Decision(BaseModel):
    note: str = Field(default="", max_length=500)
    decided_by: str = Field(default="dashboard", max_length=80)


class RunRequest(BaseModel):
    limit: int | None = Field(default=None, ge=1, le=200)


def _decode(row: dict, *fields: str) -> dict:
    for f in fields:
        raw = row.pop(f"{f}_json", None)
        row[f] = json.loads(raw) if raw else None
    return row


def create_app(rt: Runtime) -> FastAPI:
    app = FastAPI(title="E-commerce Ops Agent", version="1.0.0")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    db = rt.db

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "mode": rt.settings.mode, "seeded": rt.is_seeded()}

    @app.get("/api/overview")
    def overview() -> dict:
        totals = db.query_one(
            """SELECT COUNT(*) AS runs,
                      SUM(status = 'completed') AS completed,
                      SUM(status IN ('failed', 'stopped', 'incomplete')) AS failed,
                      COALESCE(SUM(tool_calls), 0) AS tool_calls, COALESCE(SUM(llm_calls), 0) AS llm_calls,
                      COALESCE(SUM(input_tokens + cache_read_tokens + cache_write_tokens), 0) AS input_tokens,
                      COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(cost_usd), 0) AS cost_usd, COALESCE(AVG(duration_ms), 0) AS avg_ms,
                      COALESCE(MAX(usage_estimated), 0) AS estimated
               FROM runs"""
        )
        by_workflow = db.query(
            """SELECT workflow, COUNT(*) AS runs, SUM(status = 'completed') AS completed,
                      SUM(tool_calls) AS tool_calls, ROUND(SUM(cost_usd), 4) AS cost_usd,
                      ROUND(AVG(duration_ms)) AS avg_ms, MAX(started_at) AS last_run
               FROM runs GROUP BY workflow ORDER BY workflow"""
        )
        approvals = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) AS n FROM approvals GROUP BY status")}
        pending_by_action = db.query(
            "SELECT action, COUNT(*) AS n FROM approvals WHERE status = 'pending' GROUP BY action ORDER BY n DESC")
        integrations = db.query(
            """SELECT integration, backend, COUNT(*) AS calls, SUM(status = 'ok') AS ok, SUM(attempts - 1) AS retries
               FROM integration_calls GROUP BY integration, backend ORDER BY integration""")
        triage = db.query("SELECT category, COUNT(*) AS n FROM triage GROUP BY category ORDER BY n DESC")
        tools = db.query(
            """SELECT name, COUNT(*) AS n, SUM(status = 'duplicate') AS duplicates, SUM(status IN ('error','blocked')) AS errors
               FROM steps WHERE kind IN ('tool', 'guardrail') GROUP BY name ORDER BY n DESC""")
        recent = db.query("SELECT * FROM runs ORDER BY id DESC LIMIT 8")
        return {
            "mode": rt.settings.mode, "model": rt.settings.model if rt.settings.mode == "live" else "scripted-demo",
            "backends": rt.adapters.backends(), "store_date": rt.settings.store_date.isoformat(),
            "totals": totals, "by_workflow": by_workflow, "approvals": approvals,
            "pending_by_action": pending_by_action, "integrations": integrations, "triage": triage,
            "tools": tools, "recent_runs": recent,
        }

    @app.get("/api/workflows")
    def workflows() -> list[dict]:
        return [{
            "name": w.name, "title": w.title, "description": w.description, "trigger": w.trigger,
            "allowed_tools": sorted(w.policy.allowed_tools), "max_steps": w.policy.max_steps,
            "max_tool_calls": w.policy.max_tool_calls,
        } for w in WORKFLOWS.values()]

    @app.post("/api/workflows/{name}/run")
    def run_workflow(name: str, body: RunRequest | None = None) -> dict:
        if name not in WORKFLOWS:
            raise HTTPException(404, f"Unknown workflow {name!r}.")
        try:
            results = rt.run_workflow(name, limit=body.limit if body else None)
        except (RuntimeError, LLMError) as e:
            raise HTTPException(409, str(e)) from e
        return {"workflow": name, "runs": [
            {"run_id": r.run_id, "status": r.status, "final_text": r.final_text, "error": r.error,
             "tool_calls": r.tool_calls, "cost_usd": r.cost_usd} for r in results]}

    @app.get("/api/runs")
    def runs(workflow: str | None = None, status: str | None = None,
             limit: int = Query(100, ge=1, le=500)) -> list[dict]:
        sql, params = "SELECT * FROM runs WHERE 1 = 1", []
        if workflow:
            sql += " AND workflow = ?"
            params.append(workflow)
        if status:
            sql += " AND status = ?"
            params.append(status)
        return db.query(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: int) -> dict:
        run = db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not run:
            raise HTTPException(404, f"Run {run_id} not found.")
        run["steps"] = [_decode(s, "input", "output")
                        for s in db.query("SELECT * FROM steps WHERE run_id = ? ORDER BY seq", (run_id,))]
        run["approvals"] = [redact(a) for a in rt.approvals.list() if a["run_id"] == run_id]
        run["integration_calls"] = [_decode(c, "request", "response") for c in db.query(
            "SELECT * FROM integration_calls WHERE run_id = ? ORDER BY id", (run_id,))]
        return run

    @app.get("/api/approvals")
    def approvals(status: str = Query("pending", pattern="^(pending|executed|rejected|resolved|failed|all)$")) -> list[dict]:
        items = rt.approvals.list(None if status == "all" else status)
        return [redact(a) for a in items]

    def _decide(approval_id: int, approve: bool, body: Decision | None) -> dict:
        body = body or Decision()
        try:
            fn = rt.approvals.approve if approve else rt.approvals.reject
            return redact(fn(approval_id, body.decided_by, body.note))
        except ApprovalError as e:
            code = 404 if "does not exist" in str(e) else 409
            raise HTTPException(code, str(e)) from e

    @app.post("/api/approvals/{approval_id}/approve")
    def approve(approval_id: int, body: Decision | None = None) -> dict:
        return _decide(approval_id, True, body)

    @app.post("/api/approvals/{approval_id}/reject")
    def reject(approval_id: int, body: Decision | None = None) -> dict:
        return _decide(approval_id, False, body)

    @app.get("/api/integrations")
    def integrations(integration: str | None = None, limit: int = Query(200, ge=1, le=1000)) -> list[dict]:
        sql, params = "SELECT * FROM integration_calls", []
        if integration:
            sql += " WHERE integration = ?"
            params.append(integration)
        rows = db.query(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))
        return [_decode(r, "request", "response") for r in rows]

    return app
