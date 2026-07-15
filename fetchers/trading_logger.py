"""
Trade lifecycle logger for intraday paper trading.

Two-phase logging:
  log_trade_entry  — inserts record at entry time, returns trade_id
  log_trade_exit   — updates record at close, computes P&L + adjusted P&L

adjusted_pnl subtracts half-spread cost on both legs to estimate live performance.
Paper fills (Alpaca) are optimistic by ~spread/2 per leg vs real market orders.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional

from db.database import get_db


def _extract_signal_scores(signals: dict) -> dict:
    """
    Pull per-signal scores + ngram out of a compute_intraday_signals() result.
    Returns a flat dict of column values, all defaulting to None if missing.
    """
    s = (signals.get("signals") or {})
    score = (signals.get("score") or {})
    ngram = (s.get("ngram") or {})
    return {
        "composite_raw":    score.get("composite_raw"),
        "vwap_score":       (s.get("vwap") or {}).get("score"),
        "or_score":         (s.get("or") or {}).get("score"),
        "rsi_score":        (s.get("rsi") or {}).get("score"),
        "relvol_score":     (s.get("relvol") or {}).get("score"),
        "gap_score":        (s.get("gap") or {}).get("score"),
        "trend_score":      (s.get("trend") or {}).get("score"),
        "bollinger_score":  (s.get("bollinger") or {}).get("score"),
        "volsurge_score":   (s.get("volsurge") or {}).get("score"),
        "ngram_signal":     ngram.get("signal", "NONE"),
        "ngram_confidence": ngram.get("confidence"),
    }


def log_trade_entry(
    symbol: str,
    side: str,
    entry_price: float,
    qty: float,
    position_value: float,
    entry_score: float,
    time_of_day_label: str,
    spread_pct: float,
    planned_hold_bars: int,
    stop_price: float,
    target_price: float,
    risk_dollars: float,
    alpaca_order_id: Optional[str] = None,
    entry_time: Optional[str] = None,
    signal_scores: Optional[dict] = None,
) -> int:
    """
    Insert a new open trade record. exit_time and exit_price remain NULL
    until log_trade_exit is called.
    signal_scores: optional dict from _extract_signal_scores() or a raw
                   compute_intraday_signals() result — per-signal scores stored
                   for calibration feedback loop.
    Returns the trade_id (row id).
    """
    if entry_time is None:
        entry_time = datetime.now(timezone.utc).isoformat()

    ss = _extract_signal_scores(signal_scores) if signal_scores else {}

    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO intraday_trades (
                symbol, entry_time, side,
                entry_price, qty, position_value,
                entry_score, time_of_day_label,
                spread_pct_at_entry, planned_hold_bars,
                stop_price, target_price, risk_dollars,
                alpaca_order_id,
                composite_raw, vwap_score, or_score, rsi_score,
                relvol_score, gap_score, trend_score, bollinger_score,
                volsurge_score, ngram_signal, ngram_confidence,
                logged_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                datetime('now')
            )
            """,
            (
                symbol, entry_time, side,
                entry_price, qty, position_value,
                entry_score, time_of_day_label,
                spread_pct, planned_hold_bars,
                stop_price, target_price, risk_dollars,
                alpaca_order_id,
                ss.get("composite_raw"), ss.get("vwap_score"), ss.get("or_score"),
                ss.get("rsi_score"), ss.get("relvol_score"), ss.get("gap_score"),
                ss.get("trend_score"), ss.get("bollinger_score"), ss.get("volsurge_score"),
                ss.get("ngram_signal"), ss.get("ngram_confidence"),
            ),
        )
        return cur.lastrowid


