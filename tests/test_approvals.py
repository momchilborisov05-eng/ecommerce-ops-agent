"""Human-in-the-loop approval queue."""

import unittest

from opsagent.approvals import ApprovalError

from helpers import RuntimeTestCase


class ApprovalQueueTest(RuntimeTestCase):
    def refund_request(self, amount=10.0, key="issue_refund:test"):
        order = self.rt.db.query_one("""SELECT o.id, o.total FROM orders o WHERE o.status = 'delivered'
                                        AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id) LIMIT 1""")
        q = self.rt.approvals.request(run_id=None, kind="action", action="issue_refund", title="Refund", reason="test",
                                      payload={"order_id": order["id"], "amount": amount, "reason": "damaged"},
                                      idempotency_key=key)
        return order, q

    def test_same_key_returns_the_existing_request(self):
        _, a = self.refund_request()
        _, b = self.refund_request()
        self.assertFalse(a["duplicate"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(a["approval_id"], b["approval_id"])
        self.assertEqual(len(self.rt.approvals.list("pending")), 1)

    def test_approve_executes_the_refund(self):
        order, q = self.refund_request(amount=10.0)
        item = self.rt.approvals.approve(q["approval_id"], "tester", "looks right")
        self.assertEqual(item["status"], "executed")
        self.assertEqual(item["decided_by"], "tester")
        refund = self.rt.db.query_one("SELECT * FROM refunds WHERE order_id = ?", (order["id"],))
        self.assertEqual(refund["amount"], 10.0)
        self.assertEqual(item["result"]["refund_id"], refund["id"])

    def test_reject_does_nothing(self):
        order, q = self.refund_request()
        self.assertEqual(self.rt.approvals.reject(q["approval_id"], "tester", "no")["status"], "rejected")
        self.assertIsNone(self.rt.db.query_one("SELECT 1 FROM refunds WHERE order_id = ?", (order["id"],)))

    def test_cannot_decide_twice(self):
        _, q = self.refund_request()
        self.rt.approvals.reject(q["approval_id"])
        with self.assertRaises(ApprovalError):
            self.rt.approvals.approve(q["approval_id"])
        with self.assertRaises(ApprovalError):
            self.rt.approvals.approve(99999)

    def test_failed_execution_is_recorded(self):
        order, q = self.refund_request(amount=100000.0)   # more than the order total
        item = self.rt.approvals.approve(q["approval_id"])
        self.assertEqual(item["status"], "failed")
        self.assertIn("not allowed", item["result"]["error"])

    def test_review_items_are_resolved(self):
        q = self.rt.approvals.request(run_id=None, kind="review", action="human_review", title="Check", reason="r",
                                      payload={}, idempotency_key="human_review:x")
        self.assertEqual(self.rt.approvals.approve(q["approval_id"])["status"], "resolved")

    def test_approved_email_is_sent_through_gmail(self):
        self.rt.run_workflow("inbox-triage", limit=40)
        reply = next(a for a in self.rt.approvals.list("pending") if a["action"] == "send_email_reply")
        item = self.rt.approvals.approve(reply["id"])
        self.assertEqual(item["status"], "executed")
        self.assertEqual(item["result"]["draft_id"], reply["payload"]["draft_id"])
        sent = self.rt.db.query_one("SELECT run_id FROM integration_calls WHERE operation = 'drafts.send'")
        self.assertEqual(sent["run_id"], reply["run_id"], "the send is logged under the run that drafted it")
        step = self.rt.db.query_one("SELECT * FROM steps WHERE run_id = ? AND kind = 'approval'", (reply["run_id"],))
        self.assertEqual(step["name"], "send_email_reply executed")


if __name__ == "__main__":
    unittest.main()
