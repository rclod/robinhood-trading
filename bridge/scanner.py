"""Weekly speculative ('lottery') stock scanner — social-arbitrage discovery.

Uses Grok 4.3 with xAI live search (web_search + x_search) to surface under-the-
radar US-listed stocks with outsized short/mid-term potential driven by the
current news cycle and X/social trends — information asymmetry / "buy the rumor" /
emerging cultural & consumer behavior, in the spirit of Chris Camillo's social
arbitrage. This is DISCOVERY, not the per-ticker rating pipeline: it finds names
we didn't know to watch, ranks them by conviction, and persists them for the week.

The candidates feed the speculative sleeve (a bounded slice of net liq) and the
dynamic watchlist (so once held they're managed by the normal daily analysis).

CLI: ``python -m bridge.scanner --save <path> [--n 8] [--date YYYY-MM-DD]``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a sharp equity scout in the mold of social-arbitrage investing (Chris "
    "Camillo): you find under-the-radar, culturally/socially driven stock "
    "opportunities BEFORE Wall Street's data catches up — information asymmetry, "
    "'buy the rumor', viral moments, emerging consumer behavior. You actively use "
    "live web and X (social) search to read the current cultural and news cycle."
)


def _user_prompt(d: str, n: int) -> str:
    return (
        f"Today is {d}. Scan the CURRENT news cycle and X/social trends for US-listed, "
        "retail-tradable stocks with potential for outsized SHORT-to-MID-term returns "
        "(days to a few months) from information asymmetry, emerging cultural/consumer "
        "trends, viral moments, or pre-data catalysts that data-driven investors can't "
        "yet price. Prefer under-the-radar names; small-caps (<$10 or <$100/share) are "
        "great, larger is fine if the catalyst warrants. Avoid mega-caps everyone "
        "already follows.\n\n"
        f"Return up to {n} candidates. After any reasoning, end your reply with ONLY a "
        "JSON array (nothing after it):\n"
        '[{"ticker":"TICK","company":"...","price":<number>,"thesis":"...",'
        '"catalyst":"...","timeframe":"...","conviction":<0-100>,"sources":["url"]}]'
    )


def _extract_json(text: str) -> List[dict]:
    """Pull the last JSON array out of the model's reply."""
    matches = re.findall(r"\[\s*\{.*?\}\s*\]", text or "", re.DOTALL)
    for blob in reversed(matches):
        try:
            data = json.loads(blob)
            if isinstance(data, list):
                return data
        except Exception:
            continue
    return []


def scan(trade_date: str, n: int = 8, model: str = "grok-4.3") -> Tuple[List[dict], float]:
    """Run the live-search scan. Returns (candidates, cost_usd)."""
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["XAI_API_KEY"], base_url="https://api.x.ai/v1")
    resp = client.responses.create(
        model=model,
        input=[{"role": "system", "content": _SYSTEM},
               {"role": "user", "content": _user_prompt(trade_date, n)}],
        tools=[{"type": "web_search"}, {"type": "x_search"}],
    )
    text = getattr(resp, "output_text", "") or ""
    candidates = _extract_json(text)
    # normalise + clamp
    out = []
    for c in candidates:
        tkr = str(c.get("ticker", "")).upper().strip().lstrip("$")
        if not tkr or not tkr.isalpha():
            continue
        out.append({
            "ticker": tkr,
            "company": c.get("company"),
            "price": c.get("price"),
            "thesis": c.get("thesis"),
            "catalyst": c.get("catalyst"),
            "timeframe": c.get("timeframe"),
            "conviction": max(0, min(100, int(c.get("conviction", 50) or 50))),
            "sources": c.get("sources", []),
        })
    out.sort(key=lambda c: -c["conviction"])

    u = getattr(resp, "usage", None)
    ticks = getattr(u, "cost_in_usd_ticks", 0) if u else 0
    cost = (ticks or 0) / 1e9  # ticks are nano-USD
    return out, cost


def _default_path() -> str:
    return os.path.join(os.path.expanduser("~/.tradingagents/bridge"), "speculative.json")


def load_speculative(path: Optional[str] = None) -> List[dict]:
    """Load the current week's speculative candidates (empty if none/stale)."""
    try:
        with open(path or _default_path(), "r", encoding="utf-8") as f:
            return json.load(f).get("candidates", [])
    except Exception:
        return []


def speculative_tickers(path: Optional[str] = None) -> List[str]:
    return [c["ticker"].upper() for c in load_speculative(path) if c.get("ticker")]


def main() -> None:
    from .config import BridgeConfig

    ap = argparse.ArgumentParser(description="Weekly speculative stock scanner (Grok live search).")
    ap.add_argument("--save", help="output JSON path (default: <state>/speculative.json)")
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()

    cfg = BridgeConfig.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    candidates, cost = scan(args.date, args.n)
    logger.info("scanner: %d candidates, est ~$%.3f", len(candidates), cost)

    record = {"ts": datetime.now(timezone.utc).isoformat(), "date": args.date,
              "cost_usd": round(cost, 4), "candidates": candidates}
    out = args.save or os.path.join(cfg.state_dir, "speculative.json")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    # archive too
    arch = os.path.join(cfg.state_dir, "speculative", f"speculative-{args.date}.json")
    os.makedirs(os.path.dirname(arch), exist_ok=True)
    with open(arch, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    for c in candidates:
        logger.info("  %-6s conv %3d  ~$%-7s %s", c["ticker"], c["conviction"],
                    c.get("price"), (c.get("catalyst") or "")[:60])


if __name__ == "__main__":
    main()
