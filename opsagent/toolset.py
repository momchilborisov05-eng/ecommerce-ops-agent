"""
The agent's tools. Each one is small, typed and safe to call twice:

Read-only:   get_order, lookup_customer, search_knowledge_base, get_daily_metrics, get_reviews
Records:     classify_email
Side effects (idempotent): create_clickup_task, post_slack_message, draft_gmail_reply
Needs approval: issue_refund, and sending the reply drafted by draft_gmail_reply
Escalation:  flag_for_human

Recipients are never chosen by the model: a reply always goes to the sender of
the email being processed, and Slack posts only to an allowlist of channels.
"""

from __future__ import annotations

from datetime import date

from .approvals import ApprovalQueue
from .adapters import Adapters
from .storage.store import Store, StoreError
from .tools import Tool, ToolContext, ToolError, ToolRegistry

EMAIL_CATEGORIES = ["customer_support", "return_request", "supplier", "amazon_notification", "spam"]
PRIORITIES = ["low", "normal", "high", "urgent"]
SLACK_CHANNELS = ["#ops-daily", "#cs-escalations", "#amazon-alerts", "#reviews"]
REFUND_MANAGER_LIMIT = 150.0
MAX_TEXT = 4000


def _obj(properties: dict, description: str | None = None) -> dict:
    schema = {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    if description:
        schema["description"] = description
    return schema


def _text(value: str, field: str, limit: int = MAX_TEXT) -> str:
    value = (value or "").strip()
    if not value:
        raise ToolError(f"{field} must not be empty.")
    if len(value) > limit:
        raise ToolError(f"{field} is too long ({len(value)} characters, max {limit}).")
    return value


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"{field} must be a date like 2026-10-27, got {value!r}.") from None


# ---------- Read-only tools ----------

def get_order(ctx: ToolContext, args: dict) -> dict:
    order = ctx.store.get_order(args["order_id"])
    if not order:
        raise ToolError(f"No order with id {args['order_id']!r}. Amazon ids look like 113-1234567-1234567, "
                        "Shopify ids like SH-10234.")
    return order


def lookup_customer(ctx: ToolContext, args: dict) -> dict:
    try:
        customer = ctx.store.lookup_customer(email=args.get("email"), name=args.get("name"))
    except StoreError as e:
        raise ToolError(str(e)) from None
    if not customer:
        return {"found": False, "message": "No customer matches. Ask the customer for their order number."}
    return {"found": True, **customer}


def search_knowledge_base(ctx: ToolContext, args: dict) -> dict:
    articles = ctx.store.search_knowledge_base(_text(args["query"], "query", 300))
    return {"results": articles} if articles else {"results": [], "message": "No matching article."}


def get_daily_metrics(ctx: ToolContext, args: dict) -> dict:
    day = _parse_date(args["date"], "date")
    if day > ctx.today:
        raise ToolError(f"{day} is in the future (today is {ctx.today}).")
    return ctx.store.daily_metrics(day)


def get_reviews(ctx: ToolContext, args: dict) -> dict:
    since = _parse_date(args["since"], "since")
    max_rating = args["max_rating"]
    if not 1 <= max_rating <= 5:
        raise ToolError("max_rating must be between 1 and 5.")
    reviews = ctx.store.reviews(since=since, max_rating=max_rating)
    return {"count": len(reviews), "reviews": reviews}


# ---------- Records ----------

