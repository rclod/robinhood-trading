"""Operational ledger — SQLite (WAL), the bridge's transactional hot path.

Holds exactly what must be consistent under read-modify-write:
  - ``orders``: every planned/placed order keyed by ``ref_id`` for idempotency
    (a re-run of the same day can't double-fire).
  - day-trade counter: derived from ``orders`` as telemetry only (PDT no longer
    binds — see guards.py).

SQLite is chosen over DuckDB here deliberately: it's transactional, ships in the
stdlib, matches LangGraph's checkpointer, and handles concurrent reader/writer
via WAL. Analytical queries over the book belong in the DuckDB warehouse.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional

from .models import OrderPlan, PlannedOrder

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    ref_id       TEXT PRIMARY KEY,
    trade_date   TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    quantity     REAL NOT NULL,
    order_type   TEXT NOT NULL,
    limit_price  REAL,
    notional     REAL NOT NULL,
    rating       TEXT,
    status       TEXT NOT NULL,          -- planned | placed | rejected | filled
    created_ts   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_date_symbol ON orders(trade_date, symbol);
"""


class Ledger:
    """Thin SQLite wrapper for idempotency + order history."""

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        finally:
            conn.close()

    def already_seen(self, ref_id: str) -> bool:
        """True if this exact logical order was already recorded."""
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM orders WHERE ref_id = ?", (ref_id,)
            ).fetchone()
            return row is not None

    def record(self, order: PlannedOrder, status: str, ts: str) -> bool:
        """Insert an order idempotently. Returns False if ref_id already exists."""
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT OR IGNORE INTO orders
                  (ref_id, trade_date, symbol, side, quantity, order_type,
                   limit_price, notional, rating, status, created_ts)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    order.ref_id,
                    ts[:10],
                    order.symbol,
                    order.side,
                    order.quantity,
                    order.order_type,
                    order.limit_price,
                    order.notional,
                    order.rating,
                    status,
                    ts,
                ),
            )
            return cur.rowcount > 0

    def record_plan(self, plan: OrderPlan, ts: str) -> int:
        """Record every order in a plan. Returns count of newly-inserted rows."""
        inserted = 0
        for o in plan.orders:
            status = "planned" if o.approved else "rejected"
            if self.record(o, status, ts):
                inserted += 1
        return inserted

    def day_trade_count(self, trade_date: str) -> int:
        """Telemetry: symbols with both a buy and a sell recorded on a date."""
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT symbol,
                       SUM(side = 'buy')  AS buys,
                       SUM(side = 'sell') AS sells
                FROM orders WHERE trade_date = ?
                GROUP BY symbol
                """,
                (trade_date,),
            ).fetchall()
        return sum(1 for r in rows if r["buys"] and r["sells"])


def default_db_path(state_dir: str) -> str:
    return os.path.join(state_dir, "ledger.sqlite")
