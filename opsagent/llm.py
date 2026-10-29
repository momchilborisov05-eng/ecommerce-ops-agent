"""
LLM clients behind one small interface, so the agent loop does not care which one it talks to.

- ClaudeLLM:   the Anthropic Messages API with client-side tools (live mode).
- ScriptedLLM: deterministic, rule-based responses for demo mode and tests. It returns the
               same content-block shapes as Claude, so the agent loop, guardrails, approvals
               and audit log run exactly the same code path. Its token counts are estimates.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic

from .config import MODEL
from .retry import retry_call
from .usage import Usage

FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}


class LLMError(RuntimeError):
    """The model could not produce a usable response (refusal, auth problem, outage after retries)."""


@dataclass
class LLMResponse:
    content: list[dict]                 # content blocks as plain dicts (for the audit log and the agent)
    stop_reason: str
    usage: Usage
    model: str
    raw_content: list[Any] = field(default_factory=list)   # what to send back to the API next turn

    @property
    def tool_uses(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    @property
    def text(self) -> str:
        return "\n".join(b["text"] for b in self.content if b.get("type") == "text").strip()


class LLMClient(Protocol):
    mode: str
    model: str
    estimated_usage: bool

    def create(self, *, system: str, messages: list[dict], tools: list[dict], max_tokens: int) -> LLMResponse: ...


# ---------- Claude ----------

def _retryable(e: Exception) -> bool:
    if isinstance(e, (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError)):
        return True
    return isinstance(e, anthropic.APIStatusError) and e.status_code >= 500


class ClaudeLLM:
    mode = "live"
    estimated_usage = False

    def __init__(self, client: Any = None, model: str = MODEL, effort: str = "medium",
                 on_retry: Callable[[int, Exception, float], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        # Retries are done here (with logging into the audit trail), so the SDK's own retries are off.
        self.client = client or anthropic.Anthropic(max_retries=0)
        self.model = model
        self.effort = effort
        self.on_retry = on_retry
        self.sleep = sleep

    def create(self, *, system: str, messages: list[dict], tools: list[dict], max_tokens: int) -> LLMResponse:
        extra: dict[str, Any] = {}
        if self.model in FALLBACK_MODELS:
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

        def call():
            return self.client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                # Tools + system prompt are identical for every run of a workflow, so they are prompt-cached.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=tools,
                messages=messages,
                output_config={"effort": self.effort},
                **extra,
            )

        try:
            response = retry_call(call, attempts=4, base_delay=1.0, is_retryable=_retryable,
                                  on_retry=self.on_retry, sleep=self.sleep)
        except anthropic.AuthenticationError as e:
            raise LLMError("The Anthropic API key is invalid. Check ANTHROPIC_API_KEY in .env.") from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"The API rejected the request: {e.message}") from e
        except anthropic.APIError as e:
            raise LLMError(f"Claude API error after retries: {e}") from e

        u = response.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        content = [b.model_dump(mode="json", exclude_none=True) for b in response.content]
        return LLMResponse(content=content, stop_reason=response.stop_reason or "end_turn", usage=usage,
                           model=response.model, raw_content=list(response.content))


# ---------- Scripted (demo mode) ----------

@dataclass
class ToolExchange:
    name: str
    input: dict
    result: Any
    is_error: bool


@dataclass
class Turn:
    """What a scripted brain sees: the task input, the turn number and every tool call so far."""
    task_input: dict
    number: int
    history: list[ToolExchange]

    def results(self, name: str) -> list[ToolExchange]:
        return [h for h in self.history if h.name == name]


Brain = Callable[[Turn], list[dict]]
_INPUT = re.compile(r"<(input|email)>\s*(\{.*\})\s*</\1>", re.S)


def parse_history(messages: list[dict]) -> tuple[dict, int, list[ToolExchange]]:
    first = messages[0]["content"]
    m = _INPUT.search(first if isinstance(first, str) else json.dumps(first))
    task_input = json.loads(m.group(2)) if m else {}
    calls: dict[str, dict] = {}
    history: list[ToolExchange] = []
    turns = 0
    for msg in messages[1:]:
        blocks = msg["content"] if isinstance(msg["content"], list) else []
        if msg["role"] == "assistant":
            turns += 1
            for b in blocks:
                b = b if isinstance(b, dict) else b.model_dump()
                if b.get("type") == "tool_use":
                    calls[b["id"]] = b
        else:
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b["tool_use_id"] in calls:
                    call = calls[b["tool_use_id"]]
                    try:
                        result = json.loads(b["content"])
                    except (TypeError, ValueError):
                        result = b["content"]
                    history.append(ToolExchange(call["name"], call["input"], result, bool(b.get("is_error"))))
    return task_input, turns, history


def _estimate_tokens(value: Any) -> int:
    return max(1, len(json.dumps(value, default=str)) // 4)   # ~4 characters per token


class ScriptedLLM:
    """Plays the model's part with a rule-based brain. Clearly labelled as demo output everywhere it shows up."""

    mode = "demo"
    estimated_usage = True

    def __init__(self, brain: Brain, model: str = "scripted-demo"):
        self.brain = brain
        self.model = model
        self._ids = 0

    def create(self, *, system: str, messages: list[dict], tools: list[dict], max_tokens: int) -> LLMResponse:
        task_input, turns, history = parse_history(messages)
        blocks = self.brain(Turn(task_input, turns, history))
        for b in blocks:
            if b["type"] == "tool_use" and "id" not in b:
                self._ids += 1
                b["id"] = f"toolu_demo_{self._ids:04d}"
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        # Rough token estimate so the cost dashboard has something to show; never presented as real usage.
        prompt = _estimate_tokens([system, tools])
        usage = Usage(
            input_tokens=_estimate_tokens(messages) + (prompt if turns == 0 else 0),
            output_tokens=_estimate_tokens(blocks),
            cache_read_tokens=prompt if turns > 0 else 0,
        )
        return LLMResponse(content=blocks, stop_reason=stop, usage=usage, model=self.model, raw_content=blocks)
