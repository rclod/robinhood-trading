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
    "CAT",                                    # industrials
    "COST", "WMT",                           # consumer staples / retail
]


# Sector → liquid ETF proxy for real-time sector-move reads (quoted via the
# Robinhood MCP). yfinance sector names map to SPDR sector ETFs.
SECTOR_ETF_PROXY: Dict[str, str] = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Energy": "XLE",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Industrials": "XLI",
    "Communication Services": "XLC",
    "Utilities": "XLU",
    "Basic Materials": "XLB",
    "Real Estate": "XLRE",
}

# Semiconductors hide inside "Technology" in yfinance's sector field, so known
# semis get the tighter SMH proxy to catch semi-specific rotation (e.g. the
# Broadcom-driven unwind). Override takes precedence over the broad sector ETF.
SEMI_SYMBOLS = ("NVDA", "AMD", "AVGO", "SMCI", "MU", "INTC", "QCOM", "TXN", "ASML", "TSM", "ARM")
SEMI_PROXY = "SMH"

# Sector/thematic ETFs added to the rated universe so sector conviction can be
# expressed without a single-name bet (e.g. SMH for semis vs. picking AVGO/AMD).
# Each carries a sector so it shares the sector exposure cap with its single
# names — yfinance often returns no sector for an ETF, so we map it explicitly.
DEFAULT_ETFS: List[str] = ["SMH", "XLK", "XLF", "XLE", "XLV", "XLY", "XLC", "XLI", "XLP"]
ETF_SECTOR: Dict[str, str] = {
    "SMH": "Technology", "XLK": "Technology", "XLF": "Financial Services",
    "XLE": "Energy", "XLV": "Healthcare", "XLY": "Consumer Cyclical",
    "XLC": "Communication Services", "XLI": "Industrials", "XLP": "Consumer Defensive",
}

# Symbols treated as ETFs (a bounded "sleeve" — see config fields below). ETF
# ratings come from a partially-blind analysis (no company fundamentals), so they
# get a conviction haircut and a capped share of capital, and fund AFTER stocks.
KNOWN_ETFS = frozenset(ETF_SECTOR)


def is_etf(symbol: str) -> bool:
    return symbol.upper() in KNOWN_ETFS


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
    # Dry powder: fraction of settled BUYING POWER kept UNdeployed each day. On a
    # cash account sells settle T+1, so deploying 100% today leaves nothing
    # settled to act on tomorrow. Funding deploys at most (1 - frac) of buying power.
    cash_reserve_frac: float = 0.20
    # ETF sleeve: ETFs are a bounded complement to single-name stock-picking, not
    # equal citizens in the conviction ranking. Their score is cut by the haircut
    # (so a stock of equal rating funds first), and total ETF exposure can't exceed
    # the sleeve cap (fraction of equity). Overlap with single names is handled by
    # the shared sector cap + stocks-first ordering.
    etf_conviction_haircut: float = 12.0
    etf_sleeve_frac: float = 0.33
    # Absolute cap on total new BUYS deployed in a run (USD). None = no cap; the
    # dry-powder budget governs. Set to honour "deploy up to $X this run".
    max_deploy: Optional[float] = None
    risk_per_trade: float = 0.01      # 1% of equity max loss at the stop
    stop_atr_mult: float = 2.0        # stop distance = mult * ATR(14)
    stop_floor: float = 0.05          # clamp derived stop to [floor, cap]
    stop_cap: float = 0.12
    stop_fallback: float = 0.08       # used when ATR is unavailable

    # --- caps / diversification ---
    max_positions: int = 8
    per_name_cap: float = 0.18        # hard notional ceiling per name
    sector_cap: float = 0.30          # max gross exposure per sector
    # Optional daily turnover throttle. None (default) = no separate cap; the
    # dry-powder budget (<=90% of buying power) + the buying-power guard already
    # bound how much can deploy in a day. Set a number to throttle churn further.
    max_daily_notional: Optional[float] = None

    # --- execution semantics ---
    market_hours: str = "regular_hours"   # shorts / flips can't use extended
    order_type: str = "limit"             # marketable limit at ask/bid
    time_in_force: str = "gfd"
    # Cross the spread by this fraction to make the limit marketable.
    marketable_offset: float = 0.001

    # --- intraday risk monitor (Moderate preset) ---
    intraday_position_stop: float = 0.08      # name down >8% on day -> HARD auto-exit
    intraday_sector_drop: float = 0.04        # sector proxy down >4% -> SOFT re-rate
    intraday_portfolio_drawdown: float = 0.05  # book down >5% on day -> risk-off (halt buys)
    intraday_hard_exit_fraction: float = 1.0  # fraction of a hard-stopped position to shed (1.0 = full exit)

    # --- universe (single names + sector ETFs) ---
    watchlist: List[str] = field(
        default_factory=lambda: list(DEFAULT_WATCHLIST) + list(DEFAULT_ETFS)
    )

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
            cash_reserve_frac=_env_float("BRIDGE_CASH_RESERVE_FRAC", 0.20),
            etf_conviction_haircut=_env_float("BRIDGE_ETF_HAIRCUT", 12.0),
            etf_sleeve_frac=_env_float("BRIDGE_ETF_SLEEVE_FRAC", 0.33),
            max_deploy=(
                float(os.environ["BRIDGE_MAX_DEPLOY"])
                if os.getenv("BRIDGE_MAX_DEPLOY") else None
            ),
            risk_per_trade=_env_float("BRIDGE_RISK_PER_TRADE", 0.01),
            stop_atr_mult=_env_float("BRIDGE_STOP_ATR_MULT", 2.0),
            stop_floor=_env_float("BRIDGE_STOP_FLOOR", 0.05),
            stop_cap=_env_float("BRIDGE_STOP_CAP", 0.12),
            stop_fallback=_env_float("BRIDGE_STOP_FALLBACK", 0.08),
            max_positions=_env_int("BRIDGE_MAX_POSITIONS", 8),
            per_name_cap=_env_float("BRIDGE_PER_NAME_CAP", 0.18),
            sector_cap=_env_float("BRIDGE_SECTOR_CAP", 0.30),
            max_daily_notional=(
                float(os.environ["BRIDGE_MAX_DAILY_NOTIONAL"])
                if os.getenv("BRIDGE_MAX_DAILY_NOTIONAL") else None
            ),
            market_hours=os.getenv("BRIDGE_MARKET_HOURS", "regular_hours"),
            order_type=os.getenv("BRIDGE_ORDER_TYPE", "limit"),
            time_in_force=os.getenv("BRIDGE_TIME_IN_FORCE", "gfd"),
            intraday_position_stop=_env_float("BRIDGE_INTRADAY_POSITION_STOP", 0.08),
            intraday_sector_drop=_env_float("BRIDGE_INTRADAY_SECTOR_DROP", 0.04),
            intraday_portfolio_drawdown=_env_float("BRIDGE_INTRADAY_PORTFOLIO_DD", 0.05),
            intraday_hard_exit_fraction=_env_float("BRIDGE_INTRADAY_HARD_EXIT_FRAC", 1.0),
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

    def sector_proxy_for(self, symbol: str, sector: Optional[str]) -> Optional[str]:
        """ETF proxy for a name's sector — SMH for known semis, else the SPDR
        sector ETF. ``None`` if the sector has no mapped proxy."""
        if symbol.upper() in SEMI_SYMBOLS:
            return SEMI_PROXY
        return SECTOR_ETF_PROXY.get(sector or "")
