"""End-to-end runs of each workflow in demo mode, plus the live Claude client against a fake SDK client."""

import json
import unittest
from datetime import timedelta
from types import SimpleNamespace

import anthropic
import httpx

from opsagent.llm import ClaudeLLM
from opsagent.workflows.daily_digest import digest_text

from helpers import RuntimeTestCase


class InboxTriageTest(RuntimeTestCase):
    """Triage the whole seeded inbox once, then check the outcome from several angles."""

    @classmethod
    def setUpClass(cls):
        cls.shared = RuntimeTestCase()
        cls.shared.setUp()
        cls.results = cls.shared.rt.run_workflow("inbox-triage")

    @classmethod
    def tearDownClass(cls):
        cls.shared.tearDown()

    def setUp(self):   # reuse the shared runtime instead of seeding a new database per test
        self.rt, self.settings = self.shared.rt, self.shared.settings
        self.triage = {r["email_id"]: r for r in self.rt.db.query("SELECT * FROM triage")}

    def tearDown(self):
        pass

    def email_id(self, subject_like):
        return self.rt.db.query_one("SELECT id FROM emails WHERE subject LIKE ?", (subject_like,))["id"]

    def test_every_email_is_processed_once(self):
        self.assertEqual(len(self.results), 40)
        self.assertTrue(all(r.status == "completed" for r in self.results))
        self.assertEqual(len(self.triage), 40)
        self.assertEqual(self.rt.store.untriaged_emails(), [], "a second run would find nothing new")

    def test_categories(self):
        counts = {}
        for t in self.triage.values():
            counts[t["category"]] = counts.get(t["category"], 0) + 1
        self.assertEqual(counts["spam"], 7)
        self.assertEqual(counts["supplier"], 6)
        self.assertEqual(counts["amazon_notification"], 7)
        self.assertEqual(self.triage[self.email_id("%verify your seller%")]["category"], "spam")

    def test_order_ids_are_extracted(self):
        t = self.triage[self.email_id("Where is my order%")]
        self.assertRegex(t["order_ids"], r"^\d{3}-\d{7}-\d{7}$")

    def test_damaged_items_get_a_refund_request_and_a_reply(self):
        refunds = [a for a in self.rt.approvals.list("pending") if a["action"] == "issue_refund"]
        self.assertGreaterEqual(len(refunds), 3)
        for a in refunds:
            order = self.rt.store.get_order(a["payload"]["order_id"])
            self.assertTrue(order["return_window"]["eligible"])
            self.assertLessEqual(a["payload"]["amount"], order["total"])
        self.assertEqual(self.rt.db.query_one("SELECT COUNT(*) AS n FROM refunds WHERE source = 'approved-refund'")["n"], 0)

    def test_prompt_injection_is_flagged_not_obeyed(self):
        eid = self.email_id("Question about order%")
        run_id = self.triage[eid]["run_id"]
        tools = [s["name"] for s in self.rt.db.query("SELECT name FROM steps WHERE run_id = ? AND kind = 'tool'", (run_id,))]
        self.assertIn("flag_for_human", tools)
        self.assertNotIn("issue_refund", tools)

    def test_replies_are_grounded_in_order_data(self):
        reply = next(a for a in self.rt.approvals.list("pending")
                     if a["action"] == "send_email_reply" and "tracking" in a["payload"]["body"].lower())
        order_id = next(o for o in json.dumps(reply["payload"]).split() if o.count("-") == 2 and o[:3] == "113")
        order = self.rt.store.get_order(order_id.strip(".,\""))
        self.assertIn(order["tracking"], reply["payload"]["body"])

    def test_spam_gets_no_reply(self):
        spam_ids = {k for k, t in self.triage.items() if t["category"] == "spam"}
        replied = {a["payload"]["email_id"] for a in self.rt.approvals.list() if a["action"] == "send_email_reply"}
        self.assertFalse(spam_ids & replied)

    def test_trace_and_redaction(self):
        run_id = self.results[0].run_id
        kinds = [s["kind"] for s in self.rt.db.query("SELECT kind FROM steps WHERE run_id = ?", (run_id,))]
        self.assertEqual(kinds[0], "llm")
        self.assertIn("tool", kinds)
        dump = json.dumps(self.rt.db.query("SELECT input_json, output_json FROM steps"))
        relay = self.rt.db.query_one("SELECT email FROM customers WHERE marketplace = 'amazon' LIMIT 1")["email"]
        self.assertNotIn("(555)", dump)
        self.assertNotIn(relay, dump)


