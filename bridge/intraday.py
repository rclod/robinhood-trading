"""Intraday risk monitor — the cheap, fast loop that runs midday + hourly.

It does NOT run the full agent debate. It reads real-time prices (held names +
sector-ETF proxies, quoted via the Robinhood MCP) and a best-effort news scan,
computes per-position / per-sector / portfolio moves, and applies tiered
trip-wires (Moderate preset):

  HARD  (auto-exit):  a name down ≥ position_stop on the day → shed it now.
  SOFT  (re-rate):    its sector proxy down ≥ sector_drop, OR material adverse
                      news → escalate to a *targeted* propagate re-rate on just
                      that name and act on the fresh rating.
  RISK-OFF:           the book down ≥ portfolio_drawdown → halt new buys for the
                      rest of the session (a flag the pre-open/buy path reads)
                      and escalate every held name to a re-rate.

De-risk orders are sells, so they are risk-reducing (bypass exposure caps) and
need no buying power — they work even on a near-zero-cash account. Output is the
same execution payload the executor emits, so the agent's review→place→record
flow is identical. Nothing is placed unless ``BRIDGE_ENABLED`` is on.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Dict, List, Optional

from .config import BridgeConfig
from .ledger import Ledger, default_db_path
from .models import MarketQuote, OrderPlan, PortfolioSnapshot
from .reconcile import build_plan_from_targets
from .warehouse import Warehouse

logger = logging.getLogger(__name__)


@dataclass
class PositionRisk:
    symbol: str
    sector: Optional[str]
    proxy: Optional[str]
    current_shares: float
    last: float
    prev_close: Optional[float]
    pct_day: Optional[float]       # (last - prev_close) / prev_close
    proxy_pct: Optional[float]     # sector ETF intraday move
    adverse_news: bool
    tier: str = "none"             # none | soft | hard
    reasons: List[str] = field(default_factory=list)


@dataclass
class IntradayAssessment:
    trade_date: str
    portfolio_pct: Optional[float]
    risk_off: bool
    positions: List[PositionRisk]
    sector_moves: Dict[str, float]
    alerts: List[str] = field(default_factory=list)


def _pct(last: float, prev: Optional[float]) -> Optional[float]:
    if not prev or prev <= 0:
        return None
    return (last - prev) / prev


def assess(
    trade_date: str,
    snapshot: PortfolioSnapshot,
    quotes: Dict[str, MarketQuote],
    proxy_quotes: Dict[str, MarketQuote],
    news: Dict[str, "object"],
    cfg: BridgeConfig,
) -> IntradayAssessment:
    """Classify every held position into none/soft/hard and detect risk-off."""
    sector_moves = {
        sym: _pct(q.price, q.prev_close)
        for sym, q in proxy_quotes.items()
        if _pct(q.price, q.prev_close) is not None
    }

    positions: List[PositionRisk] = []
    held_value_now = held_value_prev = 0.0

    for sym, pos in snapshot.positions.items():
        if pos.shares == 0:
            continue
        q = quotes.get(sym)
        if q is None:
            continue
        pct_day = _pct(q.price, q.prev_close)
        proxy = cfg.sector_proxy_for(sym, q.sector)
        proxy_pct = sector_moves.get(proxy) if proxy else None
        adverse = bool(getattr(news.get(sym), "adverse", False)) if news else False

        held_value_now += pos.shares * q.price
        if q.prev_close:
            held_value_prev += pos.shares * q.prev_close

        pr = PositionRisk(
            symbol=sym, sector=q.sector, proxy=proxy, current_shares=pos.shares,
            last=q.price, prev_close=q.prev_close, pct_day=pct_day,
            proxy_pct=proxy_pct, adverse_news=adverse,
        )

        if pct_day is not None and pct_day <= -cfg.intraday_position_stop:
            pr.tier = "hard"
            pr.reasons.append(f"down {pct_day:.1%} on day ≥ {cfg.intraday_position_stop:.0%} stop")
        else:
            if proxy_pct is not None and proxy_pct <= -cfg.intraday_sector_drop:
                pr.tier = "soft"
                pr.reasons.append(f"sector {proxy} down {proxy_pct:.1%} ≥ {cfg.intraday_sector_drop:.0%}")
            if adverse:
                pr.tier = "soft"
                hits = getattr(news.get(sym), "hits", [])
                pr.reasons.append(f"adverse news: {', '.join(hits)}")
        positions.append(pr)

    portfolio_pct = _pct(held_value_now, held_value_prev) if held_value_prev else None
    risk_off = portfolio_pct is not None and portfolio_pct <= -cfg.intraday_portfolio_drawdown

    alerts: List[str] = []
    if risk_off:
        alerts.append(
            f"RISK-OFF: book down {portfolio_pct:.1%} ≥ {cfg.intraday_portfolio_drawdown:.0%} "
            "— halting new buys; re-rating all held names"
        )
        # Escalate every still-untripped held name to a re-rate.
        for pr in positions:
            if pr.tier == "none":
                pr.tier = "soft"
                pr.reasons.append("portfolio risk-off escalation")

    for pr in positions:
        if pr.tier == "hard":
            alerts.append(f"HARD stop {pr.symbol}: {'; '.join(pr.reasons)}")
        elif pr.tier == "soft":
            alerts.append(f"SOFT {pr.symbol}: {'; '.join(pr.reasons)}")

    return IntradayAssessment(
        trade_date=trade_date,
        portfolio_pct=portfolio_pct, risk_off=risk_off,
        positions=positions, sector_moves=sector_moves, alerts=alerts,
    )


def _hard_exit_target(current: float, cfg: BridgeConfig) -> float:
    """Shares to keep after shedding the hard-exit fraction (whole shares)."""
    keep = current * (1.0 - cfg.intraday_hard_exit_fraction)
    return float(math.floor(keep)) if keep > 0 else 0.0


def build_intraday_plan(
    trade_date: str,
    assessment: IntradayAssessment,
    snapshot: PortfolioSnapshot,
    quotes: Dict[str, MarketQuote],
    cfg: BridgeConfig,
    rerate_fn: Optional[Callable[[str], str]] = None,
) -> OrderPlan:
    """Turn the assessment into a guarded de-risk plan.

    Hard tiers exit immediately. Soft tiers re-rate via ``rerate_fn`` (a function
    symbol -> 5-tier rating, typically a targeted propagate run) and size the new
    target; with no ``rerate_fn`` they are alert-only (left to the human).
    """
    from .guards import apply_guards
    from .sizing import stop_frac_for, target_shares

    allow_short = cfg.allow_short and snapshot.margin_enabled
    targets: Dict[str, float] = {}
    labels: Dict[str, str] = {}
    notes: List[str] = list(assessment.alerts)

    for pr in assessment.positions:
        if pr.tier == "hard":
            targets[pr.symbol] = _hard_exit_target(pr.current_shares, cfg)
            labels[pr.symbol] = f"intraday-stop({pr.pct_day:.0%})" if pr.pct_day is not None else "intraday-stop"
        elif pr.tier == "soft":
            if rerate_fn is None:
                notes.append(f"{pr.symbol}: soft trigger — alert only (no re-rate fn)")
                continue
            rating = rerate_fn(pr.symbol)
            q = quotes[pr.symbol]
            stop = stop_frac_for(cfg, None, q.price) if q.stop_frac is None else q.stop_frac
            tgt = target_shares(rating, snapshot.equity, q.price, stop,
                                pr.current_shares, cfg, allow_short=allow_short)
            targets[pr.symbol] = tgt
            labels[pr.symbol] = f"intraday-rerate:{rating}"

    plan = build_plan_from_targets(trade_date, targets, snapshot, quotes, cfg, labels)
    plan = apply_guards(plan, snapshot, cfg)
    plan.notes = notes + plan.notes
    return plan


# --- risk-off flag (read by the buy path to suppress new buys) -------------


def _risk_off_path(cfg: BridgeConfig, trade_date: str) -> str:
    return os.path.join(cfg.state_dir, f"risk_off-{trade_date}.flag")


def set_risk_off(cfg: BridgeConfig, trade_date: str, reason: str) -> None:
    os.makedirs(cfg.state_dir, exist_ok=True)
    with open(_risk_off_path(cfg, trade_date), "w", encoding="utf-8") as f:
        f.write(reason + "\n")


def is_risk_off(cfg: BridgeConfig, trade_date: str) -> bool:
    return os.path.exists(_risk_off_path(cfg, trade_date))


# --- CLI -------------------------------------------------------------------


def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _quotes_with_prevclose(raw: Dict[str, dict], cfg: BridgeConfig) -> Dict[str, MarketQuote]:
    """Intraday quotes fixture: each entry needs price + prev_close (+ optional
    sector/bid/ask). Built by the agent from MCP get_equity_quotes — which does
    not carry a sector, so a missing sector is resolved via yfinance (fail-open)
    so the agent only has to supply price + prev_close."""
    from .marketdata import _sector  # lazy: yfinance-backed, cached, fail-open

    out = {}
    for sym, q in raw.items():
        price = float(q["price"])
        sector = q.get("sector") or _sector(sym)
        out[sym] = MarketQuote(
            symbol=sym, price=price,
            stop_frac=q.get("stop_frac") or cfg.stop_fallback,
            bid=q.get("bid"), ask=q.get("ask"), sector=sector,
            prev_close=float(q["prev_close"]) if q.get("prev_close") is not None else None,
        )
    return out


def main() -> None:
    from .executor import build_execution_payload
    from .sources import snapshot_from_fixture, snapshot_from_mcp
    from . import news as news_mod

    ap = argparse.ArgumentParser(description="Intraday risk monitor → de-risk payload.")
    ap.add_argument("--account-number", required=True)
    ap.add_argument("--portfolio", help="snapshot fixture")
    ap.add_argument("--account-json")
    ap.add_argument("--portfolio-json")
    ap.add_argument("--positions-json")
    ap.add_argument("--quotes", required=True, help="held-name intraday quotes (price + prev_close)")
    ap.add_argument("--proxy-quotes", help="sector-ETF intraday quotes (price + prev_close)")
    ap.add_argument("--scan-news", action="store_true", help="scan yfinance headlines for held names")
    ap.add_argument("--rerate", action="store_true", help="run targeted propagate on soft-tripped names")
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()

    cfg = BridgeConfig.from_env()

    if args.portfolio:
        snapshot = snapshot_from_fixture(_load(args.portfolio), cfg)
    elif args.account_json and args.portfolio_json and args.positions_json:
        snapshot = snapshot_from_mcp(_load(args.account_json), _load(args.portfolio_json),
                                     _load(args.positions_json), cfg,
                                     account_number=args.account_number)
    else:
        ap.error("provide --portfolio OR --account-json/--portfolio-json/--positions-json")

    quotes = _quotes_with_prevclose(_load(args.quotes), cfg)
    proxy_quotes = _quotes_with_prevclose(_load(args.proxy_quotes), cfg) if args.proxy_quotes else {}
    held = [s for s, p in snapshot.positions.items() if p.shares != 0]
    news = news_mod.scan(held, args.date) if args.scan_news else {}

    assessment = assess(args.date, snapshot, quotes, proxy_quotes, news, cfg)
    if assessment.risk_off:
        set_risk_off(cfg, args.date, assessment.alerts[0] if assessment.alerts else "risk-off")

    rerate_fn = None
    if args.rerate:
        from .sources import ratings_from_propagate
        rerate_fn = lambda sym: ratings_from_propagate([sym], args.date).get(sym, "Hold")

    plan = build_intraday_plan(args.date, assessment, snapshot, quotes, cfg, rerate_fn)

    ts = datetime.now(timezone.utc).isoformat()
    Ledger(default_db_path(cfg.state_dir)).record_plan(plan, ts)
    Warehouse(cfg.state_dir).append_plan(plan, ts)

    payload = build_execution_payload(plan, args.account_number, cfg)
    payload["intraday"] = {
        "portfolio_pct": assessment.portfolio_pct,
        "risk_off": assessment.risk_off,
        "alerts": assessment.alerts,
        "positions": [asdict(p) for p in assessment.positions],
        "sector_moves": assessment.sector_moves,
    }
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
