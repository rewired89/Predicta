"""
Market-data recorder (2026-10-08, direct user request).

Records daily OHLCV bars for the whole tracked universe every trading day,
independent of whether any engine opened a trade. Trade-based calibration is
slot-capped (Low Value ~13 trades/month, Automaton ~10/year); this table is
the uncapped sample. Bars are point-in-time and dated, so any signal can be
recomputed later and joined to forward returns (see forward_returns()).

Universe = Low Value daily universe + High Value watchlist + benchmark ETFs.
Today's bar is skipped until 16:30 ET so a partial bar is never stored.
Idempotent: INSERT OR REPLACE per (symbol, bar_date).
"""
from __future__ import annotations
import logging
from datetime import timedelta
from typing import Optional

import requests

from db.database import get_db
from fetchers.alpaca import DATA_BASE_URL, _headers

log = logging.getLogger("market_data_recorder")

BENCHMARK_SYMBOLS = ["SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]
LOOKBACK_DAYS = 60       # first run backfills this far; later runs just refresh the window
CHUNK = 50
MAX_PAGES = 20


def _universe() -> list[str]:
    syms: list[str] = list(BENCHMARK_SYMBOLS)
    try:
        from fetchers.low_value_runner import get_daily_universe
        syms += get_daily_universe(force_refresh=False)
    except Exception as exc:
        log.warning(f"[BARS] Low Value universe unavailable: {exc}")
    try:
        from fetchers.high_value_runner import get_active_watchlist
        syms += get_active_watchlist()
    except Exception as exc:
        log.warning(f"[BARS] High Value watchlist unavailable: {exc}")
    return sorted({s for s in syms if s})


def _fetch_chunk(symbols: list[str], start: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    token: Optional[str] = None
    for _ in range(MAX_PAGES):
        params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start,
                  "limit": 10000, "feed": "iex", "adjustment": "split", "sort": "asc"}
        if token:
            params["page_token"] = token
        try:
            r = requests.get(f"{DATA_BASE_URL}/v2/stocks/bars", headers=_headers(), params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            log.warning(f"[BARS] chunk fetch failed ({len(symbols)} symbols): {exc}")
            break
        for sym, bars in (data.get("bars") or {}).items():
            out.setdefault(sym, []).extend(bars or [])
        token = data.get("next_page_token")
        if not token:
            break
    return out


def record_daily_bars(symbols: Optional[list[str]] = None) -> dict:
    """Fetch + store daily bars for the universe. Returns a summary dict and writes market_bars_log."""
    from fetchers.high_value_runner import _et_now
    now = _et_now()
    today = now.strftime("%Y-%m-%d")
    final_today = now.hour * 60 + now.minute >= 16 * 60 + 30
    syms = symbols if symbols is not None else _universe()
    start = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    rows = 0
    with_data = 0
    for i in range(0, len(syms), CHUNK):
        got = _fetch_chunk(syms[i:i + CHUNK], start)
        batch = []
        for sym, bars in got.items():
            kept = 0
            for b in bars:
                d = (b.get("t") or "")[:10]
                if not d or (d == today and not final_today):
                    continue
                batch.append((sym, d, b.get("o"), b.get("h"), b.get("l"), b.get("c"), b.get("v"), b.get("vw")))
                kept += 1
            if kept:
                with_data += 1
        if batch:
            with get_db() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO market_daily_bars (symbol, bar_date, open, high, low, close, volume, vwap) "
                    "VALUES (?,?,?,?,?,?,?,?)", batch)
            rows += len(batch)

    summary = {"run_date": today, "symbols_requested": len(syms), "symbols_with_data": with_data, "rows_written": rows}
    # Only mark the day done when data actually came back, so a dead API key
    # or outage retries on the next tick instead of looking like a clean run.
    if rows > 0:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO market_bars_log (run_date, symbols_requested, symbols_with_data, rows_written, completed_at) "
                "VALUES (?,?,?,?,datetime('now')) ON CONFLICT(run_date) DO UPDATE SET "
                "symbols_requested=excluded.symbols_requested, symbols_with_data=excluded.symbols_with_data, "
                "rows_written=excluded.rows_written, completed_at=excluded.completed_at",
                (today, len(syms), with_data, rows))
    log.info(f"[BARS] {summary}")
    return summary


def recorded_today() -> bool:
    from fetchers.high_value_runner import _et_now
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM market_bars_log WHERE run_date = ?",
                            (_et_now().strftime("%Y-%m-%d"),)).fetchone() is not None


def coverage() -> dict:
    with get_db() as conn:
        tot = conn.execute("SELECT COUNT(*), COUNT(DISTINCT symbol), COUNT(DISTINCT bar_date), MIN(bar_date), MAX(bar_date) FROM market_daily_bars").fetchone()
        logs = conn.execute("SELECT * FROM market_bars_log ORDER BY run_date DESC LIMIT 14").fetchall()
    return {"rows": tot[0], "symbols": tot[1], "days": tot[2], "first_date": tot[3], "last_date": tot[4],
            "recent_runs": [dict(r) for r in logs]}


def forward_returns(horizons: tuple[int, ...] = (1, 5, 20), symbol: Optional[str] = None) -> list[dict]:
    """
    Per (symbol, bar_date): close and forward % return over each horizon (in
    trading days present in the table). Rows without enough future bars get
    None for that horizon. This is the join target for tuning signals.
    """
    with get_db() as conn:
        q = "SELECT symbol, bar_date, close FROM market_daily_bars"
        args: tuple = ()
        if symbol:
            q += " WHERE symbol = ?"
            args = (symbol,)
        rows = conn.execute(q + " ORDER BY symbol, bar_date", args).fetchall()
    out: list[dict] = []
    series: dict[str, list] = {}
    for r in rows:
        series.setdefault(r["symbol"], []).append((r["bar_date"], r["close"]))
    for sym, s in series.items():
        for i, (d, c) in enumerate(s):
            rec = {"symbol": sym, "bar_date": d, "close": c}
            for h in horizons:
                f = s[i + h][1] if i + h < len(s) else None
                rec[f"fwd_{h}d_pct"] = round((f / c - 1) * 100, 3) if f and c else None
            out.append(rec)
    return out