def log_trade_exit(
    trade_id: int,
    exit_price: float,
    exit_reason: str,
    actual_hold_bars: Optional[int] = None,
    slippage_exit: float = 0.0,
    exit_time: Optional[str] = None,
) -> dict:
    """
    Close an open trade record with exit data. Computes:
      pnl_dollars:  raw dollar P&L (exit_price - entry_price) × qty
      pnl_pct:      raw % return per share
      pnl_r:        P&L expressed as multiples of initial risk
      adjusted_pnl: pnl_dollars minus estimated half-spread cost on both entry and exit
                    legs — conservative proxy for live execution quality.
    Returns the computed metrics dict.
    """
    if exit_time is None:
        exit_time = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM intraday_trades WHERE id = ?", (trade_id,)
        ).fetchone()
        if not row:
            return {"error": f"Trade {trade_id} not found"}

        entry_price    = row["entry_price"]
        side           = row["side"]
        qty            = row["qty"] or 1.0
        position_value = row["position_value"] or (entry_price * qty)
        risk_dollars   = row["risk_dollars"] or 0.0
        spread_pct     = row["spread_pct_at_entry"] or 0.0

        # Raw P&L
        per_share  = (exit_price - entry_price) if side == "long" else (entry_price - exit_price)
        pnl_dollars = round(per_share * qty, 4)
        pnl_pct     = round(per_share / entry_price * 100, 4) if entry_price else 0.0
        pnl_r       = round(pnl_dollars / risk_dollars, 3) if risk_dollars else None

        # Adjusted P&L: subtract half-spread cost on both legs (entry + exit)
        # half_spread × position_value × 2 legs
        slippage_cost = round((spread_pct / 2 / 100) * position_value * 2, 4)
        adjusted_pnl  = round(pnl_dollars - slippage_cost, 4)

        conn.execute(
            """
            UPDATE intraday_trades SET
                exit_time = ?, exit_price = ?,
                exit_reason = ?, actual_hold_bars = ?,
                slippage_exit = ?,
                pnl_dollars = ?, pnl_pct = ?,
                pnl_r = ?, adjusted_pnl = ?
            WHERE id = ?
            """,
            (
                exit_time, exit_price,
                exit_reason, actual_hold_bars,
                slippage_exit,
                pnl_dollars, pnl_pct,
                pnl_r, adjusted_pnl,
                trade_id,
            ),
        )

    return {
        "trade_id":      trade_id,
        "exit_price":    exit_price,
        "exit_reason":   exit_reason,
        "pnl_dollars":   pnl_dollars,
        "pnl_pct":       pnl_pct,
        "pnl_r":         pnl_r,
        "adjusted_pnl":  adjusted_pnl,
        "slippage_cost": slippage_cost,
    }


def get_trade_stats(days: int = 7) -> dict:
    """
    Aggregated paper trading performance for the last N days (closed trades only).
    Includes breakdown by time-of-day label and exit reason.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM intraday_trades
            WHERE entry_time >= datetime('now', ? || ' days')
            AND exit_time IS NOT NULL
            ORDER BY entry_time DESC
            """,
            (f"-{days}",),
        ).fetchall()

    if not rows:
        return {"message": f"No closed trades in last {days} days", "total_trades": 0}

    trades    = [dict(r) for r in rows]
    n         = len(trades)
    total_pnl = sum(t["pnl_dollars"] or 0 for t in trades)
    adj_pnl   = sum(t.get("adjusted_pnl") or t["pnl_dollars"] or 0 for t in trades)
    wins      = sum(1 for t in trades if (t["pnl_dollars"] or 0) > 0)
    avg_r     = None
    r_vals    = [t["pnl_r"] for t in trades if t.get("pnl_r") is not None]
    if r_vals:
        avg_r = round(sum(r_vals) / len(r_vals), 3)

    # Time-of-day breakdown
    tod_stats: dict = {}
    for label in ("MORNING_TREND", "AFTERNOON_TREND", "OPEN_NOISE", "CLOSE_REVERSAL", "LUNCH_CHOP"):
        subset = [t for t in trades if t.get("time_of_day_label") == label]
        if subset:
            s_wins = sum(1 for t in subset if (t["pnl_dollars"] or 0) > 0)
            tod_stats[label] = {
                "n":        len(subset),
                "win_rate": round(s_wins / len(subset), 3),
                "avg_pnl":  round(sum(t["pnl_dollars"] or 0 for t in subset) / len(subset), 2),
                "avg_r":    round(
                    sum(t["pnl_r"] for t in subset if t.get("pnl_r") is not None)
                    / max(1, sum(1 for t in subset if t.get("pnl_r") is not None)), 3
                ),
            }

    # Exit reason breakdown
    reason_counts: dict = {}
    for t in trades:
        r = t.get("exit_reason") or "UNKNOWN"
        reason_counts[r] = reason_counts.get(r, 0) + 1

    return {
        "period_days":       days,
        "total_trades":      n,
        "win_rate":          round(wins / n, 3) if n else 0,
        "total_pnl":         round(total_pnl, 2),
        "adjusted_pnl":      round(adj_pnl, 2),
        "avg_pnl_per_trade": round(total_pnl / n, 2) if n else 0,
        "avg_r":             avg_r,
        "by_time_of_day":    tod_stats,
        "by_exit_reason":    reason_counts,
        "recent_trades": [
            {
                "symbol":         t["symbol"],
                "side":           t["side"],
                "entry_time":     t["entry_time"],
                "exit_time":      t["exit_time"],
                "pnl":            t["pnl_dollars"],
                "pnl_r":          t.get("pnl_r"),
                "adjusted_pnl":   t.get("adjusted_pnl"),
                "exit_reason":    t["exit_reason"],
                "tod_label":      t.get("time_of_day_label"),
                "is_hypothetical": t.get("is_hypothetical", 0),
            }
            for t in trades[:10]
        ],
    }


