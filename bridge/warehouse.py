"""Research warehouse — append-only JSONL, queryable with DuckDB.

Every decision (the full planned book) is appended as one JSON object per line
to ``decisions-YYYY-MM-DD.jsonl``. JSONL is the lowest-common-denominator format
that DuckDB reads natively (``read_json_auto``), so the analytical layer needs
no ETL: point DuckDB at the directory and run SQL over months of books, joining
ratings to realised outcomes for P&L attribution and backtests.

DuckDB is an *optional* dependency — :func:`query` lazily imports it and raises a
clear error if it's missing. Writing never requires it.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Any, List

from .models import OrderPlan


class Warehouse:
    """Append-only JSONL sink + optional DuckDB query helper."""

    def __init__(self, state_dir: str):
        self.dir = os.path.join(state_dir, "warehouse")
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, trade_date: str) -> str:
        return os.path.join(self.dir, f"decisions-{trade_date}.jsonl")

    def append_plan(self, plan: OrderPlan, ts: str) -> str:
        """Append the full plan as one JSONL record. Returns the file path."""
        record = {
            "ts": ts,
            "trade_date": plan.trade_date,
            "equity": plan.equity,
            "execution_enabled": plan.execution_enabled,
            "orders": [asdict(o) for o in plan.orders],
            "holds": plan.holds,
            "assessments": plan.assessments,  # every rating, incl. holds
            "rotation": plan.rotation,        # recommendation + funding report
            "notes": plan.notes,
        }
        path = self._path(plan.trade_date)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return path

    def append_fill(self, trade_date: str, record: dict) -> str:
        """Append one execution result to ``fills-YYYY-MM-DD.jsonl``.

        Records what the executor actually placed (ref_id, broker order id,
        status, alerts) so realised fills can be joined to decisions in DuckDB.
        """
        path = os.path.join(self.dir, f"fills-{trade_date}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return path

    def query(self, sql: str) -> List[Any]:
        """Run a DuckDB query over the JSONL warehouse.

        The decisions glob is exposed as the ``decisions`` view, e.g.::

            wh.query("SELECT trade_date, COUNT(*) FROM decisions GROUP BY 1")
        """
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - optional dep
            raise RuntimeError(
                "DuckDB is required for warehouse queries. "
                "Install it with: pip install duckdb"
            ) from exc

        glob = os.path.join(self.dir, "decisions-*.jsonl")
        con = duckdb.connect()
        try:
            con.execute(
                f"CREATE VIEW decisions AS "
                f"SELECT * FROM read_json_auto('{glob}', format='newline_delimited')"
            )
            return con.execute(sql).fetchall()
        finally:
            con.close()
