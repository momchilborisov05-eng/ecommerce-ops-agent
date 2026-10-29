"""
Structured audit log: one row per run and one row per step (LLM call, tool call,
guardrail decision, approval request, retry, error). Every payload passes through
PII redaction before it is stored.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

from .guardrails import redact
from .storage.db import Database, to_json, utcnow
from .usage import Usage

# The run that is executing in this thread / task, so adapters can tag their calls with it.
current_run_id: ContextVar[int | None] = ContextVar("current_run_id", default=None)


class RunRecorder:
    def __init__(self, db: Database):
        self.db = db
        self._seq: dict[int, int] = {}

    def start_run(self, workflow: str, mode: str, model: str, subject: str) -> int:
        run_id = self.db.execute(
            "INSERT INTO runs (workflow, mode, model, subject, status, started_at, usage_estimated) VALUES (?, ?, ?, ?, 'running', ?, ?)",
            (workflow, mode, model, redact(subject), utcnow(), int(mode == "demo")),
        )
        self._seq[run_id] = 0
        return run_id

    def step(
        self,
        run_id: int,
        kind: str,
        name: str,
        status: str = "ok",
        input: Any = None,
        output: Any = None,
        duration_ms: int | None = None,
        usage: Usage | None = None,
    ) -> int:
        if run_id not in self._seq:   # e.g. an approval decided after the run finished
            self._seq[run_id] = self.db.query_one(
                "SELECT COALESCE(MAX(seq), 0) AS n FROM steps WHERE run_id = ?", (run_id,))["n"]
        self._seq[run_id] += 1
        return self.db.execute(
            """INSERT INTO steps (run_id, seq, kind, name, status, input_json, output_json, duration_ms,
                                  input_tokens, output_tokens, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, self._seq[run_id], kind, name, status,
                None if input is None else to_json(redact(input)),
                None if output is None else to_json(redact(output)),
                duration_ms,
                usage.total_input if usage else None,
                usage.output_tokens if usage else None,
                utcnow(),
            ),
        )

    def finish_run(
        self,
        run_id: int,
        status: str,
        started: float,
        usage: Usage,
        cost: float,
        llm_calls: int,
        tool_calls: int,
        final_text: str | None = None,
        error: str | None = None,
    ) -> None:
        self.db.execute(
            """UPDATE runs SET status = ?, finished_at = ?, duration_ms = ?, llm_calls = ?, tool_calls = ?,
                   input_tokens = ?, output_tokens = ?, cache_read_tokens = ?, cache_write_tokens = ?,
                   cost_usd = ?, final_text = ?, error = ?
               WHERE id = ?""",
            (
                status, utcnow(), int((time.monotonic() - started) * 1000), llm_calls, tool_calls,
                usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens,
                cost, redact(final_text) if final_text else None, redact(error) if error else None, run_id,
            ),
        )
        self._seq.pop(run_id, None)
