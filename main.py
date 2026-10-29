"""
E-commerce Ops Agent - command line.

    python main.py demo                      seed the store and run all three workflows (no API key needed)
    python main.py serve                     dashboard on http://127.0.0.1:8000
    python main.py seed                      reset the database with fresh demo data
    python main.py run inbox-triage --limit 5
    python main.py run daily-digest
    python main.py run review-monitor
    python main.py approvals                 list pending approvals
    python main.py approve 3  /  reject 3 --note "wrong amount"

Add --demo to force the scripted LLM even when ANTHROPIC_API_KEY is set.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))   # only this project's .env

from opsagent.approvals import ApprovalError  # noqa: E402  (imports read CLAUDE_MODEL after .env is loaded)
from opsagent.config import Settings  # noqa: E402
from opsagent.runtime import Runtime  # noqa: E402
from opsagent.workflows import WORKFLOWS  # noqa: E402


def _runtime(args) -> Runtime:
    settings = Settings.from_env(mode="demo" if args.demo else None, db_path=args.db)
    rt = Runtime.build(settings)
    backends = rt.adapters.backends()
    label = "DEMO (scripted LLM, no API calls)" if settings.mode == "demo" else f"LIVE ({settings.model})"
    print(f"Mode: {label} | Slack: {backends['slack']} | ClickUp: {backends['clickup']} | Gmail: {backends['gmail']}")
    return rt


def _print_results(name: str, results) -> None:
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    tools = sum(r.tool_calls for r in results)
    cost = sum(r.cost_usd for r in results)
    status = ", ".join(f"{n} {s}" for s, n in sorted(by_status.items())) or "nothing to do"
    print(f"  {name:<15} {len(results):>3} runs ({status}), {tools} tool calls, ${cost:.4f}")
    for r in results[:3] if len(results) > 3 else results:
        print(f"      run #{r.run_id}: {r.final_text or r.error}")


def cmd_seed(args) -> None:
    counts = _runtime(args).seed()
    print("Seeded: " + ", ".join(f"{v} {k}" for k, v in counts.items()))


def cmd_run(args) -> None:
    rt = _runtime(args)
    _print_results(args.workflow, rt.run_workflow(args.workflow, limit=args.limit))


def cmd_demo(args) -> None:
    rt = _runtime(args)
    counts = rt.seed()
    print("Seeded: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    for name in WORKFLOWS:
        _print_results(name, rt.run_workflow(name))
    print("Re-running review-monitor to show idempotency (no duplicate tasks):")
    _print_results("review-monitor", rt.run_workflow("review-monitor"))
    pending = rt.approvals.list("pending")
    print(f"\n{len(pending)} items wait for a person in the approval queue. Open the dashboard: python main.py serve")


def cmd_approvals(args) -> None:
    rt = _runtime(args)
    for a in rt.approvals.list("pending"):
        print(f"  #{a['id']:<4} {a['action']:<17} {a['title']}")


def cmd_decide(args) -> None:
    rt = _runtime(args)
    try:
        item = (rt.approvals.approve if args.command == "approve" else rt.approvals.reject)(args.id, "cli", args.note)
    except ApprovalError as e:
        sys.exit(f"Error: {e}")
    print(f"Approval #{item['id']} is now {item['status']}. Result: {item['result']}")


def cmd_serve(args) -> None:
    import uvicorn

    from opsagent.web.app import create_app

    rt = _runtime(args)
    if not rt.is_seeded():
        print("Empty database: seeding demo data first.")
        rt.seed()
    print(f"Dashboard: http://{args.host}:{args.port}")
    uvicorn.run(create_app(rt), host=args.host, port=args.port, log_level="warning")


def main() -> None:
    parser = argparse.ArgumentParser(description="E-commerce Ops Agent")
    parser.add_argument("--demo", action="store_true", help="use the scripted LLM even if an API key is set")
    parser.add_argument("--db", help="SQLite file (default: ops.db)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed").set_defaults(func=cmd_seed)
    sub.add_parser("demo").set_defaults(func=cmd_demo)
    p = sub.add_parser("run")
    p.add_argument("workflow", choices=list(WORKFLOWS))
    p.add_argument("--limit", type=int, help="max emails for inbox-triage")
    p.set_defaults(func=cmd_run)
    sub.add_parser("approvals").set_defaults(func=cmd_approvals)
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("id", type=int)
        p.add_argument("--note", default="")
        p.set_defaults(func=cmd_decide)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