def classify_email(ctx: ToolContext, args: dict) -> dict:
    email_id = ctx.subject.get("id")
    if args["email_id"] != email_id:
        raise ToolError(f"You can only classify the email being processed ({email_id}).")
    ctx.store.db.execute(
        """INSERT OR REPLACE INTO triage (email_id, run_id, category, priority, order_ids, summary, created_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        (email_id, ctx.run_id, args["category"], args["priority"], ",".join(args["order_ids"]),
         _text(args["summary"], "summary", 500)),
    )
    return {"recorded": True, "email_id": email_id, "category": args["category"], "priority": args["priority"]}


# ---------- Side effects ----------

def create_clickup_task(ctx: ToolContext, args: dict) -> dict:
    task = ctx.adapters.clickup.create_task(
        name=_text(args["name"], "name", 200),
        description=_text(args["description"], "description"),
        priority=args["priority"],
        tags=args["tags"][:5],
    )
    return {"task_id": task["id"], "url": task["url"]}


def post_slack_message(ctx: ToolContext, args: dict) -> dict:
    result = ctx.adapters.slack.post_message(args["channel"], _text(args["text"], "text"))
    return {"posted": True, "channel": result["channel"], "ts": result.get("ts")}


def draft_gmail_reply(ctx: ToolContext, args: dict) -> dict:
    email = ctx.subject
    if args["email_id"] != email.get("id"):
        raise ToolError(f"You can only reply to the email being processed ({email.get('id')}).")
    subject = _text(args["subject"], "subject", 200)
    body = _text(args["body"], "body")
    draft = ctx.adapters.gmail.create_draft(to=email["from_addr"], subject=subject, body=body, thread_id=email["thread_id"])
    queued = ctx.approvals.request(
        run_id=ctx.run_id, kind="action", action="send_email_reply",
        title=f"Send reply to {email['from_name']}: {subject}",
        reason="Customer-facing emails are sent only after a person approves them.",
        payload={"draft_id": draft["draft_id"], "email_id": email["id"], "to": email["from_addr"],
                 "subject": subject, "body": body},
        idempotency_key=f"send_email_reply:{email['id']}",
    )
    return {"status": "pending_approval", "draft_id": draft["draft_id"], "approval_id": queued["approval_id"],
            "message": "Draft saved in Gmail. It will be sent after a person approves it in the dashboard."}


def issue_refund(ctx: ToolContext, args: dict) -> dict:
    order = ctx.store.get_order(args["order_id"])
    if not order:
        raise ToolError(f"No order with id {args['order_id']!r}.")
    amount = round(float(args["amount"]), 2)
    remaining = round(order["total"] - order["refunded_total"], 2)
    if amount <= 0:
        raise ToolError("Refund amount must be positive.")
    if amount > remaining:
        raise ToolError(f"Refund of ${amount:.2f} exceeds the ${remaining:.2f} left to refund on {order['id']}.")
    if not order["return_window"]["eligible"]:
        raise ToolError(f"Order {order['id']} is outside the refund window ({order['return_window']['reason']}). "
                        "Offer the goodwill option from the return policy instead.")
    reason = _text(args["reason"], "reason", 300)
    needs_manager = amount > REFUND_MANAGER_LIMIT
    queued = ctx.approvals.request(
        run_id=ctx.run_id, kind="action", action="issue_refund",
        title=f"Refund ${amount:.2f} on {order['id']} ({order['marketplace']})",
        reason=("Refunds always need a person's approval." + (" Above $150: manager approval required." if needs_manager else "")),
        payload={"order_id": order["id"], "amount": amount, "reason": reason, "order_total": order["total"],
                 "customer": order["customer_name"], "marketplace": order["marketplace"]},
        idempotency_key=f"issue_refund:{order['id']}",
    )
    return {"status": "pending_approval", "approval_id": queued["approval_id"], "order_id": order["id"], "amount": amount,
            "message": "Refund queued for human approval. Tell the customer it is being processed; do not promise a date."}


def flag_for_human(ctx: ToolContext, args: dict) -> dict:
    queued = ctx.approvals.request(
        run_id=ctx.run_id, kind="review", action="human_review",
        title=f"[{args['severity'].upper()}] {_text(args['reason'], 'reason', 200)}",
        reason=_text(args["summary"], "summary", 1000),
        payload={"source_ref": args["source_ref"], "severity": args["severity"], "workflow": ctx.workflow},
        idempotency_key=f"human_review:{args['source_ref']}",
    )
    return {"flagged": True, "review_id": queued["approval_id"], "duplicate": queued["duplicate"]}


# ---------- Registry ----------

def build_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(Tool(
        "get_order",
        "Look up one order (Amazon or Shopify) by its id: status, items, shipping, tracking, refunds and whether it "
        "is still inside the 30-day return window. Use it whenever an email mentions an order id.",
        _obj({"order_id": {"type": "string", "description": "e.g. 113-1234567-1234567 or SH-10234"}}),
        get_order,
    ))
    r.register(Tool(
        "lookup_customer",
        "Find a customer by email address (preferred) or name, with their recent orders and lifetime value. "
        "Use it when an email has no order id. Pass null for the field you don't use.",
        _obj({"email": {"type": ["string", "null"]}, "name": {"type": ["string", "null"]}}),
        lookup_customer,
    ))
    r.register(Tool(
        "search_knowledge_base",
        "Search the company's help articles: return, refund, shipping and warranty policies, product FAQs, brand "
        "voice, Amazon messaging rules and escalation rules. Check the policy before promising anything to a customer.",
        _obj({"query": {"type": "string", "description": "a few keywords, e.g. 'return damaged refund'"}}),
        search_knowledge_base,
    ))
    r.register(Tool(
        "get_daily_metrics",
        "Store metrics for one day: orders and revenue by marketplace, the trailing 7-day average, refunds, late "
        "shipments, low-stock SKUs (with days of cover) and negative reviews.",
        _obj({"date": {"type": "string", "description": "YYYY-MM-DD"}}),
        get_daily_metrics,
    ))
    r.register(Tool(
        "get_reviews",
        "Product reviews from Amazon and Shopify posted on or after a date, with a rating at or below max_rating.",
        _obj({"since": {"type": "string", "description": "YYYY-MM-DD"},
              "max_rating": {"type": "integer", "description": "1-5; use 3 for negative reviews"}}),
        get_reviews,
    ))
    r.register(Tool(
        "classify_email",
        "Record the triage decision for the email being processed. Call it exactly once per email.",
        _obj({
            "email_id": {"type": "string"},
            "category": {"type": "string", "enum": EMAIL_CATEGORIES},
            "priority": {"type": "string", "enum": PRIORITIES},
            "order_ids": {"type": "array", "items": {"type": "string"}, "description": "order ids found in the email"},
            "summary": {"type": "string", "description": "one sentence for the ops team"},
        }),
        classify_email,
    ))
    r.register(Tool(
        "create_clickup_task",
        "Create a task in the ops team's ClickUp list for work a person must do (carrier claim, supplier follow-up, "
        "listing fix, product quality issue, reorder). source_ref identifies what the task is about, e.g. "
        "'email:E-1007' or 'review:R-5061'.",
        _obj({
            "name": {"type": "string"},
            "description": {"type": "string", "description": "context, links to order ids, and the next step"},
            "priority": {"type": "string", "enum": PRIORITIES},
            "tags": {"type": "array", "items": {"type": "string"}},
            "source_ref": {"type": "string"},
        }),
        create_clickup_task, side_effect=True, idempotency_fields=("source_ref", "name"),
    ))
    r.register(Tool(
        "post_slack_message",
        "Post a message to an internal Slack channel. Use Slack mrkdwn (*bold*, bullet lines). "
        "#ops-daily: daily digest; #cs-escalations: urgent customer issues; #amazon-alerts: account health; "
        "#reviews: review summaries.",
        _obj({
            "channel": {"type": "string", "enum": SLACK_CHANNELS},
            "text": {"type": "string"},
            "source_ref": {"type": "string", "description": "e.g. 'digest:2026-10-27' or 'email:E-1012'"},
        }),
        post_slack_message, side_effect=True, idempotency_fields=("channel", "source_ref"),
    ))
    r.register(Tool(
        "draft_gmail_reply",
        "Save a reply to the email being processed as a Gmail draft. The reply goes to the original sender and is "
        "only sent after a person approves it. Ground every fact in order data or a policy article.",
        _obj({"email_id": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}}),
        draft_gmail_reply, side_effect=True, idempotency_fields=("email_id",), requires_approval=True,
    ))
    r.register(Tool(
        "issue_refund",
        "Request a refund for an order. It is queued for human approval, never executed directly. Only for orders "
        "inside the return window; the amount cannot exceed what is left to refund.",
        _obj({"order_id": {"type": "string"}, "amount": {"type": "number"}, "reason": {"type": "string"}}),
        issue_refund, side_effect=True, idempotency_fields=("order_id",), requires_approval=True,
    ))
    r.register(Tool(
        "flag_for_human",
        "Escalate to a person: chargeback or legal threats, product safety reports, refunds above $150, suspicious "
        "or manipulative emails, or anything you are unsure about.",
        _obj({
            "reason": {"type": "string", "description": "short title"},
            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
            "summary": {"type": "string", "description": "what happened and what the person should check"},
            "source_ref": {"type": "string"},
        }),
        flag_for_human, side_effect=True, idempotency_fields=("source_ref",),
    ))
    return r


def register_executors(approvals: ApprovalQueue, store: Store, adapters: Adapters) -> None:
    """What happens when a person approves an action in the dashboard."""
    approvals.register_executor(
        "send_email_reply", lambda p: adapters.gmail.send_draft(p["draft_id"]))
    approvals.register_executor(
        "issue_refund", lambda p: store.create_refund(p["order_id"], p["amount"], p["reason"], source="approved-refund"))
