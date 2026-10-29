"""
SQLite storage for everything the agent touches.

One database file holds two groups of tables:

- the mock store (an Amazon + Shopify seller's orders, customers, products,
  reviews, refunds, inbox and knowledge base), and
- the agent's own records (runs, steps, approvals, integration calls,
  idempotency keys and triage results).

A new connection is opened per unit of work, so the same Database object can be
shared by the CLI, background runs and the web server's worker threads.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
-- ---------- Mock store (Amazon + Shopify) ----------
CREATE TABLE IF NOT EXISTS products (
    sku            TEXT PRIMARY KEY,
    asin           TEXT NOT NULL,
    title          TEXT NOT NULL,
    category       TEXT NOT NULL,
    price          REAL NOT NULL,
    stock          INTEGER NOT NULL,
    reorder_point  INTEGER NOT NULL,
    daily_velocity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    phone       TEXT,
    city        TEXT,
    state       TEXT,
    marketplace TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id            TEXT PRIMARY KEY,
    marketplace   TEXT NOT NULL CHECK (marketplace IN ('amazon', 'shopify')),
    customer_id   TEXT NOT NULL REFERENCES customers(id),
    status        TEXT NOT NULL,
    ordered_at    TEXT NOT NULL,
    shipped_at    TEXT,
    delivered_at  TEXT,
    carrier       TEXT,
    tracking      TEXT,
    total         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS order_items (
    order_id   TEXT NOT NULL REFERENCES orders(id),
    sku        TEXT NOT NULL REFERENCES products(sku),
    quantity   INTEGER NOT NULL,
    unit_price REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS refunds (
    id          TEXT PRIMARY KEY,
    order_id    TEXT NOT NULL REFERENCES orders(id),
    amount      REAL NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    source      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id          TEXT PRIMARY KEY,
    sku         TEXT NOT NULL REFERENCES products(sku),
    marketplace TEXT NOT NULL,
    rating      INTEGER NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    author      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS emails (
    id          TEXT PRIMARY KEY,
    from_name   TEXT NOT NULL,
    from_addr   TEXT NOT NULL,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    received_at TEXT NOT NULL,
    thread_id   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_base (
    id       TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    category TEXT NOT NULL,
    body     TEXT NOT NULL
);

-- ---------- Agent records ----------
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow        TEXT NOT NULL,
    mode            TEXT NOT NULL,
    model           TEXT NOT NULL,
    subject         TEXT NOT NULL,
    status          TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    duration_ms     INTEGER,
    llm_calls       INTEGER NOT NULL DEFAULT 0,
    tool_calls      INTEGER NOT NULL DEFAULT 0,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL NOT NULL DEFAULT 0,
    usage_estimated INTEGER NOT NULL DEFAULT 0,
    final_text      TEXT,
    error           TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,   -- llm | tool | guardrail | approval | retry | error
    name        TEXT NOT NULL,
    status      TEXT NOT NULL,   -- ok | error | blocked | pending | duplicate
    input_json  TEXT,
    output_json TEXT,
    duration_ms INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES runs(id),
    kind            TEXT NOT NULL,      -- action (executes on approve) | review (human follow-up)
    action          TEXT NOT NULL,      -- send_email_reply | issue_refund | human_review
    title           TEXT NOT NULL,
    reason          TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL,      -- pending | executed | rejected | resolved | failed
    idempotency_key TEXT UNIQUE,
    created_at      TEXT NOT NULL,
    decided_at      TEXT,
    decided_by      TEXT,
    note            TEXT,
    result_json     TEXT
);
CREATE TABLE IF NOT EXISTS integration_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER,
    integration   TEXT NOT NULL,   -- slack | clickup | gmail
    backend       TEXT NOT NULL,   -- sandbox | live
    operation     TEXT NOT NULL,
    status        TEXT NOT NULL,   -- ok | error
    attempts      INTEGER NOT NULL,
    request_json  TEXT NOT NULL,
    response_json TEXT,
    duration_ms   INTEGER,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    key         TEXT PRIMARY KEY,
    tool        TEXT NOT NULL,
    run_id      INTEGER,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS triage (
    email_id   TEXT PRIMARY KEY REFERENCES emails(id),
    run_id     INTEGER REFERENCES runs(id),
    category   TEXT NOT NULL,
    priority   TEXT NOT NULL,
    order_ids  TEXT NOT NULL,
    summary    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_calls_run ON integration_calls(run_id);
"""

AGENT_TABLES = ("triage", "idempotency", "integration_calls", "approvals", "steps", "runs")
STORE_TABLES = ("refunds", "order_items", "orders", "reviews", "emails", "knowledge_base", "customers", "products")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")   # readers (dashboard) don't block the writer (agent runs)
            conn.executescript(SCHEMA)

    def reset(self) -> None:
        """Drop every table and recreate the schema (used by the seed script and tests)."""
        with self.connect() as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            for table in AGENT_TABLES + STORE_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        self.init_schema()

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        """Run one write statement and return the new row id."""
        with self.connect() as conn:
            return conn.execute(sql, params).lastrowid