def log_hypothetical_trade(
    symbol: str,
    side: str,
    score_value: float,
    signals: dict,
    levels: dict,
    hold_bars: int = 6,
    model_version: str = "v3",
    regime_tags: Optional[dict] = None,
) -> int:
    """
    Log what WOULD have happened without placing an Alpaca order.
    Used for Week 1-2 dry-run validation: signal quality without execution noise.

    Captures the full signal context so outcomes can be compared to what the
    model predicted — critical for validating direction, stop distance, and ToD bias
    before any capital is at risk.

    regime_tags: optional dict from paper_runner's market-regime gate — keys
    spy_gap_pct, xlk_change_pct, regime ("NORMAL"/"EXTREME"), spy_realized_vol_pct,
    market_vol_regime ("LOW"/"NORMAL"/"HIGH"), macro_event_today (bool, FOMC
    decision day). Logged only, for post-hoc analysis of which market
    conditions produced which outcomes (Kimi review) — never fed back into
    live scoring.

    Resolve outcomes via log_trade_exit() when stop/target/time exit would have hit.
    """
    entry_time = datetime.now(timezone.utc).isoformat()

    liq    = signals.get("liquidity", {})
    em     = signals.get("intraday_expected_move", {})
    relvol = (signals.get("signals") or {}).get("relvol", {})
    ss     = _extract_signal_scores(signals)
    rt     = regime_tags or {}

    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO intraday_trades (
                symbol, side, entry_time, planned_hold_bars,
                entry_price, theoretical_entry,
                stop_price, target1_price, target_price,
                qty, position_value,
                risk_dollars, entry_score,
                spread_pct_at_entry, liquidity_label, time_of_day_label,
                intraday_vol, relative_volume,
                model_version, is_hypothetical, notes,
                composite_raw, vwap_score, or_score, rsi_score,
                relvol_score, gap_score, trend_score, bollinger_score,
                volsurge_score, ngram_signal, ngram_confidence,
                spy_gap_pct, xlk_change_pct, market_regime,
                spy_realized_vol_pct, market_vol_regime, macro_event_today,
                logged_at
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                datetime('now')
            )
            """,
            (
                symbol, side, entry_time, hold_bars,
                levels.get("entry"), levels.get("entry"),
                levels.get("stop"), levels.get("target1"), levels.get("target2"),
                levels.get("shares"), levels.get("position_value"),
                levels.get("risk_dollars"), score_value,
                liq.get("spread_pct", 0.0), liq.get("label", "UNKNOWN"),
                (signals.get("score") or {}).get("time_label", "UNKNOWN"),
                em.get("bar_vol_pct"),
                relvol.get("rel_vol"),
                model_version, 1,
                "HYPOTHETICAL: no order placed",
                ss.get("composite_raw"), ss.get("vwap_score"), ss.get("or_score"),
                ss.get("rsi_score"), ss.get("relvol_score"), ss.get("gap_score"),
                ss.get("trend_score"), ss.get("bollinger_score"), ss.get("volsurge_score"),
                ss.get("ngram_signal"), ss.get("ngram_confidence"),
                rt.get("spy_gap_pct"), rt.get("xlk_change_pct"), rt.get("regime"),
                rt.get("spy_realized_vol_pct"), rt.get("market_vol_regime"),
                int(rt["macro_event_today"]) if rt.get("macro_event_today") is not None else None,
            ),
        )
        return cur.lastrowid


def log_low_value_trade(
    symbol: str,
    side: str,
    entry_price: float,
    score_value: float,
    thesis_result: dict,
    thesis_type: str,
    news_result: Optional[dict] = None,
    hold_days: int = 5,
    model_version: str = "v1",
) -> int:
    """
    Log a Low Value (sub-$20 contrarian) hypothetical trade — is_hypothetical=1,
    engine='low_value', same intraday_trades table as High Value but a
    completely separate signal pipeline (Kimi review, round 6 follow-up).

    Position sizing is fixed $25/trade (models.trading.shared.kelly.
    low_value_position_size), not the ATR/Kelly sizing High Value uses.
    planned_hold_bars stores the 1-5 TRADING-DAY hold window (not 5-min bars
    — Low Value checks once daily, per spec), and stop/target1 store the
    -50%/+50% exit levels the runner checks against daily closes.

    thesis_result: models.trading.low_value.thesis_tracker.compute_thesis_score()
    output. thesis_type: dominant_thesis_type() output, stored for per-thesis
    win-rate calibration. news_result: news_overlay.score_symbol_news() output
    or None.

    Also logs (Kimi review, round-2 follow-up): lv_missing_signals — the
    signals compute_thesis_score() couldn't compute for this symbol, so a
    post-hoc query can check "did missing-data trades underperform" without
    guessing; lv_short_interest_asof — the settlement date behind
    short_interest_pct, so staleness is always visible.
    """
    import json
    from models.trading.shared.kelly import low_value_position_size

    entry_time = datetime.now(timezone.utc).isoformat()
    sizing = low_value_position_size(entry_price)
    nr = news_result or {}
    missing_signals = thesis_result.get("missing_signals", [])
    si_detail = thesis_result.get("signals", {}).get("short_interest_pct", {}).get("detail", {})
    short_interest_asof = si_detail.get("short_interest_as_of")

    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO intraday_trades (
                symbol, side, entry_time, planned_hold_bars,
                entry_price, theoretical_entry,
                stop_price, target1_price, target_price,
                qty, position_value,
                entry_score, model_version, is_hypothetical, notes,
                engine, lv_thesis_type, lv_news_flags, lv_news_sentiment, lv_headline_count,
                lv_missing_signals, lv_short_interest_asof, lv_signals_json,
                logged_at
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                datetime('now')
            )
            """,
            (
                symbol, side, entry_time, hold_days,
                entry_price, entry_price,
                round(entry_price * 0.5, 4), round(entry_price * 1.5, 4), round(entry_price * 1.5, 4),
                sizing.get("shares"), sizing.get("position_size"),
                score_value, model_version, 1,
                "HYPOTHETICAL: Low Value engine, no order placed",
                "low_value", thesis_type,
                json.dumps(nr.get("flags", [])), nr.get("sentiment"), nr.get("headline_count"),
                json.dumps(missing_signals), short_interest_asof, json.dumps(thesis_result.get("signals", {})),
            ),
        )
        return cur.lastrowid


