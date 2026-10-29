"""
Live adapters for the real APIs. They are only created when credentials are set
(see adapters/__init__.py). The HTTP client is injectable, so the tests run
them against httpx.MockTransport without any network access.

- Slack:   chat.postMessage with a bot token (xoxb-...), or an incoming webhook URL
- ClickUp: API v2, POST /list/{list_id}/task with a personal API token
- Gmail:   API v1, users.drafts.create and users.drafts.send with an OAuth access token
           (scope https://www.googleapis.com/auth/gmail.compose)
"""

from __future__ import annotations

import base64
from email.message import EmailMessage

import httpx

from ..retry import TransientError
from .base import IntegrationAdapter, IntegrationError, http_call

TIMEOUT = httpx.Timeout(15.0, connect=5.0)
CLICKUP_PRIORITY = {"urgent": 1, "high": 2, "normal": 3, "low": 4}


class LiveSlack(IntegrationAdapter):
    name = "slack"
    backend = "live"
    API = "https://slack.com/api/chat.postMessage"

    def __init__(self, db, bot_token: str = "", webhook_url: str = "", client: httpx.Client | None = None, **kwargs):
        super().__init__(db, **kwargs)
        if not (bot_token or webhook_url):
            raise IntegrationError("Slack needs SLACK_BOT_TOKEN or SLACK_WEBHOOK_URL.")
        self.bot_token, self.webhook_url = bot_token, webhook_url
        self.client = client or httpx.Client(timeout=TIMEOUT)

    def post_message(self, channel: str, text: str) -> dict:
        def send() -> dict:
            if self.bot_token:
                data = http_call(lambda: self.client.post(
                    self.API, headers={"Authorization": f"Bearer {self.bot_token}"},
                    json={"channel": channel, "text": text, "unfurl_links": False},
                ))
                if not data.get("ok"):
                    err = data.get("error", "unknown_error")
                    if err in ("ratelimited", "service_unavailable", "internal_error"):
                        raise TransientError(f"Slack error: {err}")
                    raise IntegrationError(f"Slack error: {err}")
                return {"ok": True, "channel": data.get("channel", channel), "ts": data.get("ts")}
            # Incoming webhooks post to the channel chosen when the webhook was created.
            http_call(lambda: self.client.post(self.webhook_url, json={"text": text}))
            return {"ok": True, "channel": channel, "ts": None, "via": "webhook"}

        return self._call("chat.postMessage", {"channel": channel, "text": text}, send)


class LiveClickUp(IntegrationAdapter):
    name = "clickup"
    backend = "live"
    API = "https://api.clickup.com/api/v2"

    def __init__(self, db, token: str, list_id: str, client: httpx.Client | None = None, **kwargs):
        super().__init__(db, **kwargs)
        if not (token and list_id):
            raise IntegrationError("ClickUp needs CLICKUP_API_TOKEN and CLICKUP_LIST_ID.")
        self.token, self.list_id = token, list_id
        self.client = client or httpx.Client(timeout=TIMEOUT)

    def create_task(self, name: str, description: str, priority: str, tags: list[str]) -> dict:
        body = {"name": name, "description": description, "priority": CLICKUP_PRIORITY.get(priority, 3), "tags": tags}

        def create() -> dict:
            data = http_call(lambda: self.client.post(
                f"{self.API}/list/{self.list_id}/task", headers={"Authorization": self.token}, json=body,
            ))
            return {"id": data.get("id"), "url": data.get("url"), "status": (data.get("status") or {}).get("status")}

        return self._call("task.create", {"name": name, "description": description, "priority": priority, "tags": tags}, create)


class LiveGmail(IntegrationAdapter):
    name = "gmail"
    backend = "live"
    API = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(self, db, access_token: str, send_as: str = "", client: httpx.Client | None = None, **kwargs):
        super().__init__(db, **kwargs)
        if not access_token:
            raise IntegrationError("Gmail needs GMAIL_ACCESS_TOKEN.")
        self.access_token, self.send_as = access_token, send_as
        self.client = client or httpx.Client(timeout=TIMEOUT)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}"}

    def create_draft(self, to: str, subject: str, body: str, thread_id: str | None) -> dict:
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        if self.send_as:
            msg["From"] = self.send_as
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload: dict = {"message": {"raw": raw}}
        if thread_id:
            payload["message"]["threadId"] = thread_id

        def create() -> dict:
            data = http_call(lambda: self.client.post(f"{self.API}/drafts", headers=self._headers(), json=payload))
            return {"draft_id": data.get("id"), "thread_id": (data.get("message") or {}).get("threadId")}

        return self._call("drafts.create", {"to": to, "subject": subject, "body": body, "thread_id": thread_id}, create)

    def send_draft(self, draft_id: str) -> dict:
        def send() -> dict:
            data = http_call(lambda: self.client.post(f"{self.API}/drafts/send", headers=self._headers(), json={"id": draft_id}))
            return {"message_id": data.get("id"), "draft_id": draft_id, "label_ids": data.get("labelIds", [])}

        return self._call("drafts.send", {"draft_id": draft_id}, send)
