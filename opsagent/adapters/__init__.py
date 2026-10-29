"""Builds the adapter set: live adapters where credentials exist, sandbox adapters everywhere else."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..storage.db import Database
from .base import ClickUpAdapter, GmailAdapter, IntegrationError, SlackAdapter
from .live import LiveClickUp, LiveGmail, LiveSlack
from .sandbox import SandboxClickUp, SandboxGmail, SandboxSlack

__all__ = ["Adapters", "build_adapters", "IntegrationError"]


@dataclass
class Adapters:
    slack: SlackAdapter
    clickup: ClickUpAdapter
    gmail: GmailAdapter

    def backends(self) -> dict[str, str]:
        return {"slack": self.slack.backend, "clickup": self.clickup.backend, "gmail": self.gmail.backend}


def build_adapters(settings: Settings, db: Database) -> Adapters:
    return Adapters(
        slack=LiveSlack(db, settings.slack_bot_token, settings.slack_webhook_url) if settings.slack_live else SandboxSlack(db),
        clickup=LiveClickUp(db, settings.clickup_token, settings.clickup_list_id) if settings.clickup_live else SandboxClickUp(db),
        gmail=LiveGmail(db, settings.gmail_access_token, settings.gmail_send_as) if settings.gmail_live else SandboxGmail(db),
    )
