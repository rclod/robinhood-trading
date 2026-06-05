"""TradingAgents → Robinhood equity bridge.

Turns the framework's 5-tier ratings into a guarded, sized, idempotent equity
order plan. Pure-Python and execution-agnostic: it decides and sizes; an
executor (a scheduled Claude agent with the Robinhood MCP) places the plan only
when the kill switch (``BRIDGE_ENABLED``) is on. See ``bridge/README.md``.
"""

from __future__ import annotations

from .config import BridgeConfig
from .models import MarketQuote, OrderPlan, PlannedOrder, Position, PortfolioSnapshot
from .plan import build_order_plan, persist

__all__ = [
    "BridgeConfig",
    "MarketQuote",
    "OrderPlan",
    "PlannedOrder",
    "Position",
    "PortfolioSnapshot",
    "build_order_plan",
    "persist",
]
