"""
Human-in-the-loop approval queue.

Risky actions (sending an email to a customer, issuing a refund) are never
executed by the agent directly. The tool puts a request in this queue and the
agent is told it is pending. A person approves or rejects it in the dashboard;
only an approval runs the action, through an executor registered per action.

"review" items (from flag_for_human) have no executor: approving one marks it resolved.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from .audit import RunRecorder, current_run_id
from .guardrails import redact
from .storage.db import Database, to_json, utcnow

Executor = Callable[[dict], dict]


class ApprovalError(RuntimeError):
    """The approval cannot be decided, e.g. it was already decided."""


class ApprovalQueue:
    def __init__(self, db: Database, recorder: RunRecorder | None = None):
        self.db = db
        self.recorder = recorder
        self._executors: dict[str, Executor] = {}

    def register_executor(self, action: str, executor: Executor) -> None:
        self._executors[action] = executor

    def request(
        self,
        *,
        run_id: int | None,
        kind: str,
        action: str,
        title: str,
        reason: str,
        payload: dict,
        idempotency_key: str,
    ) -> dict:
        """Queue an action. The same idempotency key returns the existing request instead of a new one."""
        existing = self.db.query_one("SELECT id, status FROM approvals WHERE idempotency_key = ?", (idempotency_key,))
        if existing:
            return {"approval_id": existing["id"], "status": existing["status"], "duplicate": True}
        approval_id = self.db.execute(
            """INSERT INTO approvals (run_id, kind, action, title, reason, payload_json, status, idempotency_key, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (run_id, kind, action, title, reason, to_json(payload), idempotency_key, utcnow()),
        )
        return {"approval_id": approval_id, "status": "pending", "duplicate": False}

    def get(self, approval_id: int) -> dict | None:
        row = self.db.query_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return self._decode(row) if row else None

    def list(self, status: str | None = None, limit: int = 200) -> list[dict]:
        """Refunds first, then escalations, then emails; newest first within each group."""
        order = "CASE action WHEN 'issue_refund' THEN 0 WHEN 'human_review' THEN 1 ELSE 2 END, id DESC"
        if status:
            rows = self.db.query(f"SELECT * FROM approvals WHERE status = ? ORDER BY {order} LIMIT ?", (status, limit))
        else:
            rows = self.db.query(f"SELECT * FROM approvals ORDER BY {order} LIMIT ?", (limit,))
        return [self._decode(r) for r in rows]

    def approve(self, approval_id: int, decided_by: str = "dashboard", note: str = "") -> dict:
        item = self._pending(approval_id)
        if item["kind"] == "review":
            return self._decide(item, "resolved", decided_by, note, {"resolved": True})
        executor = self._executors.get(item["action"])
        if executor is None:
            raise ApprovalError(f"No executor is registered for action {item['action']!r}.")
        token = current_run_id.set(item["run_id"])   # integration calls are logged under the original run
        try:
            result = executor(item["payload"])
        except Exception as e:
            return self._decide(item, "failed", decided_by, note, {"error": str(e)})
        finally:
            current_run_id.reset(token)
        return self._decide(item, "executed", decided_by, note, result)

    def reject(self, approval_id: int, decided_by: str = "dashboard", note: str = "") -> dict:
        item = self._pending(approval_id)
        return self._decide(item, "rejected", decided_by, note, None)

    # ---------- internals ----------

    def _pending(self, approval_id: int) -> dict:
        item = self.get(approval_id)
        if item is None:
            raise ApprovalError(f"Approval {approval_id} does not exist.")
        if item["status"] != "pending":
            raise ApprovalError(f"Approval {approval_id} was already decided ({item['status']}).")
        return item

    def _decide(self, item: dict, status: str, decided_by: str, note: str, result: dict | None) -> dict:
        self.db.execute(
            """UPDATE approvals SET status = ?, decided_at = ?, decided_by = ?, note = ?, result_json = ?
               WHERE id = ? AND status = 'pending'""",
            (status, utcnow(), decided_by, note or None, None if result is None else to_json(result), item["id"]),
        )
        if self.recorder and item["run_id"]:
            self.recorder.step(
                item["run_id"], "approval", f"{item['action']} {status}",
                status="ok" if status in ("executed", "resolved") else ("error" if status == "failed" else "blocked"),
                input={"approval_id": item["id"], "decided_by": decided_by, "note": note},
                output=result,
            )
        return self.get(item["id"])

    @staticmethod
    def _decode(row: dict) -> dict:
        row = dict(row)
        row["payload"] = json.loads(row.pop("payload_json"))
        row["result"] = json.loads(row.pop("result_json")) if row.get("result_json") else None
        return row

    def redacted(self, item: dict) -> dict:
        """The dashboard shows the full draft to the approver but redacts contact details."""
        return redact(item)
