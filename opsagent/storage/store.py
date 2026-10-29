"""
Read and write access to the mock e-commerce store.

In production these methods would call the Amazon SP-API and the Shopify Admin
API. Keeping them behind one class means the tools never know where the data
comes from, so swapping in real APIs does not touch the agent.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from .db import Database, utcnow

RETURN_WINDOW_DAYS = 30
NEGATIVE_RATING = 3   # reviews with this rating or lower count as negative

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "my", "is", "it", "do", "i", "you", "how", "what"}


class StoreError(ValueError):
    """A store lookup or change that cannot be done, e.g. an unknown order."""


class Store:
    def __init__(self, db: Database, today: date):
        self.db = db
        self.today = today

    # ---------- Orders and customers ----------

    def get_order(self, order_id: str) -> dict | None:
        order = self.db.query_one(
            """SELECT o.*, c.name AS customer_name, c.email AS customer_email
               FROM orders o JOIN customers c ON c.id = o.customer_id
               WHERE o.id = ? COLLATE NOCASE""",
            (order_id.strip(),),
        )
        if not order:
            return None
        order["items"] = self.db.query(
            """SELECT i.sku, p.title, i.quantity, i.unit_price
               FROM order_items i JOIN products p ON p.sku = i.sku WHERE i.order_id = ?""",
            (order["id"],),
        )
        refunds = self.db.query("SELECT id, amount, reason, created_at FROM refunds WHERE order_id = ?", (order["id"],))
        order["refunds"] = refunds
        order["refunded_total"] = round(sum(r["amount"] for r in refunds), 2)
        order["return_window"] = self._return_window(order)
        return order

    def _return_window(self, order: dict) -> dict:
        if not order.get("delivered_at"):
            return {"eligible": False, "reason": "not delivered yet", "days_since_delivery": None}
        days = (self.today - date.fromisoformat(order["delivered_at"][:10])).days
        eligible = days <= RETURN_WINDOW_DAYS
        return {
            "eligible": eligible,
            "days_since_delivery": days,
            "reason": f"delivered {days} days ago; window is {RETURN_WINDOW_DAYS} days",
        }

    def lookup_customer(self, email: str | None = None, name: str | None = None) -> dict | None:
        if email:
            customer = self.db.query_one("SELECT * FROM customers WHERE email = ? COLLATE NOCASE", (email.strip(),))
        elif name:
            customer = self.db.query_one("SELECT * FROM customers WHERE name LIKE ? ORDER BY id LIMIT 1", (f"%{name.strip()}%",))
        else:
            raise StoreError("Give an email or a name to look up a customer.")
        if not customer:
            return None
        customer["orders"] = self.db.query(
            """SELECT id, marketplace, status, ordered_at, total FROM orders
               WHERE customer_id = ? ORDER BY ordered_at DESC LIMIT 10""",
            (customer["id"],),
        )
        stats = self.db.query_one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS spent FROM orders WHERE customer_id = ?",
            (customer["id"],),
        )
        customer["lifetime_orders"] = stats["n"]
        customer["lifetime_value"] = round(stats["spent"], 2)
        return customer

    def create_refund(self, order_id: str, amount: float, reason: str, source: str = "agent") -> dict:
        order = self.get_order(order_id)
        if not order:
            raise StoreError(f"Order {order_id} does not exist.")
        remaining = round(order["total"] - order["refunded_total"], 2)
        if amount <= 0 or amount > remaining + 0.001:
            raise StoreError(f"Refund of ${amount:.2f} is not allowed: ${remaining:.2f} is left to refund on {order_id}.")
        count = self.db.query_one("SELECT COUNT(*) AS n FROM refunds")["n"]
        refund_id = f"RF-{9001 + count}"
        self.db.execute(
            "INSERT INTO refunds (id, order_id, amount, reason, created_at, source) VALUES (?, ?, ?, ?, ?, ?)",
            (refund_id, order["id"], round(amount, 2), reason, utcnow(), source),
        )
        if abs(remaining - amount) < 0.001:
            self.db.execute("UPDATE orders SET status = 'refunded' WHERE id = ?", (order["id"],))
        return {"refund_id": refund_id, "order_id": order["id"], "amount": round(amount, 2)}

    # ---------- Ops metrics ----------

    def daily_metrics(self, day: date) -> dict:
        d = day.isoformat()
        by_marketplace = self.db.query(
            """SELECT marketplace, COUNT(*) AS orders, ROUND(SUM(total), 2) AS revenue
               FROM orders WHERE substr(ordered_at, 1, 10) = ? GROUP BY marketplace ORDER BY marketplace""",
            (d,),
        )
        week_start = (day - timedelta(days=7)).isoformat()
        avg = self.db.query_one(
            """SELECT COUNT(*) / 7.0 AS orders, COALESCE(SUM(total), 0) / 7.0 AS revenue FROM orders
               WHERE substr(ordered_at, 1, 10) >= ? AND substr(ordered_at, 1, 10) < ?""",
            (week_start, d),
        )
        refunds = self.db.query(
            """SELECT r.id, r.order_id, r.amount, r.reason FROM refunds r
               WHERE substr(r.created_at, 1, 10) = ? ORDER BY r.amount DESC""",
            (d,),
        )
        late = self.db.query_one(
            """SELECT COUNT(*) AS n FROM orders WHERE status = 'shipped'
               AND julianday(?) - julianday(substr(shipped_at, 1, 10)) > 7""",
            (self.today.isoformat(),),
        )["n"]
        return {
            "date": d,
            "orders": sum(r["orders"] for r in by_marketplace),
            "revenue": round(sum(r["revenue"] for r in by_marketplace), 2),
            "by_marketplace": by_marketplace,
            "trailing_7d_avg": {"orders": round(avg["orders"], 1), "revenue": round(avg["revenue"], 2)},
            "refunds": {"count": len(refunds), "total": round(sum(r["amount"] for r in refunds), 2), "items": refunds},
            "late_shipments": late,
            "low_stock": self.low_stock(),
            "negative_reviews": self.reviews(since=day, max_rating=NEGATIVE_RATING, until=day),
        }

    def low_stock(self) -> list[dict]:
        rows = self.db.query(
            """SELECT sku, title, stock, reorder_point, daily_velocity FROM products
               WHERE stock <= reorder_point ORDER BY stock * 1.0 / daily_velocity"""
        )
        for r in rows:
            r["days_of_cover"] = round(r["stock"] / r["daily_velocity"], 1) if r["daily_velocity"] else None
        return rows

    def reviews(self, since: date, max_rating: int = 5, until: date | None = None) -> list[dict]:
        until = until or self.today
        return self.db.query(
            """SELECT r.id, r.sku, p.title AS product, r.marketplace, r.rating, r.title, r.body, r.author, r.created_at
               FROM reviews r JOIN products p ON p.sku = r.sku
               WHERE substr(r.created_at, 1, 10) BETWEEN ? AND ? AND r.rating <= ?
               ORDER BY r.rating, r.created_at""",
            (since.isoformat(), until.isoformat(), max_rating),
        )

    def product(self, sku: str) -> dict | None:
        return self.db.query_one("SELECT * FROM products WHERE sku = ?", (sku,))

    # ---------- Knowledge base ----------

    def search_knowledge_base(self, query: str, limit: int = 3) -> list[dict]:
        """Keyword search with a simple score: title matches count double. Good enough for ~20 articles."""
        terms = [t for t in _WORD.findall(query.lower()) if t not in _STOPWORDS]
        if not terms:
            return []
        scored = []
        for art in self.db.query("SELECT id, title, category, body FROM knowledge_base"):
            title, body = art["title"].lower(), art["body"].lower()
            score = sum(2 * title.count(t) + body.count(t) for t in terms)
            if score:
                scored.append((score, art))
        scored.sort(key=lambda x: (-x[0], x[1]["id"]))
        return [a for _, a in scored[:limit]]

    # ---------- Inbox ----------

    def email(self, email_id: str) -> dict | None:
        return self.db.query_one("SELECT * FROM emails WHERE id = ?", (email_id,))

    def untriaged_emails(self, limit: int | None = None) -> list[dict]:
        sql = """SELECT e.* FROM emails e LEFT JOIN triage t ON t.email_id = e.id
                 WHERE t.email_id IS NULL ORDER BY e.received_at, e.id"""
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.db.query(sql)
