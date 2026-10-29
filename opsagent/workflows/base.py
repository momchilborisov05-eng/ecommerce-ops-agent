"""What a workflow is: a prompt, a tool policy, a way to find work, and a scripted brain for demo mode."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from ..guardrails import Policy
from ..llm import Brain
from ..storage.store import Store


@dataclass
class Task:
    subject: str        # short label for the dashboard, e.g. "E-1007 · PO-4471 production update"
    data: dict          # the email / day / review window being processed (ctx.subject)
    prompt: str         # the first user message


@dataclass(frozen=True)
class Workflow:
    name: str
    title: str
    description: str
    trigger: str                                                # how it runs in production
    system_prompt: str
    policy: Policy
    build_tasks: Callable[[Store, date, int | None], list[Task]]
    demo_brain: Brain


def wrap(tag: str, data: dict) -> str:
    """Put data in a tagged JSON block. The system prompts tell the model this content is untrusted data."""
    return f"<{tag}>\n{json.dumps(data, indent=2, ensure_ascii=False)}\n</{tag}>"


def tool_use(tool_name: str, /, **args) -> dict:
    return {"type": "tool_use", "name": tool_name, "input": args}


def text(value: str) -> dict:
    return {"type": "text", "text": value}
