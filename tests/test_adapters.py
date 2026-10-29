"""Adapters: sandbox implementations, retries, and the live Slack / ClickUp / Gmail clients against a mock HTTP transport."""

import base64
import json
import unittest
from email import message_from_bytes

import httpx

from opsagent.adapters import IntegrationError, build_adapters
from opsagent.adapters.live import LiveClickUp, LiveGmail, LiveSlack
from opsagent.adapters.sandbox import SandboxClickUp, SandboxGmail, SandboxSlack
from opsagent.config import Settings
from opsagent.retry import TransientError

from helpers import RuntimeTestCase

no_sleep = lambda s: None  # noqa: E731


class SandboxAdapterTest(RuntimeTestCase):
    seed = False

    def calls(self, integration):
        return self.rt.db.query("SELECT * FROM integration_calls WHERE integration = ? ORDER BY id", (integration,))

    def test_default_adapters_are_sandboxes(self):
        self.assertEqual(self.rt.adapters.backends(), {"slack": "sandbox", "clickup": "sandbox", "gmail": "sandbox"})

    def test_live_adapters_only_when_credentials_are_set(self):
        s = Settings(db_path=self.tmp / "x.db", slack_bot_token="xoxb-test", clickup_token="pk_1", clickup_list_id="9")
        adapters = build_adapters(s, self.rt.db)
        self.assertEqual(adapters.backends(), {"slack": "live", "clickup": "live", "gmail": "sandbox"})

    def test_slack_records_the_call(self):
        res = SandboxSlack(self.rt.db).post_message("#ops-daily", "hello")
        self.assertTrue(res["ok"])
        [call] = self.calls("slack")
        self.assertEqual((call["operation"], call["status"], call["backend"], call["attempts"]),
                         ("chat.postMessage", "ok", "sandbox", 1))
        self.assertEqual(json.loads(call["request_json"])["text"], "hello")

    def test_clickup_ids_are_sequential(self):
        cu = SandboxClickUp(self.rt.db)
        a = cu.create_task("A", "d", "high", ["x"])
        b = cu.create_task("B", "d", "low", [])
        self.assertEqual((a["id"], b["id"]), ("sbx00001", "sbx00002"))
        self.assertTrue(a["url"].startswith("https://app.clickup.com/t/"))

    def test_gmail_send_requires_an_existing_draft(self):
        gm = SandboxGmail(self.rt.db, sleep=no_sleep)
        draft = gm.create_draft("a@b.com", "Re: hi", "body", "thr-1")
        self.assertEqual(gm.send_draft(draft["draft_id"])["label_ids"], ["SENT"])
        with self.assertRaises(IntegrationError):
            gm.send_draft("r-unknown")
        self.assertEqual(self.calls("gmail")[-1]["status"], "error")

    def test_transient_failures_are_retried(self):
        slack = SandboxSlack(self.rt.db, fail_times=2, sleep=no_sleep)
        slack.post_message("#reviews", "x")
        [call] = self.calls("slack")
        self.assertEqual((call["status"], call["attempts"]), ("ok", 3))

    def test_gives_up_after_max_attempts_and_logs_the_error(self):
        slack = SandboxSlack(self.rt.db, fail_times=5, sleep=no_sleep)
        with self.assertRaises(TransientError):
            slack.post_message("#reviews", "x")
        [call] = self.calls("slack")
        self.assertEqual((call["status"], call["attempts"]), ("error", 3))
        self.assertIn("503", call["response_json"])

    def test_logged_payloads_are_redacted(self):
        SandboxGmail(self.rt.db).create_draft("jane.doe@gmail.com", "Re: x", "Call me at (555) 201-3344", None)
        row = self.calls("gmail")[0]["request_json"]
        self.assertNotIn("jane.doe@gmail.com", row)
        self.assertNotIn("201-3344", row)


