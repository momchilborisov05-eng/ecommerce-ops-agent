"""
Offline sandbox adapters (the default). They behave like the real APIs - return
ids, URLs and timestamps - but only record the call in SQLite. `fail_times`
simulates transient outages so retries can be tested and demonstrated.
"""

from __future__ import annotations

from ..retry import TransientError
from .base import IntegrationAdapter, IntegrationError


class _Sandbox(IntegrationAdapter):
    backend = "sandbox"

    def __init__(self, *args, fail_times: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_times = fail_times

    def _maybe_fail(self) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TransientError(f"{self.name} sandbox: simulated 503 Service Unavailable")

    def _next_number(self, operation: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM integration_calls WHERE integration = ? AND operation = ? AND status = 'ok'",
            (self.name, operation),
        )
        return row["n"] + 1


class SandboxSlack(_Sandbox):
    name = "slack"

    def post_message(self, channel: str, text: str) -> dict:
        def send() -> dict:
            self._maybe_fail()
            n = self._next_number("chat.postMessage")
            return {"ok": True, "channel": channel, "ts": f"1793160000.{n:06d}"}

        return self._call("chat.postMessage", {"channel": channel, "text": text}, send)


class SandboxClickUp(_Sandbox):
    name = "clickup"

    def create_task(self, name: str, description: str, priority: str, tags: list[str]) -> dict:
        def create() -> dict:
            self._maybe_fail()
            n = self._next_number("task.create")
            task_id = f"sbx{n:05d}"
            return {"id": task_id, "url": f"https://app.clickup.com/t/{task_id}", "status": "to do"}

        request = {"name": name, "description": description, "priority": priority, "tags": tags}
        return self._call("task.create", request, create)


class SandboxGmail(_Sandbox):
    name = "gmail"

    def create_draft(self, to: str, subject: str, body: str, thread_id: str | None) -> dict:
        def create() -> dict:
            self._maybe_fail()
            n = self._next_number("drafts.create")
            return {"draft_id": f"r-sbx-{n:05d}", "thread_id": thread_id}

        request = {"to": to, "subject": subject, "body": body, "thread_id": thread_id}
        return self._call("drafts.create", request, create)

    def send_draft(self, draft_id: str) -> dict:
        def send() -> dict:
            self._maybe_fail()
            exists = self.db.query_one(
                """SELECT 1 FROM integration_calls WHERE integration = 'gmail' AND operation = 'drafts.create'
                   AND status = 'ok' AND response_json LIKE ?""",
                (f'%"{draft_id}"%',),
            )
            if not exists:
                raise IntegrationError(f"Draft {draft_id} does not exist.")
            n = self._next_number("drafts.send")
            return {"message_id": f"msg-sbx-{n:05d}", "draft_id": draft_id, "label_ids": ["SENT"]}

        return self._call("drafts.send", {"draft_id": draft_id}, send)
