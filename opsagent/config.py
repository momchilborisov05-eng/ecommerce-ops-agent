"""
Settings, read from environment variables (and a .env file, loaded by main.py).

Mode:
- "demo": a scripted LLM, no API key needed, no network calls.
- "live": Claude through the Anthropic API.
"auto" (the default) picks live when ANTHROPIC_API_KEY is set, otherwise demo.

Each integration (Slack, ClickUp, Gmail) uses its real API only when its
credentials are set; otherwise it uses the offline sandbox adapter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_DIR / "ops.db"
DEFAULT_STORE_DATE = "2026-10-28"   # the seed data is built around this date, so demo output is repeatable

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


@dataclass(frozen=True)
class Settings:
    db_path: Path = DEFAULT_DB
    mode: str = "demo"
    model: str = MODEL
    store_date: date = field(default_factory=lambda: date.fromisoformat(DEFAULT_STORE_DATE))
    slack_bot_token: str = ""
    slack_webhook_url: str = ""
    clickup_token: str = ""
    clickup_list_id: str = ""
    gmail_access_token: str = ""
    gmail_send_as: str = ""

    @classmethod
    def from_env(cls, mode: str | None = None, db_path: Path | str | None = None) -> "Settings":
        requested = (mode or _env("OPS_AGENT_MODE") or "auto").lower()
        if requested not in {"auto", "demo", "live"}:
            raise ValueError(f"OPS_AGENT_MODE must be auto, demo or live, not {requested!r}.")
        if requested == "auto":
            requested = "live" if _env("ANTHROPIC_API_KEY") else "demo"
        return cls(
            db_path=Path(db_path or _env("OPS_AGENT_DB") or DEFAULT_DB),
            mode=requested,
            model=_env("CLAUDE_MODEL") or MODEL,
            store_date=date.fromisoformat(_env("OPS_STORE_DATE") or DEFAULT_STORE_DATE),
            slack_bot_token=_env("SLACK_BOT_TOKEN"),
            slack_webhook_url=_env("SLACK_WEBHOOK_URL"),
            clickup_token=_env("CLICKUP_API_TOKEN"),
            clickup_list_id=_env("CLICKUP_LIST_ID"),
            gmail_access_token=_env("GMAIL_ACCESS_TOKEN"),
            gmail_send_as=_env("GMAIL_SEND_AS"),
        )

    @property
    def slack_live(self) -> bool:
        return bool(self.slack_bot_token or self.slack_webhook_url)

    @property
    def clickup_live(self) -> bool:
        return bool(self.clickup_token and self.clickup_list_id)

    @property
    def gmail_live(self) -> bool:
        return bool(self.gmail_access_token)
