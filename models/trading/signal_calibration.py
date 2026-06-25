"""
Signal accuracy feedback loop.

Queries closed intraday_trades to compute per-bucket win rates, time-of-day
accuracy, and empirical score → win rate mapping.

Zero closed trades → all functions return safe empty results.
5–20 trades      → directional hints only (note: "preliminary").
50+ trades       → meaningful calibration.
100+ trades      → Kelly sizing unlocks (ml_layer.py MIN_SAMPLES).

Never falls back to the (score+100)/200 heuristic — if data is insufficient
for a bucket, calibrated_win_rate returns None and the caller must decide.
"""
from __future__ import annotations
import math
from typing import Optional

# Score bucket boundaries for composite score grouping
_BUCKETS: list[tuple[int, int, str]] = [
    (-100, -60, "Strong Sell"),
    (-60,  -20, "Sell"),
    (-20,   20, "Neutral"),
    ( 20,   60, "Buy"),
    ( 60,  101, "Strong Buy"),   # 101 so score=100 is captured
]


def _is_winner(t: dict) -> bool:
    """True if trade was profitable. Uses pnl_r when available."""
    if t.get("pnl_r") is not None:
        return t["pnl_r"] > 0
    if t.get("pnl_dollars") is not None:
        return t["pnl_dollars"] > 0
    return False


def _load_closed_trades() -> list[dict]:
    """All closed trades (exit_price IS NOT NULL), newest first."""
    try:
        from db.database import get_db
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT entry_score, pnl_r, pnl_dollars, pnl_pct,
                       time_of_day_label, side, symbol, exit_reason,
                       liquidity_label, relative_volume, is_hypothetical
                FROM intraday_trades
                WHERE exit_price IS NOT NULL
                ORDER BY logged_at DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def score_accuracy_report(min_trades: int = 5) -> dict:
    """
    Win rate and avg P&L (in R) per composite score bucket.

    Returns:
        total_closed     — number of closed trades
        overall_win_rate — across all buckets (None if < min_trades)
        avg_pnl_r        — mean P&L in R units
        buckets          — list of per-bucket stats
        note             — calibration quality flag
    """
    trades = _load_closed_trades()
    n_total = len(trades)

    if n_total == 0:
        return {
            "total_closed": 0, "overall_win_rate": None,
            "avg_pnl_r": None, "buckets": [],
            "note": "No closed trades yet — run paper trades first",
        }

    buckets = []
    for lo, hi, label in _BUCKETS:
        bt = [t for t in trades
              if t.get("entry_score") is not None and lo <= t["entry_score"] < hi]
        n = len(bt)
        if n < min_trades:
            buckets.append({
                "label": label, "min_score": lo, "max_score": hi,
                "n": n, "win_rate": None, "avg_pnl_r": None,
                "note": f"Only {n} trade(s) — need {min_trades}",
            })
            continue
        wins   = sum(1 for t in bt if _is_winner(t))
        pnl_rs = [t["pnl_r"] for t in bt if t.get("pnl_r") is not None]
        buckets.append({
            "label":     label,
            "min_score": lo,
            "max_score": hi,
            "n":         n,
            "win_rate":  round(wins / n, 3),
            "avg_pnl_r": round(sum(pnl_rs) / len(pnl_rs), 3) if pnl_rs else None,
        })

    overall_wins   = sum(1 for t in trades if _is_winner(t))
    overall_wr     = round(overall_wins / n_total, 3) if n_total >= min_trades else None
    all_pnl_r      = [t["pnl_r"] for t in trades if t.get("pnl_r") is not None]
    overall_pnl_r  = round(sum(all_pnl_r) / len(all_pnl_r), 3) if all_pnl_r else None

    note = (
        "Calibrated (100+ trades)" if n_total >= 100
        else "Stable (50+ trades)" if n_total >= 50
        else "Preliminary — 50+ trades needed for stable calibration"
    )

    return {
        "total_closed":     n_total,
        "overall_win_rate": overall_wr,
        "avg_pnl_r":        overall_pnl_r,
        "buckets":          buckets,
        "note":             note,
    }


def time_accuracy_report(min_trades: int = 5) -> dict:
    """Win rate and avg P&L per time-of-day session."""
    trades = _load_closed_trades()
    sessions: dict[str, list[dict]] = {}
    for t in trades:
        key = t.get("time_of_day_label") or "UNKNOWN"
        sessions.setdefault(key, []).append(t)

    rows = []
    for label, st in sessions.items():
        n      = len(st)
        wins   = sum(1 for t in st if _is_winner(t))
        pnl_rs = [t["pnl_r"] for t in st if t.get("pnl_r") is not None]
        rows.append({
            "session":   label,
            "n":         n,
            "win_rate":  round(wins / n, 3) if n >= min_trades else None,
            "avg_pnl_r": round(sum(pnl_rs) / len(pnl_rs), 3) if len(pnl_rs) >= min_trades else None,
            "note":      f"Need {min_trades} trades" if n < min_trades else None,
        })

    return {
        "total_closed": len(trades),
        "by_session":   sorted(rows, key=lambda x: -(x["n"])),
    }


def calibrated_win_rate(score: float) -> Optional[float]:
    """
    Empirical win rate for a composite score value.
    Returns None when the score's bucket has insufficient data.
    Does NOT fall back to any formula — None means "unknown."
    """
    report = score_accuracy_report(min_trades=10)
    for b in report["buckets"]:
        if b["min_score"] <= score < b["max_score"]:
            return b.get("win_rate")  # None if insufficient data for this bucket
    return None


def calibration_summary() -> dict:
    """
    One-call readiness check: trade count, Kelly eligibility, empirical score
    threshold (lowest score bucket with win_rate > 50%).
    """
    trades = _load_closed_trades()
    n = len(trades)

    report = score_accuracy_report(min_trades=5)

    # Lowest score threshold with confirmed >50% win rate
    profitable_buckets = [
        b for b in report["buckets"]
        if b.get("win_rate") is not None and b["win_rate"] > 0.50
    ]
    threshold = min((b["min_score"] for b in profitable_buckets), default=None)

    return {
        "total_closed_trades":   n,
        "kelly_ready":           n >= 50,
        "calibration_quality":   (
            "calibrated"   if n >= 100
            else "stable"  if n >= 50
            else "preliminary" if n >= 20
            else "insufficient"
        ),
        "empirical_score_threshold":  threshold,
        "recommended_min_score":      threshold if threshold is not None else 60,
        "overall_win_rate":           report.get("overall_win_rate"),
        "avg_pnl_r":                  report.get("avg_pnl_r"),
        "note":                       report.get("note"),
    }
