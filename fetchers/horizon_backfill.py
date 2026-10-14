"""
Horizon Forecast (2026-10-14): multi-year daily-bar backfill into
market_daily_bars (same table the daily recorder fills). Reuses the recorder's
Alpaca fetch; INSERT OR REPLACE, so safe to re-run. Today's partial bar skipped.
Survivorship note: the symbol list is whatever is tradable now.
"""
from __future__ import annotations
import logging
from datetime import timedelta
from typing import Optional

from db.database import get_db
from fetchers.market_data_recorder import _fetch_chunk, _universe, CHUNK

log = logging.getLogger("horizon_backfill")


def backfill(years: int = 3, symbols: Optional[list[str]] = None) -> dict:
    from fetchers.high_value_runner import _et_now
    now = _et_now()
    today = now.strftime("%Y-%m-%d")
    start = (now - timedelta(days=int(365 * years))).strftime("%Y-%m-%d")
    syms = symbols if symbols is not None else _universe()
    rows = with_data = 0
    for i in range(0, len(syms), CHUNK):
        got = _fetch_chunk(syms[i:i + CHUNK], start)
        batch = []
        for sym, bars in got.items():
            kept = 0
            for b in bars:
                d = (b.get("t") or "")[:10]
                if d and d != today:
                    batch.append((sym, d, b.get("o"), b.get("h"), b.get("l"), b.get("c"), b.get("v"), b.get("vw")))
                    kept += 1
            with_data += 1 if kept else 0
        if batch:
            with get_db() as conn:
                conn.executemany("INSERT OR REPLACE INTO market_daily_bars "
                                 "(symbol, bar_date, open, high, low, close, volume, vwap) VALUES (?,?,?,?,?,?,?,?)", batch)
            rows += len(batch)
    summary = {"start": start, "symbols_requested": len(syms), "symbols_with_data": with_data, "rows_written": rows}
    log.info(f"[HORIZON-BACKFILL] {summary}")
    return summary
