"""
Tool registry.

A Tool is a JSON schema (what Claude sees) plus a Python handler (what runs).
The registry gives each workflow only the tools its policy allows, and runs
every call through the same checks:

    allowlist  ->  schema validation  ->  idempotency lookup  ->  handler  ->  idempotency store
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from .guardrails import Policy, idempotency_key, validate_input
from .storage.db import to_json, utcnow

if TYPE_CHECKING:
    from .adapters import Adapters
    from .approvals import ApprovalQueue
    from .storage.store import Store


class ToolError(RuntimeError):
    """An error the model should see and can react to, e.g. 'order not found'."""


@dataclass
class ToolContext:
    run_id: int | None
    workflow: str
    store: "Store"
    adapters: "Adapters"
    approvals: "ApprovalQueue"
    today: date
    subject: dict = field(default_factory=dict)   # the email / day / review batch being processed


Handler = Callable[[ToolContext, dict], Any]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Handler
    side_effect: bool = False                 # changes something outside the agent (task, message, draft, refund)
    idempotency_fields: tuple[str, ...] = ()  # which arguments identify the side effect
    requires_approval: bool = False           # the handler queues the action instead of doing it

    def definition(self) -> dict:
        """The tool definition sent to Claude. strict=True makes Claude's arguments match the schema exactly."""
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema, "strict": True}


@dataclass
class ToolOutcome:
    status: str          # ok | error | blocked | duplicate | pending
    content: Any         # what goes back to the model
    idempotency_key: str | None = None

    @property
    def is_error(self) -> bool:
        return self.status in ("error", "blocked")

    def as_text(self) -> str:
        return self.content if isinstance(self.content, str) else json.dumps(self.content, default=str)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name!r} is already registered.")
        schema = tool.input_schema
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ValueError(f"Tool {tool.name!r}: input_schema must be an object with additionalProperties: false.")
        missing = set(schema.get("properties", {})) - set(schema.get("required", []))
        if missing:
            raise ValueError(f"Tool {tool.name!r}: strict tools must list every property as required: {sorted(missing)}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def definitions(self, allowed: Iterable[str]) -> list[dict]:
        """Definitions in registration order (a stable order keeps the prompt cache valid)."""
        allowed = set(allowed)
        unknown = allowed - set(self._tools)
        if unknown:
            raise ValueError(f"Policy allows unknown tools: {sorted(unknown)}")
        return [t.definition() for t in self._tools.values() if t.name in allowed]

    def execute(self, ctx: ToolContext, name: str, args: Any, policy: Policy) -> ToolOutcome:
        tool = self._tools.get(name)
        if tool is None or not policy.allows(name):
            return ToolOutcome("blocked", f"Tool '{name}' is not available in the {ctx.workflow} workflow.")
        problems = validate_input(tool.input_schema, args)
        if problems:
            return ToolOutcome("error", "Invalid arguments: " + "; ".join(problems))

        key = None
        if tool.side_effect:
            key = idempotency_key(name, args, tool.idempotency_fields)
            done = ctx.store.db.query_one("SELECT result_json FROM idempotency WHERE key = ?", (key,))
            if done:
                previous = json.loads(done["result_json"])
                return ToolOutcome("duplicate", {"duplicate": True, "note": "Already done earlier; not repeated.",
                                                 "previous_result": previous}, key)
        try:
            result = tool.handler(ctx, args)
        except ToolError as e:
            return ToolOutcome("error", str(e), key)
        except Exception as e:   # a bug or an integration outage; the model is told, the run continues
            return ToolOutcome("error", f"{name} failed: {type(e).__name__}: {e}", key)

        if key:
            ctx.store.db.execute(
                "INSERT OR IGNORE INTO idempotency (key, tool, run_id, result_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (key, name, ctx.run_id, to_json(result), utcnow()),
            )
        status = "pending" if isinstance(result, dict) and result.get("status") == "pending_approval" else "ok"
        return ToolOutcome(status, result, key)
