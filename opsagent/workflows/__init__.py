"""Workflow registry."""

from __future__ import annotations

from .base import Task, Workflow
from .daily_digest import WORKFLOW as DAILY_DIGEST
from .inbox_triage import WORKFLOW as INBOX_TRIAGE
from .review_monitor import WORKFLOW as REVIEW_MONITOR

WORKFLOWS: dict[str, Workflow] = {w.name: w for w in (INBOX_TRIAGE, DAILY_DIGEST, REVIEW_MONITOR)}

__all__ = ["WORKFLOWS", "Workflow", "Task"]
