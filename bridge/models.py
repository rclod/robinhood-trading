"""Shared dataclasses for the Robinhood equity bridge.

The bridge is intentionally execution-agnostic: pure-Python code consumes a
:class:`PortfolioSnapshot` plus per-ticker :class:`Rating`/:class:`MarketQuote`
inputs and emits an :class:`OrderPlan`.  Nothing here places an order — the
plan is handed to an executor (a scheduled Claude agent with the Robinhood MCP
connected) for the live path, or simply logged in dry-run.

See ``bridge/README.md`` for how the pieces fit together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# --- Inputs ----------------------------------------------------------------


@dataclass(frozen=True)
class Position:
    """An open equity position. ``shares`` is signed (negative = short)."""

    symbol: str
    shares: float
    avg_cost: Optional[float] = None


@dataclass(frozen=True)
class PortfolioSnapshot:
    """Account state at decision time.

    In dry-run this comes from a fixture/JSON file; in the live path the
    executor populates it from the Robinhood MCP (``get_portfolio`` +
    ``get_equity_positions``). ``agentic_allowed`` and ``margin_enabled``
    gate execution and shorting respectively.
    """

    account_number: Optional[str]
    equity: float
    buying_power: float
    positions: Dict[str, Position] = field(default_factory=dict)
    agentic_allowed: bool = False
    margin_enabled: bool = False

    def shares_of(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        return pos.shares if pos else 0.0


@dataclass(frozen=True)
class MarketQuote:
    """Per-symbol pricing used for sizing and limit-price construction.

    ``stop_frac`` is the fractional stop distance (e.g. 0.08 == 8%) the bridge
    derives — the framework supplies no stop. ``shortable``/``sector`` feed the
    guards. All fields except ``price`` are optional so a quote can fail open.
    """

    symbol: str
    price: float
    stop_frac: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    sector: Optional[str] = None
    shortable: bool = True
    halted: bool = False
    prev_close: Optional[float] = None  # prior-session close, for intraday % move


# --- Outputs ---------------------------------------------------------------


@dataclass
class PlannedOrder:
    """One intended order with the full rationale that produced it.

    Carries enough context to be (a) human-auditable in dry-run and (b) handed
    verbatim to the executor. ``approved`` + ``reasons`` are filled by the
    guards; ``ref_id`` is the idempotency key.
    """

    symbol: str
    side: str  # "buy" | "sell"
    quantity: float
    order_type: str  # "market" | "limit"
    limit_price: Optional[float]
    notional: float
    rating: str
    sector: Optional[str]
    target_shares: float
    current_shares: float
    crosses_zero: bool
    ref_id: str
    # Fractional/dollar-based buy: when set, the order is placed by USD notional
    # (type=market, regular_hours) rather than share count — used by funding so
    # share price never strands capital or biases name selection.
    dollar_amount: Optional[float] = None
    shortable: bool = True
    halted: bool = False
    approved: bool = True
    reasons: List[str] = field(default_factory=list)

    def reject(self, reason: str) -> None:
        self.approved = False
        self.reasons.append(reason)


@dataclass
class OrderPlan:
    """The full day's book: every delta order plus the names left untouched."""

    trade_date: str
    equity: float
    orders: List[PlannedOrder] = field(default_factory=list)
    holds: List[str] = field(default_factory=list)  # rated, zero-delta (carry)
    notes: List[str] = field(default_factory=list)
    execution_enabled: bool = False  # kill switch; False => dry-run only
    # Every assessed name -> its rating, traded or not (so holds keep their
    # rating for audit). Populated by the plan builders.
    assessments: Dict[str, str] = field(default_factory=dict)
    # Capital-allocation report (set by build_rotation_plan): budget, deployed,
    # remaining, ranked candidates funded/scaled/deferred. Empty for non-rotation plans.
    rotation: Dict = field(default_factory=dict)

    @property
    def approved_orders(self) -> List[PlannedOrder]:
        return [o for o in self.orders if o.approved]

    @property
    def rejected_orders(self) -> List[PlannedOrder]:
        return [o for o in self.orders if not o.approved]
