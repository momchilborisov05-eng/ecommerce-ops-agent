"""Tool registry: registration rules, per-workflow tool lists, and the checks every call goes through."""

import unittest

from opsagent.guardrails import Policy
from opsagent.tools import Tool, ToolContext, ToolError, ToolRegistry
from opsagent.toolset import build_registry

from helpers import RuntimeTestCase


def schema(**props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


class RegistryRulesTest(unittest.TestCase):
    def test_duplicate_names_are_rejected(self):
        r = ToolRegistry()
        r.register(Tool("a", "A", schema(x={"type": "string"}), lambda c, a: 1))
        with self.assertRaises(ValueError):
            r.register(Tool("a", "A again", schema(), lambda c, a: 1))

    def test_strict_schemas_must_forbid_extra_fields_and_require_everything(self):
        r = ToolRegistry()
        with self.assertRaises(ValueError):
            r.register(Tool("loose", "", {"type": "object", "properties": {}}, lambda c, a: 1))
        with self.assertRaises(ValueError):
            r.register(Tool("optional", "", {"type": "object", "properties": {"x": {"type": "string"}}, "required": [],
                                             "additionalProperties": False}, lambda c, a: 1))

    def test_definitions_only_include_allowed_tools_in_stable_order(self):
        r = build_registry()
        defs = r.definitions({"post_slack_message", "get_order"})
        self.assertEqual([d["name"] for d in defs], ["get_order", "post_slack_message"])
        self.assertTrue(all(d["strict"] for d in defs))
        with self.assertRaises(ValueError):
            r.definitions({"delete_everything"})

    def test_full_toolset_is_registered(self):
        self.assertEqual(set(build_registry().names), {
            "get_order", "lookup_customer", "search_knowledge_base", "get_daily_metrics", "get_reviews",
            "classify_email", "create_clickup_task", "post_slack_message", "draft_gmail_reply", "issue_refund",
            "flag_for_human"})


class ExecuteTest(RuntimeTestCase):
    def ctx(self, workflow="inbox-triage", subject=None):
        return ToolContext(run_id=None, workflow=workflow, store=self.rt.store, adapters=self.rt.adapters,
                           approvals=self.rt.approvals, today=self.settings.store_date, subject=subject or {})

    def policy(self, *names):
        return Policy(allowed_tools=frozenset(names))

    def test_tool_outside_policy_is_blocked(self):
        out = self.rt.registry.execute(self.ctx(), "issue_refund", {"order_id": "x", "amount": 1, "reason": "r"},
                                       self.policy("get_order"))
        self.assertEqual(out.status, "blocked")
        self.assertTrue(out.is_error)

    def test_unknown_tool_is_blocked(self):
        out = self.rt.registry.execute(self.ctx(), "rm_rf", {}, self.policy("get_order"))
        self.assertEqual(out.status, "blocked")

    def test_invalid_arguments_never_reach_the_handler(self):
        out = self.rt.registry.execute(self.ctx(), "get_order", {"order_id": 42, "extra": True}, self.policy("get_order"))
        self.assertEqual(out.status, "error")
        self.assertIn("order_id must be string", out.content)
        self.assertIn("extra is not an allowed field", out.content)

    def test_get_order_returns_items_and_return_window(self):
        order_id = self.rt.db.query_one("SELECT id FROM orders WHERE status = 'delivered' LIMIT 1")["id"]
        out = self.rt.registry.execute(self.ctx(), "get_order", {"order_id": order_id}, self.policy("get_order"))
        self.assertEqual(out.status, "ok")
        self.assertTrue(out.content["items"])
        self.assertIn("eligible", out.content["return_window"])

    def test_tool_errors_are_returned_to_the_model(self):
        out = self.rt.registry.execute(self.ctx(), "get_order", {"order_id": "999-0000000-0000000"}, self.policy("get_order"))
        self.assertEqual(out.status, "error")
        self.assertIn("No order", out.content)

    def test_unexpected_exceptions_become_tool_errors(self):
        r = ToolRegistry()
        r.register(Tool("boom", "", schema(), lambda c, a: 1 / 0))
        out = r.execute(self.ctx(), "boom", {}, self.policy("boom"))
        self.assertEqual(out.status, "error")
        self.assertIn("ZeroDivisionError", out.content)

    def test_tool_error_class(self):
        r = ToolRegistry()

        def handler(c, a):
            raise ToolError("nope")
        r.register(Tool("t", "", schema(), handler))
        self.assertEqual(r.execute(self.ctx(), "t", {}, self.policy("t")).content, "nope")

    def test_side_effects_are_idempotent(self):
        args = {"name": "Reorder", "description": "d", "priority": "high", "tags": [], "source_ref": "reorder:x"}
        p = self.policy("create_clickup_task")
        first = self.rt.registry.execute(self.ctx(), "create_clickup_task", args, p)
        second = self.rt.registry.execute(self.ctx(), "create_clickup_task", {**args, "description": "changed"}, p)
        self.assertEqual(first.status, "ok")
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(second.content["previous_result"]["task_id"], first.content["task_id"])
        calls = self.rt.db.query_one("SELECT COUNT(*) AS n FROM integration_calls WHERE integration = 'clickup'")["n"]
        self.assertEqual(calls, 1)

    def test_reply_goes_to_the_original_sender_only(self):
        email = self.rt.store.email("E-1005")
        p = self.policy("draft_gmail_reply")
        wrong = self.rt.registry.execute(self.ctx(subject=email), "draft_gmail_reply",
                                         {"email_id": "E-1001", "subject": "Re", "body": "Hi"}, p)
        self.assertEqual(wrong.status, "error")
        ok = self.rt.registry.execute(self.ctx(subject=email), "draft_gmail_reply",
                                      {"email_id": "E-1005", "subject": "Re", "body": "Hi"}, p)
        self.assertEqual(ok.status, "pending")
        approval = self.rt.approvals.get(ok.content["approval_id"])
        self.assertEqual(approval["payload"]["to"], email["from_addr"])

    def test_refund_rules(self):
        p = self.policy("issue_refund")
        old = self.rt.db.query_one("""SELECT id, total FROM orders WHERE status = 'delivered'
                                      AND julianday('2026-10-28') - julianday(delivered_at) > 40 LIMIT 1""")
        out = self.rt.registry.execute(self.ctx(), "issue_refund", {"order_id": old["id"], "amount": 5, "reason": "x"}, p)
        self.assertIn("outside the refund window", out.content)
        recent = self.rt.db.query_one("""SELECT o.id, o.total FROM orders o WHERE status = 'delivered'
                                         AND julianday('2026-10-28') - julianday(delivered_at) < 20
                                         AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id) LIMIT 1""")
        too_much = self.rt.registry.execute(
            self.ctx(), "issue_refund", {"order_id": recent["id"], "amount": recent["total"] + 1, "reason": "x"}, p)
        self.assertIn("exceeds", too_much.content)
        ok = self.rt.registry.execute(self.ctx(), "issue_refund", {"order_id": recent["id"], "amount": 5, "reason": "x"}, p)
        self.assertEqual(ok.status, "pending")
        self.assertEqual(self.rt.db.query_one("SELECT COUNT(*) AS n FROM refunds WHERE order_id = ?", (recent["id"],))["n"], 0,
                         "a refund must not be executed before approval")


if __name__ == "__main__":
    unittest.main()