class LiveAdapterTest(RuntimeTestCase):
    """The real API clients, with httpx.MockTransport standing in for the network."""

    seed = False

    def client(self, handler):
        self.requests = []

        def record(request: httpx.Request):
            self.requests.append(request)
            return handler(request)
        return httpx.Client(transport=httpx.MockTransport(record))

    def test_slack_bot_token(self):
        c = self.client(lambda r: httpx.Response(200, json={"ok": True, "channel": "C123", "ts": "171.1"}))
        res = LiveSlack(self.rt.db, bot_token="xoxb-1", client=c).post_message("#ops-daily", "*hi*")
        req = self.requests[0]
        self.assertEqual(str(req.url), "https://slack.com/api/chat.postMessage")
        self.assertEqual(req.headers["Authorization"], "Bearer xoxb-1")
        self.assertEqual(json.loads(req.content)["channel"], "#ops-daily")
        self.assertEqual(res["ts"], "171.1")

    def test_slack_api_error_is_not_retried(self):
        c = self.client(lambda r: httpx.Response(200, json={"ok": False, "error": "channel_not_found"}))
        with self.assertRaises(IntegrationError):
            LiveSlack(self.rt.db, bot_token="xoxb-1", client=c, sleep=no_sleep).post_message("#nope", "x")
        self.assertEqual(len(self.requests), 1)

    def test_slack_rate_limit_is_retried(self):
        responses = iter([httpx.Response(429), httpx.Response(200, json={"ok": True, "ts": "1"})])
        c = self.client(lambda r: next(responses))
        LiveSlack(self.rt.db, bot_token="xoxb-1", client=c, sleep=no_sleep).post_message("#ops-daily", "x")
        self.assertEqual(len(self.requests), 2)

    def test_slack_webhook(self):
        c = self.client(lambda r: httpx.Response(200, text="ok"))
        res = LiveSlack(self.rt.db, webhook_url="https://hooks.slack.com/services/T/B/X", client=c).post_message("#x", "hi")
        self.assertEqual(json.loads(self.requests[0].content), {"text": "hi"})
        self.assertEqual(res["via"], "webhook")

    def test_clickup_create_task(self):
        c = self.client(lambda r: httpx.Response(200, json={"id": "86abc", "url": "https://app.clickup.com/t/86abc",
                                                            "status": {"status": "to do"}}))
        res = LiveClickUp(self.rt.db, token="pk_42", list_id="901", client=c).create_task("Name", "Desc", "urgent", ["a"])
        req = self.requests[0]
        self.assertEqual(str(req.url), "https://api.clickup.com/api/v2/list/901/task")
        self.assertEqual(req.headers["Authorization"], "pk_42")
        self.assertEqual(json.loads(req.content)["priority"], 1)
        self.assertEqual(res, {"id": "86abc", "url": "https://app.clickup.com/t/86abc", "status": "to do"})

    def test_clickup_auth_error(self):
        c = self.client(lambda r: httpx.Response(401, json={"err": "Token invalid"}))
        with self.assertRaises(IntegrationError):
            LiveClickUp(self.rt.db, token="bad", list_id="1", client=c, sleep=no_sleep).create_task("n", "d", "low", [])
        self.assertEqual(len(self.requests), 1, "4xx errors must not be retried")

    def test_gmail_draft_and_send(self):
        def handler(r):
            if r.url.path.endswith("/drafts"):
                return httpx.Response(200, json={"id": "r-777", "message": {"id": "m1", "threadId": "thr-9"}})
            return httpx.Response(200, json={"id": "m2", "labelIds": ["SENT"]})
        gm = LiveGmail(self.rt.db, access_token="ya29.x", send_as="support@northpeak.example", client=self.client(handler))
        draft = gm.create_draft("buyer@example.com", "Where is my order?", "Hello there", "thr-9")
        self.assertEqual(draft, {"draft_id": "r-777", "thread_id": "thr-9"})
        body = json.loads(self.requests[0].content)
        self.assertEqual(body["message"]["threadId"], "thr-9")
        mime = message_from_bytes(base64.urlsafe_b64decode(body["message"]["raw"]))
        self.assertEqual(mime["To"], "buyer@example.com")
        self.assertEqual(mime["Subject"], "Re: Where is my order?")
        self.assertEqual(mime.get_payload().strip(), "Hello there")
        sent = gm.send_draft("r-777")
        self.assertEqual(json.loads(self.requests[1].content), {"id": "r-777"})
        self.assertEqual(sent["label_ids"], ["SENT"])

    def test_network_errors_are_transient(self):
        def handler(r):
            raise httpx.ConnectError("boom", request=r)
        with self.assertRaises(TransientError):
            LiveGmail(self.rt.db, access_token="t", client=self.client(handler), sleep=no_sleep).send_draft("r-1")
        self.assertEqual(len(self.requests), 3)

    def test_missing_credentials(self):
        with self.assertRaises(IntegrationError):
            LiveSlack(self.rt.db)
        with self.assertRaises(IntegrationError):
            LiveClickUp(self.rt.db, token="x", list_id="")


if __name__ == "__main__":
    unittest.main()
