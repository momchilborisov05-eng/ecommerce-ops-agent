"""Guardrails: input validation, PII redaction, idempotency keys, and the agent loop's limits."""

import unittest

from opsagent.agent import Agent
from opsagent.guardrails import Policy, idempotency_key, redact, redact_text, validate_input
from opsagent.llm import LLMResponse, ScriptedLLM
from opsagent.retry import TransientError, retry_call
from opsagent.tools import ToolContext
from opsagent.usage import Usage

from helpers import RuntimeTestCase


class ValidateInputTest(unittest.TestCase):
    schema = {
        "type": "object", "additionalProperties": False, "required": ["a", "b", "tags", "n"],
        "properties": {"a": {"type": "string", "enum": ["x", "y"]}, "b": {"type": ["string", "null"]},
                       "tags": {"type": "array", "items": {"type": "string"}}, "n": {"type": "integer"}},
    }

    def test_valid(self):
        self.assertEqual(validate_input(self.schema, {"a": "x", "b": None, "tags": ["t"], "n": 3}), [])

    def test_problems_are_listed(self):
        errors = validate_input(self.schema, {"a": "z", "tags": [1], "n": True, "zzz": 1})
        text = " | ".join(errors)
        self.assertIn("input.a must be one of", text)
        self.assertIn("input.b is required", text)
        self.assertIn("input.tags[0] must be string", text)
        self.assertIn("input.n must be integer", text)   # booleans are not integers
        self.assertIn("input.zzz is not an allowed field", text)


class RedactionTest(unittest.TestCase):
    def test_emails_phones_and_cards(self):
        out = redact_text("Mail jane.doe@gmail.com or call (555) 201-3344 / +1 555 201 3344. Card 4242 4242 4242 4242.")
        self.assertNotIn("jane.doe", out)
        self.assertIn("j***@gmail.com", out)
        self.assertNotIn("201-3344", out)
        self.assertNotIn("201 3344", out)
        self.assertIn("[CARD]", out)

    def test_order_and_tracking_ids_survive(self):
        text = "Order 113-4829174-2948172 shipped, tracking 1Z999AA10123456784 and 9400111202555842261234, SH-10234."
        self.assertEqual(redact_text(text), text)

    def test_non_luhn_numbers_are_not_cards(self):
        self.assertEqual(redact_text("Invoice 1234567890123"), "Invoice 1234567890123")

    def test_nested_structures(self):
        out = redact({"to": "a.b@x.com", "items": [{"phone": "555-201-3344"}], "n": 5})
        self.assertEqual(out["to"], "a***@x.com")
        self.assertEqual(out["items"][0]["phone"], "[PHONE]")
        self.assertEqual(out["n"], 5)


class IdempotencyKeyTest(unittest.TestCase):
    def test_key_depends_only_on_identity_fields(self):
        a = idempotency_key("create_clickup_task", {"source_ref": "r:1", "name": "A", "description": "x"}, ("source_ref", "name"))
        b = idempotency_key("create_clickup_task", {"source_ref": "r:1", "name": "A", "description": "y"}, ("source_ref", "name"))
        c = idempotency_key("create_clickup_task", {"source_ref": "r:2", "name": "A", "description": "x"}, ("source_ref", "name"))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


class RetryTest(unittest.TestCase):
    def test_backoff_grows_and_stops(self):
        delays, attempts = [], []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise TransientError("503")
            return "ok"
        self.assertEqual(retry_call(flaky, attempts=3, base_delay=1, sleep=delays.append), "ok")
        self.assertEqual(len(delays), 2)
        self.assertGreaterEqual(delays[1], delays[0] * 1.6)

    def test_non_retryable_errors_raise_immediately(self):
        calls = []

        def bad():
            calls.append(1)
            raise ValueError("bad request")
        with self.assertRaises(ValueError):
            retry_call(bad, attempts=5, sleep=lambda s: None)
        self.assertEqual(len(calls), 1)


class AgentLimitsTest(RuntimeTestCase):
    def run_agent(self, llm, policy):
        agent = Agent(llm, self.rt.registry, self.rt.recorder, policy, "system")
        ctx = ToolContext(None, "test", self.rt.store, self.rt.adapters, self.rt.approvals, self.settings.store_date)
        return agent.run(ctx, "do it", "test run")

    def steps(self, run_id):
        return self.rt.db.query("SELECT kind, name, status FROM steps WHERE run_id = ? ORDER BY seq", (run_id,))

    def test_disallowed_tool_is_blocked_and_run_continues(self):
        def brain(turn):
            if turn.number == 0:
                return [{"type": "tool_use", "name": "issue_refund", "input": {"order_id": "x", "amount": 1, "reason": "r"}}]
            return [{"type": "text", "text": "done"}]
        result = self.run_agent(ScriptedLLM(brain), Policy(frozenset({"get_order"})))
        self.assertEqual(result.status, "completed")
        self.assertIn(("guardrail", "issue_refund", "blocked"), [tuple(s.values()) for s in self.steps(result.run_id)])

    def test_max_steps_stops_a_looping_agent(self):
        brain = lambda turn: [{"type": "tool_use", "name": "search_knowledge_base", "input": {"query": "returns"}}]  # noqa: E731
        result = self.run_agent(ScriptedLLM(brain), Policy(frozenset({"search_knowledge_base"}), max_steps=3))
        self.assertEqual(result.status, "stopped")
        self.assertEqual(result.llm_calls, 3)
        self.assertIn("max_steps", result.error)

    def test_tool_call_budget(self):
        brain = lambda turn: ([{"type": "tool_use", "name": "search_knowledge_base", "input": {"query": f"q{i}"}}  # noqa: E731
                               for i in range(5)] if turn.number == 0 else [{"type": "text", "text": "ok"}])
        result = self.run_agent(ScriptedLLM(brain), Policy(frozenset({"search_knowledge_base"}), max_tool_calls=2))
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(sum(1 for s in self.steps(result.run_id) if s["kind"] == "guardrail"), 3)

    def test_refusal_and_truncation_fail_the_run(self):
        for stop, message in (("refusal", "declined"), ("max_tokens", "cut off")):
            class Fake:
                mode, model, estimated_usage = "live", "claude-opus-5-5", False

                def create(self, **kw):
                    return LLMResponse([{"type": "text", "text": "..."}], stop, Usage(10, 5), "claude-opus-5-5", [])
            result = self.run_agent(Fake(), Policy(frozenset({"get_order"})))
            self.assertEqual(result.status, "failed")
            self.assertIn(message, result.error)

    def test_run_records_usage_and_cost(self):
        result = self.run_agent(ScriptedLLM(lambda t: [{"type": "text", "text": "hi"}]), Policy(frozenset({"get_order"})))
        run = self.rt.db.query_one("SELECT * FROM runs WHERE id = ?", (result.run_id,))
        self.assertEqual(run["status"], "completed")
        self.assertGreater(run["input_tokens"], 0)
        self.assertGreater(run["cost_usd"], 0)
        self.assertEqual(run["usage_estimated"], 1)


if __name__ == "__main__":
    unittest.main()
