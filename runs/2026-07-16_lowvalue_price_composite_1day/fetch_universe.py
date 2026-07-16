"""
Snapshot today's REAL Low Value universe + sector-ETF mapping, once, using the
actual production scanner (models/trading/low_value/scanner.py) and signal
engine (models/trading/low_value/thesis_tracker.py) — not a reimplementation.

Writes universe.json and sector_map.json into this run folder. run.py then
holds both fixed for the whole historical backtest window (see
strategy_spec.json's "universe" and "sector_etf_mapping" sections for why).

Requires the same env vars as the live Predicta app: ALPACA_API_KEY,
ALPACA_SECRET_KEY (required), FINNHUB_API_KEY (optional, improves market-cap
and sector-mapping coverage), SEC_EDGAR_USER_AGENT (required by SEC's fair-
access policy for the bankruptcy-filing check).

Run from the Predicta repo root:
    python runs/2026-07-16_lowvalue_price_composite_1day/fetch_universe.py
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from models.trading.low_value.scanner import build_low_value_universe
from models.trading.low_value.thesis_tracker import sector_etf_for_symbol

OUT_DIR = Path(__file__).resolve().parent


def main() -> None:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"[fetch_universe] scanning today's live Low Value universe ({today_str})...")
    universe, stats = build_low_value_universe(today_str)
    print(f"[fetch_universe] {len(universe)} symbols qualified. stage breakdown: {json.dumps(stats)}")

    if not universe:
        print("[fetch_universe] WARNING: empty universe — check FINNHUB_API_KEY / SEC_EDGAR_USER_AGENT "
              "/ ALPACA credentials before trusting this as a real 'no qualifying stocks' result "
              "(see CLAUDE.md's Finnhub-key incident for how a silent auth failure can look identical "
              "to a real empty result).")

    sector_map: dict[str, str] = {}
    for i, sym in enumerate(universe):
        sector_map[sym] = sector_etf_for_symbol(sym)
        if (i + 1) % 20 == 0:
            print(f"[fetch_universe] sector-mapped {i + 1}/{len(universe)}")

    universe_path = OUT_DIR / "universe.json"
    sector_map_path = OUT_DIR / "sector_map.json"
    universe_path.write_text(json.dumps({
        "snapshot_date": today_str,
        "symbols": universe,
        "scan_stats": stats,
    }, indent=2))
    sector_map_path.write_text(json.dumps(sector_map, indent=2))

    print(f"[fetch_universe] wrote {universe_path}")
    print(f"[fetch_universe] wrote {sector_map_path}")
    print("[fetch_universe] next: python run.py --universe-file universe.json --sector-map sector_map.json "
          "--start <START> --end <END>")


if __name__ == "__main__":
    main()