def log_universe_snapshot(scan_date: str, symbols: list[str], filter_stats: Optional[dict] = None) -> int:
    """
    Logs the Low Value universe scanner's daily output to
    low_value_universe_snapshot (Kimi review, round 6 follow-up spec:
    "Log universe composition ... so composition drift/quality is auditable").

    filter_stats (Kimi review, 2026-07-07 follow-up): scanner.py's
    build_low_value_universe() stats dict — currently stagnant_filtered_count,
    how many price-filtered candidates the volatility floor excluded before
    any paid API call.
    """
    import json
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO low_value_universe_snapshot (scan_date, symbols_json, symbol_count, filter_stats_json, logged_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (scan_date, json.dumps(symbols), len(symbols), json.dumps(filter_stats or {})),
        )
        return cur.lastrowid


def get_universe_snapshots(days: int = 30) -> list[dict]:
    """Recent Low Value universe scans, newest first."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM low_value_universe_snapshot WHERE logged_at >= datetime('now', ? || ' days') "
            "ORDER BY logged_at DESC",
            (f"-{days}",),
        ).fetchall()
    return [dict(r) for r in rows]


def get_universe_snapshot_for_date(scan_date: str) -> Optional[dict]:
    """
    Most recent logged snapshot for an exact scan_date (YYYY-MM-DD, ET),
    or None. Added 2026-07-14 so fetchers/low_value_runner.py's
    get_daily_universe() can survive a process restart — its in-memory
    _universe_cache alone doesn't, and this repo's Railway deployment
    redeploys (restarting the process, wiping that dict) far more often
    than once a day, which made "build the universe once per day" not
    actually true in practice: a manual scan right after a redeploy looked
    like a brand new, different build even minutes after an earlier one on
    the same calendar day.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM low_value_universe_snapshot WHERE scan_date = ? ORDER BY logged_at DESC LIMIT 1",
            (scan_date,),
        ).fetchone()
    return dict(row) if row else None


def log_manual_override(action: str, symbol: Optional[str] = None, details: Optional[dict] = None) -> int:
    """
    Records any manual order placed outside the automated signal pipeline
    (Kimi review, round 5 — Jane Street's "never override the computer" rule
    applied structurally: overrides aren't forbidden, but they can't be silent).

    details is stored as a JSON string. Called from place_trade() (app.py)
    on every POST /trade/order — the only path that bypasses signal generation
    entirely and lets a human place an arbitrary order.
    """
    import json
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO manual_override_log (action, symbol, details, logged_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (action, symbol, json.dumps(details or {})),
        )
        return cur.lastrowid


def get_manual_overrides(days: int = 30) -> list[dict]:
    """Recent manual overrides for review — GET /trade/manual-overrides."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM manual_override_log WHERE logged_at >= datetime('now', ? || ' days') "
            "ORDER BY logged_at DESC",
            (f"-{days}",),
        ).fetchall()
    return [dict(r) for r in rows]
