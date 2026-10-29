"""Dashboard API endpoints (FastAPI TestClient, no server, no network)."""

import unittest

from fastapi.testclient import TestClient

from opsagent.web.app import create_app

from helpers import RuntimeTestCase


class ApiTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.rt))

    def run_inbox(self, limit=12):
        res = self.client.post("/api/workflows/inbox-triage/run", json={"limit": limit})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["runs"]

    def test_health_and_static_pages(self):
        self.assertEqual(self.client.get("/api/health").json(), {"ok": True, "mode": "demo", "seeded": True})
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Ops Agent", page.text)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)

    def test_workflows_list(self):
        names = [w["name"] for w in self.client.get("/api/workflows").json()]
        self.assertEqual(names, ["inbox-triage", "daily-digest", "review-monitor"])

    def test_run_workflow_and_overview(self):
        runs = self.run_inbox(limit=5)
        self.assertEqual(len(runs), 5)
        self.client.post("/api/workflows/daily-digest/run")
        o = self.client.get("/api/overview").json()
        self.assertEqual(o["mode"], "demo")
        self.assertEqual(o["totals"]["runs"], 6)
        self.assertEqual({w["workflow"] for w in o["by_workflow"]}, {"inbox-triage", "daily-digest"})
        self.assertEqual(sum(t["n"] for t in o["triage"]), 5)

    def test_unknown_workflow(self):
        self.assertEqual(self.client.post("/api/workflows/nope/run").status_code, 404)

    def test_runs_and_trace(self):
        runs = self.run_inbox(limit=3)
        listed = self.client.get("/api/runs?workflow=inbox-triage").json()
        self.assertEqual(len(listed), 3)
        detail = self.client.get(f"/api/runs/{runs[0]['run_id']}").json()
        self.assertEqual(detail["steps"][0]["kind"], "llm")
        self.assertIsInstance(detail["steps"][0]["output"], dict)
        self.assertIn("integration_calls", detail)
        self.assertEqual(self.client.get("/api/runs/99999").status_code, 404)

    def test_approve_and_reject(self):
        self.run_inbox()
        pending = self.client.get("/api/approvals").json()
        self.assertTrue(pending)
        self.assertTrue(all(a["status"] == "pending" for a in pending))
        first, second = pending[0]["id"], pending[1]["id"]
        ok = self.client.post(f"/api/approvals/{first}/approve", json={"note": "fine"})
        self.assertEqual(ok.status_code, 200)
        self.assertIn(ok.json()["status"], ("executed", "resolved"))
        self.assertEqual(self.client.post(f"/api/approvals/{first}/approve").status_code, 409)
        self.assertEqual(self.client.post(f"/api/approvals/{second}/reject", json={"note": "no"}).json()["status"], "rejected")
        self.assertEqual(self.client.post("/api/approvals/99999/approve").status_code, 404)
        self.assertEqual(len(self.client.get("/api/approvals?status=all").json()), len(pending))
        self.assertEqual(self.client.get("/api/approvals?status=bogus").status_code, 422)

    def test_approvals_are_redacted(self):
        self.run_inbox(limit=40)
        for a in self.client.get("/api/approvals?status=all").json():
            to = a["payload"].get("to")
            if to:
                self.assertIn("***@", to)

    def test_integration_log(self):
        self.run_inbox()
        calls = self.client.get("/api/integrations").json()
        self.assertTrue(calls)
        self.assertTrue(all(c["backend"] == "sandbox" for c in calls))
        slack = self.client.get("/api/integrations?integration=slack").json()
        self.assertTrue(all(c["integration"] == "slack" for c in slack))
        self.assertIsInstance(calls[0]["request"], dict)


if __name__ == "__main__":
    unittest.main()
