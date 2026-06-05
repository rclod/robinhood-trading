"""Configuration for the Robinhood equity bridge.

All money-risk knobs live here. Defaults target a ~$25k margin account running
a daily swing book (pre-open run, open execution), long + short, with %-equity
sizing capped by the 1%-risk golden rule.

Every field can be overridden via ``BRIDGE_*`` environment variables so the
config can be tuned without editing code (mirrors TradingAgents'
``TRADINGAGENTS_*`` convention). ``account_number`` has no safe default — the
Robinhood MCP forbids auto-selecting it — so it must be supplied explicitly
before the live path will run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# Canonical 5-tier scale (kept in sync with
# tradingagents.agents.utils.rating.RATINGS_5_TIER).
DEFAULT_TIER_TARGET: Dict[str, float] = {
    "Buy": 0.15,
    "Overweight": 0.08,
    "Hold": 0.0,  # special-cased: target == current (carry), never "go flat"
    "Underweight": -0.08,
    "Sell": -0.12,  # short side trimmed vs Buy (borrow / assignment risk)
}

# A liquid, sector-diversified seed universe. Edit freely — this is just the
# starting watchlist propagate() runs on each day.
DEFAULT_WATCHLIST: List[str] = [
    "AAPL", "MSFT", "NVDA", "AMD", "AVGO",   # semis / tech
    "GOOGL", "META", "NFLX",                 # communication
    "AMZN", "TSLA", "HD",                    # consumer discretionary
    "JPM", "V", "GS",                        # financials
    "UNH", "LLY",                            # health care
    "XOM", "CVX",                            # energy
    "CAT", "COST",                           # industrials / staples
]


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class BridgeConfig:
    """Tunable policy + safety envelope for the bridge."""

    # --- account / execution ---
    account_number: Optional[str] = None
    # Master kill switch. While False, the bridge plans and logs but the
    # executor must NOT place anything. Phase 0 ships with this off.
    execution_enabled: bool = False

    # --- sizing ---
    tier_target: Dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_TIER_TARGET)
    )
    allow_short: bool = True          # honoured only if the account has margin;
                                      # on a cash account bearish ratings exit to flat
    risk_per_trade: float = 0.01      # 1% of equity max loss at the stop
    stop_atr_mult: float = 2.0        # stop distance = mult * ATR(14)
    stop_floor: float = 0.05          # clamp derived stop to [floor, cap]
    stop_cap: float = 0.12
    stop_fallback: float = 0.08       # used when ATR is unavailable

    # --- caps / diversification ---
    max_positions: int = 8
    per_name_cap: float = 0.18        # hard notional ceiling per name
    sector_cap: float = 0.30          # max gross exposure per sector
    max_daily_notional: float = 10_000.0

    # --- execution semantics ---
    market_hours: str = "regular_hours"   # shorts / flips can't use extended
    order_type: str = "limit"             # marketable limit at ask/bid
    time_in_force: str = "gfd"
    # Cross the spread by this fraction to make the limit marketable.
    marketable_offset: float = 0.001

    # --- universe ---
    watchlist: List[str] = field(default_factory=lambda: list(DEFAULT_WATCHLIST))

    # --- storage ---
    state_dir: str = field(
        default_factory=lambda: os.getenv(
            "BRIDGE_STATE_DIR",
            os.path.join(os.path.expanduser("~"), ".tradingagents", "bridge"),
        )
    )

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        """Build a config with ``BRIDGE_*`` env-var overrides applied."""
        cfg = cls(
            account_number=os.getenv("BRIDGE_ACCOUNT_NUMBER") or None,
            execution_enabled=_env_bool("BRIDGE_ENABLED", False),
            allow_short=_env_bool("BRIDGE_ALLOW_SHORT", True),
            risk_per_trade=_env_float("BRIDGE_RISK_PER_TRADE", 0.01),
            stop_atr_mult=_env_float("BRIDGE_STOP_ATR_MULT", 2.0),
            stop_floor=_env_float("BRIDGE_STOP_FLOOR", 0.05),
            stop_cap=_env_float("BRIDGE_STOP_CAP", 0.12),
            stop_fallback=_env_float("BRIDGE_STOP_FALLBACK", 0.08),
            max_positions=_env_int("BRIDGE_MAX_POSITIONS", 8),
            per_name_cap=_env_float("BRIDGE_PER_NAME_CAP", 0.18),
            sector_cap=_env_float("BRIDGE_SECTOR_CAP", 0.30),
            max_daily_notional=_env_float("BRIDGE_MAX_DAILY_NOTIONAL", 10_000.0),
            market_hours=os.getenv("BRIDGE_MARKET_HOURS", "regular_hours"),
            order_type=os.getenv("BRIDGE_ORDER_TYPE", "limit"),
            time_in_force=os.getenv("BRIDGE_TIME_IN_FORCE", "gfd"),
        )
        watchlist = os.getenv("BRIDGE_WATCHLIST")
        if watchlist:
            cfg.watchlist = [s.strip().upper() for s in watchlist.split(",") if s.strip()]
        return cfg

    def stop_frac(self, atr: Optional[float], price: float) -> float:
        """Derive the fractional stop distance, clamped, with a fixed fallback."""
        if not atr or not price or price <= 0:
            return self.stop_fallback
        raw = self.stop_atr_mult * atr / price
        return max(self.stop_floor, min(self.stop_cap, raw))
