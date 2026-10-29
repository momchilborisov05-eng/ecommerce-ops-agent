"""
Wires everything together: settings -> database, store, adapters, tool registry,
approval queue, audit recorder and LLM client. The CLI, the web app and the tests
all build a Runtime and call run_workflow().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .adapters import Adapters, build_adapters
from .agent import Agent, RunResult
from .approvals import ApprovalQueue
from .audit import RunRecorder, current_run_id
from .config import Settings
from .llm import ClaudeLLM, LLMClient, ScriptedLLM
from .storage.db import Database
from .storage.seed import seed
from .storage.store import Store
from .tools import ToolContext, ToolRegistry
from .toolset import build_registry, register_executors
from .workflows import WORKFLOWS, Workflow

log = logging.getLogger("opsagent")


@dataclass
class Runtime:
    settings: Settings
    db: Database
    store: Store
    adapters: Adapters
    registry: ToolRegistry
    recorder: RunRecorder
    approvals: ApprovalQueue
    llm_override: LLMClient | None = None      # tests can inject a fake Claude client
    _claude: ClaudeLLM | None = field(default=None, repr=False)

    @classmethod
    def build(cls, settings: Settings, adapters: Adapters | None = None, llm: LLMClient | None = None) -> "Runtime":
        db = Database(settings.db_path)
        db.init_schema()
        store = Store(db, settings.store_date)
        adapters = adapters or build_adapters(settings, db)
        recorder = RunRecorder(db)
        approvals = ApprovalQueue(db, recorder)
        register_executors(approvals, store, adapters)
        return cls(settings, db, store, adapters, build_registry(), recorder, approvals, llm_override=llm)

    def seed(self) -> dict:
        return seed(self.db, self.settings.store_date)

    def is_seeded(self) -> bool:
        return bool(self.db.query_one("SELECT 1 FROM sqlite_master WHERE name = 'orders'")) and \
            bool(self.db.query_one("SELECT 1 FROM orders LIMIT 1"))

    def llm_for(self, workflow: Workflow) -> LLMClient:
        if self.llm_override is not None:
            return self.llm_override
        if self.settings.mode == "demo":
            return ScriptedLLM(workflow.demo_brain)
        if self._claude is None:
            def on_retry(attempt: int, error: Exception, delay: float) -> None:
                run_id = current_run_id.get()
                if run_id:
                    self.recorder.step(run_id, "retry", "llm", status="error",
                                       output={"attempt": attempt, "error": str(error), "retry_in_s": round(delay, 2)})
            self._claude = ClaudeLLM(model=self.settings.model, on_retry=on_retry)
        return self._claude

    def run_workflow(self, name: str, limit: int | None = None) -> list[RunResult]:
        workflow = WORKFLOWS.get(name)
        if workflow is None:
            raise KeyError(f"Unknown workflow {name!r}. Choose one of: {', '.join(WORKFLOWS)}")
        if not self.is_seeded():
            raise RuntimeError("The store is empty. Run: python main.py seed")
        agent = Agent(self.llm_for(workflow), self.registry, self.recorder, workflow.policy, workflow.system_prompt)
        results = []
        for task in workflow.build_tasks(self.store, self.settings.store_date, limit):
            ctx = ToolContext(run_id=None, workflow=workflow.name, store=self.store, adapters=self.adapters,
                              approvals=self.approvals, today=self.settings.store_date, subject=task.data)
            try:
                result = agent.run(ctx, task.prompt, task.subject)
            except Exception:   # one bad email must not stop the batch; the run is already marked failed
                log.exception("Run for %s crashed", task.subject)
                continue
            if workflow.name == "inbox-triage" and result.status == "completed":
                if not self.db.query_one("SELECT 1 FROM triage WHERE email_id = ?", (task.data["id"],)):
                    self.recorder.step(result.run_id, "guardrail", "classification_missing", status="blocked",
                                       output={"error": "The agent finished without calling classify_email."})
                    self.db.execute("UPDATE runs SET status = 'incomplete' WHERE id = ?", (result.run_id,))
                    result.status = "incomplete"
            results.append(result)
        return results
