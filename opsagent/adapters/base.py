"""
Adapter interfaces for the business tools the agent works with.

Each integration has two implementations with the same interface:
- a sandbox adapter (default): no network, deterministic ids, works offline;
- a live adapter: the real REST API, used only when credentials are configured.

Both go through IntegrationAdapter._call(), which adds retries with backoff and
writes every call (redacted) to the integration_calls table.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from ..audit import current_run_id
from ..guardrails import redact
from ..retry import TransientError, retry_call
from ..storage.db import Database, to_json, utcnow


class IntegrationError(RuntimeError):
    """A non-retryable integration failure, e.g. invalid credentials or a bad request."""


class SlackAdapter(Protocol):
    name: str
    backend: str

    def post_message(self, channel: str, text: str) -> dict: ...


class ClickUpAdapter(Protocol):
    name: str
    backend: str

    def create_task(self, name: str, description: str, priority: str, tags: list[str]) -> dict: ...


class GmailAdapter(Protocol):
    name: str
    backend: str

    def create_draft(self, to: str, subject: str, body: str, thread_id: str | None) -> dict: ...

    def send_draft(self, draft_id: str) -> dict: ...


class IntegrationAdapter:
    name = "integration"
    backend = "sandbox"

    def __init__(self, db: Database, attempts: int = 3, base_delay: float = 0.5, sleep: Callable[[float], None] = time.sleep):
        self.db = db
        self.attempts = attempts
        self.base_delay = base_delay
        self.sleep = sleep

    def _call(self, operation: str, request: dict, fn: Callable[[], dict]) -> dict:
        started = time.monotonic()
        attempts = 0

        def attempt() -> dict:
            nonlocal attempts
            attempts += 1
            return fn()

        status, response, error = "ok", None, None
        try:
            response = retry_call(attempt, attempts=self.attempts, base_delay=self.base_delay, sleep=self.sleep)
            return response
        except Exception as e:
            status, error = "error", e
            raise
        finally:
            self.db.execute(
                """INSERT INTO integration_calls (run_id, integration, backend, operation, status, attempts,
                       request_json, response_json, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    current_run_id.get(), self.name, self.backend, operation, status, attempts,
                    to_json(redact(request)),
                    to_json(redact(response if error is None else {"error": str(error)})),
                    int((time.monotonic() - started) * 1000), utcnow(),
                ),
            )


def check_http(response: httpx.Response) -> dict[str, Any]:
    """Turn an HTTP response into JSON, mapping 429/5xx to TransientError and other errors to IntegrationError."""
    if response.status_code == 429 or response.status_code >= 500:
        raise TransientError(f"HTTP {response.status_code} from {response.request.url.host}")
    if response.status_code >= 400:
        raise IntegrationError(f"HTTP {response.status_code} from {response.request.url.host}: {response.text[:300]}")
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {"text": response.text}


def http_call(fn: Callable[[], httpx.Response]) -> dict[str, Any]:
    try:
        return check_http(fn())
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise TransientError(f"Network error: {e}") from e
