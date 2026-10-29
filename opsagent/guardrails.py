"""
Guardrails that keep the agent inside safe limits.

- Policy:            which tools a workflow may use, and how many steps / tool calls it gets.
- validate_input():  checks tool arguments against the tool's JSON schema before anything runs.
- redact():          removes personal data (emails, phone numbers, card numbers) from everything
                     written to the audit log and integration log.
- idempotency_key(): a stable key for side effects, so re-running a workflow never creates the
                     same ClickUp task, Slack post or refund twice.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

# ---------- Policy ----------


@dataclass(frozen=True)
class Policy:
    allowed_tools: frozenset[str]
    max_steps: int = 8            # LLM calls per run
    max_tool_calls: int = 20      # tool executions per run
    max_tokens: int = 4000        # per LLM response

    def allows(self, tool_name: str) -> bool:
        return tool_name in self.allowed_tools


class GuardrailViolation(RuntimeError):
    """The agent tried something the policy does not allow."""


# ---------- Input validation (the subset of JSON Schema the tools use) ----------

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
    "array": (list,), "object": (dict,), "null": (type(None),),
}


def validate_input(schema: dict, value: Any, path: str = "input") -> list[str]:
    """Return a list of problems; an empty list means the value is valid."""
    errors: list[str] = []
    types = schema.get("type")
    if types:
        types = [types] if isinstance(types, str) else types
        ok = any(isinstance(value, _TYPES[t]) and not (t in ("integer", "number") and isinstance(value, bool))
                 for t in types)
        if not ok:
            return [f"{path} must be {' or '.join(types)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} must be one of {schema['enum']}, got {value!r}")
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        errors.append(f"{path} is longer than {schema['maxLength']} characters")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} must be <= {schema['maximum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}.{name} is required")
        for name, item in value.items():
            if name in props:
                errors += validate_input(props[name], item, f"{path}.{name}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{name} is not an allowed field")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors += validate_input(schema["items"], item, f"{path}[{i}]")
    return errors


# ---------- PII redaction ----------

_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,16}\d?(?!\d)")
_PHONE = re.compile(r"(?<![\w-])(?:\+?\d{1,2}[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]\d{4}(?![\w-])")
# Order and tracking ids look like long digit runs; protect them before redacting.
_PROTECT = re.compile(r"\b\d{3}-\d{7}-\d{7}\b|\b(?:1Z[0-9A-Z]{16}|9400\d{18}|TBA\d{12})\b")


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def redact_text(text: str) -> str:
    protected: list[str] = []

    def keep(m: re.Match) -> str:
        protected.append(m.group(0))
        return f"\x00{len(protected) - 1}\x00"

    out = _PROTECT.sub(keep, text)

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "[CARD]" if 13 <= len(digits) <= 16 and _luhn_ok(digits) else m.group(0)

    out = _CARD.sub(card, out)
    out = _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", out)
    out = _PHONE.sub("[PHONE]", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], out)


def redact(value: Any) -> Any:
    """Redact personal data in strings anywhere inside nested dicts and lists."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


# ---------- Idempotency ----------


def idempotency_key(tool: str, args: dict, fields: tuple[str, ...]) -> str:
    """Key from the fields that identify the side effect, e.g. ('source_ref',) for a ClickUp task."""
    identity = {f: args.get(f) for f in fields} if fields else args
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()[:20]
    return f"{tool}:{digest}"