class DailyDigestTest(RuntimeTestCase):
    def test_digest_numbers_match_the_store(self):
        [result] = self.rt.run_workflow("daily-digest")
        self.assertEqual(result.status, "completed")
        day = self.settings.store_date - timedelta(days=1)
        metrics = self.rt.store.daily_metrics(day)
        call = self.rt.db.query_one("SELECT request_json FROM integration_calls WHERE integration = 'slack'")
        text = json.loads(call["request_json"])["text"]
        self.assertEqual(text, digest_text(metrics))
        self.assertIn(f"*Orders:* {metrics['orders']} ", text)
        self.assertIn(f"${metrics['revenue']:,.2f}", text)
        tasks = self.rt.db.query("SELECT request_json FROM integration_calls WHERE integration = 'clickup'")
        self.assertEqual(len(tasks), 1)
        self.assertIn("NP-MF-15", tasks[0]["request_json"])


class ReviewMonitorTest(RuntimeTestCase):
    def test_tasks_flags_and_idempotent_rerun(self):
        [first] = self.rt.run_workflow("review-monitor")
        since = self.settings.store_date - timedelta(days=7)
        negative = self.rt.store.reviews(since=since, max_rating=3)
        tasks = self.rt.db.query("SELECT request_json FROM integration_calls WHERE integration = 'clickup'")
        self.assertEqual(len(tasks), len(negative))
        urgent = [t for t in tasks if json.loads(t["request_json"])["priority"] == "urgent"]
        flags = [a for a in self.rt.approvals.list() if a["action"] == "human_review"]
        self.assertEqual(len(urgent), len(flags))
        self.assertTrue(any("Overheated" in f["title"] or "Frother" in f["title"] for f in flags))

        [second] = self.rt.run_workflow("review-monitor")
        self.assertEqual(second.status, "completed")
        self.assertEqual(len(self.rt.db.query("SELECT 1 FROM integration_calls WHERE integration = 'clickup'")), len(negative))
        self.assertEqual(len(self.rt.db.query("SELECT 1 FROM integration_calls WHERE integration = 'slack'")), 1)
        self.assertIn("already existed", second.final_text)


# ---------- The live Claude client, with a fake SDK client (no network) ----------

class FakeBlock(SimpleNamespace):
    """Stands in for an SDK content block (attribute access + model_dump)."""

    def model_dump(self, **kwargs):
        return dict(vars(self))


def _resp(content, stop, inp=1200, out=80, cache_read=0):
    blocks = [FakeBlock(**b) for b in content]
    usage = SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=cache_read,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(content=blocks, stop_reason=stop, usage=usage, model="claude-opus-5-5")


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ClaudeClientTest(RuntimeTestCase):
    def fake(self, responses):
        messages = FakeMessages(responses)
        return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages

    def test_live_loop_with_tool_use(self):
        rate_limited = anthropic.RateLimitError(
            "slow down", response=httpx.Response(429, request=httpx.Request("POST", "https://api.anthropic.com")), body=None)
        client, messages = self.fake([
            rate_limited,
            _resp([{"type": "text", "text": "Checking."},
                   {"type": "tool_use", "id": "toolu_1", "name": "get_daily_metrics", "input": {"date": "2026-10-27"}}],
                  "tool_use"),
            _resp([{"type": "text", "text": "Digest ready."}], "end_turn", cache_read=1000),
        ])
        sleeps = []
        llm = ClaudeLLM(client=client, model="claude-opus-5-5", sleep=sleeps.append)
        rt = self.make_runtime(llm=llm)
        [result] = rt.run_workflow("daily-digest")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.final_text, "Digest ready.")
        self.assertEqual(len(sleeps), 1, "the 429 was retried once")
        first = messages.calls[0]
        self.assertEqual(first["model"], "claude-opus-5-5")
        self.assertEqual(first["fallbacks"], "default")
        self.assertEqual(first["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(first["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertTrue(all(t["strict"] for t in first["tools"]))
        self.assertEqual({t["name"] for t in first["tools"]},
                         {"get_daily_metrics", "get_reviews", "post_slack_message", "create_clickup_task"})
        followup = messages.calls[2]["messages"]
        self.assertEqual(followup[-1]["content"][0]["tool_use_id"], "toolu_1")
        self.assertIn('"orders"', followup[-1]["content"][0]["content"])
        run = self.rt.db.query_one("SELECT * FROM runs WHERE id = ?", (result.run_id,))
        self.assertEqual((run["input_tokens"], run["output_tokens"], run["cache_read_tokens"]), (2400, 160, 1000))
        self.assertAlmostEqual(run["cost_usd"], (2400 * 4 + 160 * 20 + 1000 * 0.2) / 1e6, places=6)
        self.assertEqual(run["usage_estimated"], 0)

    def test_auth_error_fails_cleanly(self):
        err = anthropic.AuthenticationError(
            "bad key", response=httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com")), body=None)
        client, _ = self.fake([err])
        rt = self.make_runtime(llm=ClaudeLLM(client=client, sleep=lambda s: None))
        [result] = rt.run_workflow("daily-digest")
        self.assertEqual(result.status, "failed")
        self.assertIn("ANTHROPIC_API_KEY", result.error)


if __name__ == "__main__":
    unittest.main()
