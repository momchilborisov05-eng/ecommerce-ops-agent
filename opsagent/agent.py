"""
The agent loop, written directly on the Messages API tool-use protocol:

    1. Send the system prompt, the allowed tools and the conversation to the model.
    2. If the model answers with tool_use blocks, run each one through the registry
       (allowlist, schema validation, idempotency, approval gating) and send all
       results back in one user message.
    3. Repeat until the model answers without tool calls, or a guardrail stops the run
       (step limit, tool-call budget, refusal, truncated output).

Every model call and every tool call is written to the audit log with its timing,
token usage and (redacted) inputs and outputs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .audit import RunRecorder, current_run_id
from .guardrails import Policy
from .llm import LLMClient, LLMError
from .tools import ToolContext, ToolOutcome, ToolRegistry
from .usage import Usage


@dataclass
class RunResult:
    run_id: int
    status: str            # completed | failed | stopped
    final_text: str
    usage: Usage
    cost_usd: float
    llm_calls: int
    tool_calls: int
    error: str | None = None


def _audit_blocks(content: list[dict]) -> list[dict]:
    """Keep the parts of a response worth reading in a trace (text and tool calls)."""
    out = []
    for b in content:
        if b.get("type") == "text" and b.get("text"):
            out.append({"type": "text", "text": b["text"]})
        elif b.get("type") == "tool_use":
            out.append({"type": "tool_use", "name": b["name"], "input": b.get("input", {})})
        elif b.get("type") == "fallback":
            out.append({"type": "fallback", "detail": {k: v for k, v in b.items() if k != "type"}})
    return out


class Agent:
    def __init__(self, llm: LLMClient, registry: ToolRegistry, recorder: RunRecorder, policy: Policy, system_prompt: str):
        self.llm = llm
        self.registry = registry
        self.recorder = recorder
        self.policy = policy
        self.system_prompt = system_prompt
        self.tools = registry.definitions(policy.allowed_tools)

    def run(self, ctx: ToolContext, task: str, subject: str) -> RunResult:
        started = time.monotonic()
        run_id = self.recorder.start_run(ctx.workflow, self.llm.mode, self.llm.model, subject)
        ctx.run_id = run_id
        token = current_run_id.set(run_id)
        usage, llm_calls, tool_calls = Usage(), 0, 0
        status, final_text, error = "stopped", "", None
        messages: list[dict] = [{"role": "user", "content": task}]
        try:
            for _ in range(self.policy.max_steps):
                t0 = time.monotonic()
                try:
                    response = self.llm.create(system=self.system_prompt, messages=messages,
                                               tools=self.tools, max_tokens=self.policy.max_tokens)
                except LLMError as e:
                    self.recorder.step(run_id, "error", "llm", status="error", output={"error": str(e)})
                    status, error = "failed", str(e)
                    break
                llm_calls += 1
                usage = usage + response.usage
                self.recorder.step(
                    run_id, "llm", response.model, input={"messages": len(messages)},
                    output={"stop_reason": response.stop_reason, "content": _audit_blocks(response.content)},
                    duration_ms=int((time.monotonic() - t0) * 1000), usage=response.usage,
                )

                if response.stop_reason == "refusal":
                    status, error = "failed", "The model declined this request."
                    break
                if response.stop_reason == "max_tokens":
                    status, error = "failed", f"The response hit the {self.policy.max_tokens}-token limit and was cut off."
                    break
                if response.stop_reason == "pause_turn":
                    messages.append({"role": "assistant", "content": response.raw_content})
                    continue
                if not response.tool_uses:
                    status, final_text = "completed", response.text
                    break

                messages.append({"role": "assistant", "content": response.raw_content})
                results = []
                for call in response.tool_uses:
                    if tool_calls >= self.policy.max_tool_calls:
                        outcome = ToolOutcome("blocked", f"Tool-call budget of {self.policy.max_tool_calls} is used up. "
                                                         "Finish with a summary.")
                    else:
                        t1 = time.monotonic()
                        outcome = self.registry.execute(ctx, call["name"], call.get("input", {}), self.policy)
                        tool_calls += 1
                    self.recorder.step(
                        run_id, "guardrail" if outcome.status == "blocked" else "tool", call["name"],
                        status=outcome.status, input=call.get("input", {}), output=outcome.content,
                        duration_ms=int((time.monotonic() - t1) * 1000) if outcome.status != "blocked" else 0,
                    )
                    result = {"type": "tool_result", "tool_use_id": call["id"], "content": outcome.as_text()}
                    if outcome.is_error:
                        result["is_error"] = True
                    results.append(result)
                messages.append({"role": "user", "content": results})   # all results in one message
            else:
                error = f"Stopped after {self.policy.max_steps} model calls (max_steps guardrail)."
                self.recorder.step(run_id, "guardrail", "max_steps", status="blocked", output={"error": error})
        except Exception as e:   # never leave a run stuck in "running"
            status, error = "failed", f"{type(e).__name__}: {e}"
            self.recorder.step(run_id, "error", "agent", status="error", output={"error": error})
            raise
        finally:
            cost = usage.cost(self.llm.model)
            self.recorder.finish_run(run_id, status, started, usage, cost, llm_calls, tool_calls, final_text, error)
            current_run_id.reset(token)
        return RunResult(run_id, status, final_text, usage, cost, llm_calls, tool_calls, error)
