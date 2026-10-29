"""Daily ops digest: yesterday's orders, refunds, late shipments, low stock and bad reviews, posted to Slack."""

from __future__ import annotations

from datetime import date, timedelta

from ..guardrails import Policy
from ..llm import Turn
from ..storage.store import Store
from .base import Task, Workflow, text, tool_use, wrap

SYSTEM_PROMPT = """\
You write the daily operations digest for Northpeak Goods, a brand selling on Amazon and Shopify. The CEO and the \
ops team read it in Slack before 9am, so it has to be short, accurate and actionable.

Steps:
1. Call get_daily_metrics for the date you are given.
2. Post exactly one message to #ops-daily with post_slack_message (source_ref "digest:<date>"), in Slack mrkdwn, \
at most about 15 lines:
   - orders and revenue, split by marketplace, compared with the trailing 7-day average (as a percentage);
   - refunds (count and total) and late shipments;
   - low-stock SKUs with units left and days of cover, most urgent first;
   - negative reviews from that day, one line each.
3. If any SKU has less than 7 days of cover, create one ClickUp task to reorder those SKUs (source_ref \
"reorder:<date>", priority high), listing each SKU with its stock and days of cover.
4. End with a one-sentence summary.

Use only numbers returned by the tools; never estimate or invent figures. If the tool returns no data for the day, \
say so in the digest instead of guessing.
"""

POLICY = Policy(
    allowed_tools=frozenset({"get_daily_metrics", "get_reviews", "post_slack_message", "create_clickup_task"}),
    max_steps=5,
    max_tool_calls=6,
)


def build_tasks(store: Store, today: date, limit: int | None = None) -> list[Task]:
    day = today - timedelta(days=1)
    data = {"date": day.isoformat()}
    prompt = f"Today is {today.isoformat()}. Write the digest for {day.isoformat()}.\n\n{wrap('input', data)}"
    return [Task(subject=f"Digest for {day.isoformat()}", data=data, prompt=prompt)]


def _pct(value: float, base: float) -> str:
    if not base:
        return "n/a"
    change = (value - base) / base * 100
    return f"{change:+.0f}%"


def digest_text(m: dict) -> str:
    day = date.fromisoformat(m["date"])
    by_mp = " · ".join(f"{r['marketplace'].title()} {r['orders']}" for r in m["by_marketplace"]) or "none"
    avg = m["trailing_7d_avg"]
    lines = [
        f"*Daily ops digest - {day.strftime('%a %b')} {day.day}, {day.year}*",
        f"*Orders:* {m['orders']} ({by_mp}), {_pct(m['orders'], avg['orders'])} vs 7-day avg of {avg['orders']}",
        f"*Revenue:* ${m['revenue']:,.2f}, {_pct(m['revenue'], avg['revenue'])} vs 7-day avg of ${avg['revenue']:,.2f}",
        f"*Refunds:* {m['refunds']['count']} totalling ${m['refunds']['total']:,.2f}",
        f"*Late shipments:* {m['late_shipments']} (shipped 7+ days ago, not delivered)",
    ]
    if m["low_stock"]:
        lines.append(f"*Low stock ({len(m['low_stock'])} SKUs):*")
        for p in m["low_stock"]:
            lines.append(f"• {p['sku']} {p['title']} - {p['stock']} units, {p['days_of_cover']} days of cover")
    if m["negative_reviews"]:
        lines.append(f"*Negative reviews ({len(m['negative_reviews'])}):*")
        for r in m["negative_reviews"]:
            lines.append(f"• {'★' * r['rating']} {r['sku']} \"{r['title']}\" ({r['marketplace']})")
    else:
        lines.append("*Negative reviews:* none")
    return "\n".join(lines)


def digest_brain(turn: Turn) -> list[dict]:
    day = turn.task_input["date"]
    metrics = [h.result for h in turn.results("get_daily_metrics") if not h.is_error]
    if turn.number == 0:
        return [text(f"[demo] Pulling store metrics for {day}."), tool_use("get_daily_metrics", date=day)]
    if not turn.results("post_slack_message"):
        m = metrics[0]
        calls = [text("[demo] Writing the digest."),
                 tool_use("post_slack_message", channel="#ops-daily", text=digest_text(m), source_ref=f"digest:{day}")]
        urgent = [p for p in m["low_stock"] if p["days_of_cover"] is not None and p["days_of_cover"] < 7]
        if urgent:
            lines = "\n".join(f"- {p['sku']} {p['title']}: {p['stock']} units, {p['days_of_cover']} days of cover "
                              f"(reorder point {p['reorder_point']})" for p in urgent)
            calls.append(tool_use("create_clickup_task", name=f"Reorder {len(urgent)} low-stock SKUs", priority="high",
                                  tags=["inventory", "reorder"], source_ref=f"reorder:{day}",
                                  description=f"These SKUs have less than 7 days of cover:\n{lines}\n\n"
                                              "Next step: place POs / send FBA replenishment."))
        return calls
    m = metrics[0]
    return [text(f"[demo] Posted the {day} digest to #ops-daily: {m['orders']} orders, ${m['revenue']:,.2f} revenue, "
                 f"{m['refunds']['count']} refunds, {len(m['low_stock'])} low-stock SKUs.")]


WORKFLOW = Workflow(
    name="daily-digest",
    title="Daily ops digest",
    description="Summarizes yesterday's orders, refunds, late shipments, low stock and negative reviews into Slack.",
    trigger="Every day at 08:00",
    system_prompt=SYSTEM_PROMPT,
    policy=POLICY,
    build_tasks=build_tasks,
    demo_brain=digest_brain,
)
