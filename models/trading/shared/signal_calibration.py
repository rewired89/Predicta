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

# Per-signal DB column names (v4 schema)
_SIGNAL_COLS: dict[str, str] = {
    "vwap":     "vwap_score",
    "or":       "or_score",
    "rsi":      "rsi_score",
    "relvol":   "relvol_score",
    "gap":      "gap_score",
    "trend":    "trend_score",
    "bollinger":"bollinger_score",
    "volsurge": "volsurge_score",
}

# Static fallback weights (from intraday.py WEIGHTS) — used when dynamic
# weights can't be computed (insufficient data).
_STATIC_WEIGHTS: dict[str, float] = {
    "vwap": 0.20, "or": 0.15, "rsi": 0.15, "relvol": 0.10,
    "gap": 0.10,  "trend": 0.15, "bollinger": 0.10, "volsurge": 0.05,
}

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


def _load_closed_trades(engine: str = "high_value") -> list[dict]:
    """
    All closed trades (exit_price IS NOT NULL) for one engine, newest first.

    engine (Kimi review round 6 follow-up — Low Value contrarian engine):
    every calibration query is scoped to a single engine's rows so Low Value's
    thin, early data can never dilute or corrupt High Value's calibration
    stats (and vice versa). Defaults to 'high_value' so every pre-existing
    caller that doesn't pass engine behaves exactly as before.
    """
    try:
        from db.database import get_db
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT entry_score, pnl_r, pnl_dollars, pnl_pct,
                       time_of_day_label, side, symbol, exit_reason,
                       liquidity_label, relative_volume, is_hypothetical
                FROM intraday_trades
                WHERE exit_price IS NOT NULL AND engine = ?
                ORDER BY logged_at DESC
                """,
                (engine,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def score_accuracy_report(min_trades: int = 5, engine: str = "high_value") -> dict:
    """
    Win rate and avg P&L (in R) per composite score bucket.

    Returns:
        total_closed     — number of closed trades
        overall_win_rate — across all buckets (None if < min_trades)
        avg_pnl_r        — mean P&L in R units
        buckets          — list of per-bucket stats
        note             — calibration quality flag
    """
    trades = _load_closed_trades(engine=engine)
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


def time_accuracy_report(min_trades: int = 5, engine: str = "high_value") -> dict:
    """Win rate and avg P&L per time-of-day session."""
    trades = _load_closed_trades(engine=engine)
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


def calibrated_win_rate(score: float, engine: str = "high_value") -> Optional[float]:
    """
    Empirical win rate for a composite score value.
    Returns None when the score's bucket has insufficient data.
    Does NOT fall back to any formula — None means "unknown."
    """
    report = score_accuracy_report(min_trades=10, engine=engine)
    for b in report["buckets"]:
        if b["min_score"] <= score < b["max_score"]:
            return b.get("win_rate")  # None if insufficient data for this bucket
    return None


# Hard floor before ANY calibration output (empirical win rate, veto, dynamic
# weights) is allowed to influence a live decision (Kimi review, round 2): the
# base ensemble's edge hasn't been validated yet, so acting on 30-50 trade
# calibration data risks tuning noise rather than a real signal.
MIN_TRADES_FOR_ANY_CALIBRATION = 100


def calibration_globally_active(engine: str = "high_value") -> bool:
    """
    True once total closed trades >= MIN_TRADES_FOR_ANY_CALIBRATION (100).
    Callers (e.g. kelly_from_signals) must check this before using
    calibrated_win_rate() or veto_decision() for a real decision — below the
    floor, fall back to static behavior instead.
    """
    return len(_load_closed_trades(engine=engine)) >= MIN_TRADES_FOR_ANY_CALIBRATION


# Confidence level for veto_decision's CI test (Kimi review, round 3): named
# constant instead of a hardcoded default so tightening it later (e.g. to 0.90
# or 0.95 once 500+ trades exist) is a one-line change, not a re-audit of the
# veto function.
WILSON_CONFIDENCE = 0.80


def _wilson_ci(wins: int, n: int, confidence: float = WILSON_CONFIDENCE) -> tuple[float, float]:
    """
    Wilson score interval for a binomial proportion — much better small-sample
    behavior than a normal approximation, which is why the CI-gated veto below
    uses it instead of a raw point estimate.
    """
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    z = {0.80: 1.2816, 0.90: 1.6449, 0.95: 1.96}.get(confidence, 1.2816)
    denom  = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt((p * (1 - p) / n) + (z * z / (4 * n * n)))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def veto_decision(score: float, min_trades: int = 30, ci_floor: float = 0.48, engine: str = "high_value") -> dict:
    """
    Statistically-gated trade veto (Kimi review): replaces a naive point-estimate
    check (e.g. "win rate < 45%") with a WILSON_CONFIDENCE-level confidence-interval
    test, and refuses to veto at all below min_trades in the score's bucket.

    Rationale: a 45% observed win rate at n=15 could easily be a true 55% rate
    with bad variance, or a true 35% rate with good variance — vetoing on the
    point estimate alone is noise responding to noise. This only vetoes when
    the bucket's CI upper bound sits entirely below ci_floor, which in
    practice requires roughly 25-30+ trades in the bucket to ever trigger.

    Returns: {veto, reason, n, win_rate, ci_lower, ci_upper, min_trades}
    """
    trades = _load_closed_trades(engine=engine)
    bucket = next(
        ((lo, hi, label) for lo, hi, label in _BUCKETS if lo <= score < hi), None
    )
    if bucket is None:
        return {
            "veto": False, "reason": "score out of bucket range", "n": 0,
            "win_rate": None, "ci_lower": None, "ci_upper": None,
            "min_trades": min_trades,
        }

    lo, hi, label = bucket
    bt = [
        t for t in trades
        if t.get("entry_score") is not None and lo <= t["entry_score"] < hi
    ]
    n = len(bt)

    if n < min_trades:
        return {
            "veto": False,
            "reason": f"Only {n} trade(s) in '{label}' bucket — need {min_trades} before vetoing",
            "n": n, "win_rate": None, "ci_lower": None, "ci_upper": None,
            "min_trades": min_trades,
        }

    wins = sum(1 for t in bt if _is_winner(t))
    win_rate = round(wins / n, 3)
    ci_lower, ci_upper = _wilson_ci(wins, n, WILSON_CONFIDENCE)

    ci_pct = f"{WILSON_CONFIDENCE:.0%}"
    veto = ci_upper < ci_floor
    reason = (
        f"{ci_pct} CI upper bound {ci_upper:.1%} < {ci_floor:.0%} floor — edge statistically gone"
        if veto else
        f"{ci_pct} CI [{ci_lower:.1%}, {ci_upper:.1%}] does not confirm the edge is gone"
    )
    return {
        "veto": veto, "reason": reason, "n": n, "win_rate": win_rate,
        "ci_lower": round(ci_lower, 3), "ci_upper": round(ci_upper, 3),
        "min_trades": min_trades,
    }


def calibration_summary(engine: str = "high_value") -> dict:
    """
    One-call readiness check: trade count, Kelly eligibility, empirical score
    threshold (lowest score bucket with win_rate > 50%).
    """
    trades = _load_closed_trades(engine=engine)
    n = len(trades)

    report = score_accuracy_report(min_trades=5, engine=engine)

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


def pairs_calibration_summary(min_trades: int = 3) -> dict:
    """
    Per-pair realized edge from closed pair_signals rows.

    Queries pair_signals WHERE exit_time IS NOT NULL and computes:
      win_rate    — trades with pnl_pct > 0
      avg_pnl_pct — mean P&L percentage across closed trades
      avg_hold_h  — mean hold time in hours (created_at → exit_time)
      flag        — "UNDERPERFORMING" if win_rate < 0.50 or avg_pnl_pct < 0

    Returns a per-pair breakdown sorted by avg_pnl_pct descending.
    """
    from datetime import datetime as _dt

    try:
        from db.database import get_db
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT sym1, sym2, action, zscore, pnl_pct, created_at, exit_time
                FROM pair_signals
                WHERE exit_time IS NOT NULL
                ORDER BY created_at DESC
                """
            ).fetchall()
    except Exception:
        return {"total_closed": 0, "pairs": [], "note": "No closed pair trades yet"}

    rows = [dict(r) for r in rows]
    if not rows:
        return {"total_closed": 0, "pairs": [], "note": "No closed pair trades yet"}

    # Group by (sym1, sym2)
    groups: dict[tuple, list] = {}
    for r in rows:
        key = (r["sym1"], r["sym2"])
        groups.setdefault(key, []).append(r)

    pair_stats = []
    for (sym1, sym2), trades in groups.items():
        n = len(trades)
        pnl_vals = [t["pnl_pct"] for t in trades if t.get("pnl_pct") is not None]
        wins = sum(1 for p in pnl_vals if p > 0)
        win_rate = round(wins / len(pnl_vals), 3) if pnl_vals else None
        avg_pnl  = round(sum(pnl_vals) / len(pnl_vals), 3) if pnl_vals else None

        # Average hold time
        hold_hours = []
        for t in trades:
            try:
                t0 = _dt.fromisoformat(t["created_at"].replace("Z", "+00:00"))
                t1 = _dt.fromisoformat(t["exit_time"].replace("Z", "+00:00"))
                hold_hours.append((t1 - t0).total_seconds() / 3600)
            except Exception:
                pass
        avg_hold_h = round(sum(hold_hours) / len(hold_hours), 1) if hold_hours else None

        flag = None
        if win_rate is not None and n >= min_trades:
            if win_rate < 0.50 or (avg_pnl is not None and avg_pnl < 0):
                flag = "UNDERPERFORMING"

        pair_stats.append({
            "pair":       f"{sym1}/{sym2}",
            "n":          n,
            "win_rate":   win_rate,
            "avg_pnl_pct": avg_pnl,
            "avg_hold_h": avg_hold_h,
            "flag":       flag,
            "note":       f"Need {min_trades} trades" if n < min_trades else None,
        })

    pair_stats.sort(key=lambda x: (x.get("avg_pnl_pct") or -999), reverse=True)

    return {
        "total_closed": len(rows),
        "pairs":        pair_stats,
    }


def _load_closed_trades_full(engine: str = "high_value") -> list[dict]:
    """
    All closed trades with v4 per-signal score columns, scoped to one engine
    (these columns — vwap_score, or_score, etc. — are High Value's signal
    set; Low Value's per-thesis-type calibration uses thesis_type_calibration_report
    instead, since its 8 signals don't map to these columns).
    """
    try:
        from db.database import get_db
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT entry_score, pnl_r, pnl_dollars, pnl_pct,
                       time_of_day_label, side, symbol, exit_reason,
                       liquidity_label, relative_volume, is_hypothetical,
                       composite_raw,
                       vwap_score, or_score, rsi_score, relvol_score,
                       gap_score, trend_score, bollinger_score, volsurge_score,
                       ngram_signal, ngram_confidence
                FROM intraday_trades
                WHERE exit_price IS NOT NULL AND engine = ?
                ORDER BY logged_at DESC
                """,
                (engine,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _per_signal_stats(
    trades: list[dict],
    col: str,
    active_threshold: float = 10.0,
) -> dict:
    """
    Win rate and avg P&L for one signal column.
    Only counts trades where the signal was 'active' (|score| >= active_threshold).
    """
    active = [
        t for t in trades
        if t.get(col) is not None and abs(t[col]) >= active_threshold
    ]
    n = len(active)
    if n == 0:
        return {"n": 0, "win_rate": None, "avg_r": None}
    wins   = sum(1 for t in active if _is_winner(t))
    pnl_rs = [t["pnl_r"] for t in active if t.get("pnl_r") is not None]
    return {
        "n":        n,
        "win_rate": round(wins / n, 3),
        "avg_r":    round(sum(pnl_rs) / len(pnl_rs), 3) if pnl_rs else None,
    }


def per_signal_accuracy_report(
    min_trades: int = 10,
    active_threshold: float = 10.0,
) -> dict:
    """
    Win rate and average P&L (in R) per individual signal component.

    Only counts a trade for a signal when |signal_score| >= active_threshold,
    meaning the signal was contributing meaningfully to the composite.

    Requires v4 schema columns; trades logged before schema migration will
    have NULL values and are silently excluded from signal-level stats.
    """
    trades = _load_closed_trades_full()
    n_total = len(trades)

    by_signal = []
    for signal_key, col in _SIGNAL_COLS.items():
        stats = _per_signal_stats(trades, col, active_threshold)
        note = None
        if stats["n"] < min_trades:
            note = f"Only {stats['n']} active trade(s) — need {min_trades}"
        elif stats["win_rate"] is not None and stats["win_rate"] < 0.50:
            note = "Below 50% win rate — may be dragging composite score"
        by_signal.append({
            "signal":   signal_key,
            "column":   col,
            **stats,
            "note":     note,
        })

    by_signal.sort(key=lambda x: (x.get("win_rate") or -1), reverse=True)
    return {
        "total_closed": n_total,
        "by_signal":    by_signal,
        "active_threshold": active_threshold,
        "note": (
            "Per-signal data requires v4 schema; pre-migration trades excluded"
            if n_total > 0 else "No closed trades yet"
        ),
    }


def compute_dynamic_weights(min_trades: int = 30) -> Optional[dict]:
    """
    Compute empirically-driven signal weights from closed-trade data.

    Formula per signal:
        raw = max(0, (win_rate - 0.5) * avg_r)
    Normalized:
        weight = raw / sum(all raws)
    Falls back to _STATIC_WEIGHTS when sum of raws is zero (no signal shows edge).

    Returns None when total closed trades < min_trades (insufficient data).
    Returns dict with: weights, raw_weights, negative_utility, n_trades, status.
    """
    trades = _load_closed_trades_full()
    if len(trades) < min_trades:
        return None

    raw_weights: dict[str, float] = {}
    negative_utility: list[str]   = []

    for signal_key, col in _SIGNAL_COLS.items():
        stats = _per_signal_stats(trades, col, active_threshold=10.0)
        wr  = stats["win_rate"]
        avg = stats["avg_r"]
        if wr is None or avg is None or stats["n"] < 5:
            raw_weights[signal_key] = 0.0
            continue
        raw = max(0.0, (wr - 0.5) * avg)
        raw_weights[signal_key] = raw
        if wr < 0.50 or avg < 0:
            negative_utility.append(signal_key)

    total_raw = sum(raw_weights.values())
    if total_raw > 0:
        weights = {k: round(v / total_raw, 4) for k, v in raw_weights.items()}
        status  = "dynamic"
    else:
        weights = dict(_STATIC_WEIGHTS)
        status  = "fallback_static"

    return {
        "weights":          weights,
        "raw_weights":      {k: round(v, 6) for k, v in raw_weights.items()},
        "negative_utility": negative_utility,
        "n_trades":         len(trades),
        "status":           status,
    }


def compute_ngram_blend_weight(min_samples: int = 20) -> Optional[dict]:
    """
    Calibrate the n-gram overlay multiplier from closed-trade outcomes.

    Agree cohort:    n-gram direction matches composite_raw direction.
    Disagree cohort: n-gram direction opposes composite_raw direction.

    If the agree cohort meaningfully outperforms baseline avg_r, the n-gram
    signal is adding value and the agree_multiplier should be > 1.0.
    If the disagree cohort underperforms, disagree_multiplier should be < 1.0.

    Returns None when either cohort has fewer than min_samples trades.
    """
    trades = _load_closed_trades_full()

    agree    = []
    disagree = []
    for t in trades:
        ng  = t.get("ngram_signal")
        raw = t.get("composite_raw")
        if ng is None or raw is None or ng == "NONE":
            continue
        if (ng == "UP" and raw > 0) or (ng == "DOWN" and raw < 0):
            agree.append(t)
        elif (ng == "UP" and raw < 0) or (ng == "DOWN" and raw > 0):
            disagree.append(t)

    if len(agree) < min_samples or len(disagree) < min_samples:
        return None

    def _avg_r(cohort: list[dict]) -> Optional[float]:
        rs = [t["pnl_r"] for t in cohort if t.get("pnl_r") is not None]
        return round(sum(rs) / len(rs), 3) if rs else None

    all_rs = [t["pnl_r"] for t in trades if t.get("pnl_r") is not None]
    baseline_avg_r = round(sum(all_rs) / len(all_rs), 3) if all_rs else None

    agree_avg_r    = _avg_r(agree)
    disagree_avg_r = _avg_r(disagree)

    # Multiplier = ratio to baseline; clamp to [0.5, 2.0]
    def _mult(avg: Optional[float], base: Optional[float]) -> Optional[float]:
        if avg is None or base is None or base == 0:
            return None
        return round(max(0.5, min(2.0, avg / base)), 3)

    return {
        "agree_multiplier":   _mult(agree_avg_r, baseline_avg_r),
        "disagree_multiplier": _mult(disagree_avg_r, baseline_avg_r),
        "n_agree":            len(agree),
        "n_disagree":         len(disagree),
        "agree_avg_r":        agree_avg_r,
        "disagree_avg_r":     disagree_avg_r,
        "baseline_avg_r":     baseline_avg_r,
        "status":             "calibrated",
    }


# Signal kill switch (Kimi review, round 5 — Citadel "pod" model: small teams,
# tight risk limits, kill underperformers). Diagnostic + persisted flag only —
# does NOT itself zero out WEIGHTS in intraday.py; the live ensemble stays
# untouched until dynamic weights are actually wired to production, per the
# same "don't tune pre-data" principle applied everywhere else this session.
SIGNAL_KILL_MIN_TRADES     = 50
SIGNAL_KILL_CI_FLOOR       = 0.48
SIGNAL_KILL_RESURRECT_N    = 20


def _per_signal_wilson_ci(trades: list[dict], col: str, active_threshold: float = 10.0) -> dict:
    """Win rate + Wilson CI for one signal column, mirroring veto_decision's math."""
    active = [
        t for t in trades
        if t.get(col) is not None and abs(t[col]) >= active_threshold
    ]
    n = len(active)
    if n == 0:
        return {"n": 0, "win_rate": None, "ci_lower": None, "ci_upper": None}
    wins = sum(1 for t in active if _is_winner(t))
    win_rate = round(wins / n, 3)
    ci_lower, ci_upper = _wilson_ci(wins, n, WILSON_CONFIDENCE)
    return {
        "n": n, "win_rate": win_rate,
        "ci_lower": round(ci_lower, 3), "ci_upper": round(ci_upper, 3),
    }


def _persist_kill_switch(signal: str, stats: dict) -> None:
    """Upserts a kill record; no-op if already killed and not yet resurrected."""
    from db.database import get_db
    with get_db() as conn:
        existing = conn.execute(
            "SELECT resurrected FROM signal_kill_switches WHERE signal = ?", (signal,)
        ).fetchone()
        if existing and not existing[0]:
            return
        conn.execute(
            """
            INSERT INTO signal_kill_switches
                (signal, killed_at, n_trades_at_kill, win_rate_at_kill, ci_upper_at_kill, resurrected)
            VALUES (?, datetime('now'), ?, ?, ?, 0)
            ON CONFLICT(signal) DO UPDATE SET
                killed_at=excluded.killed_at, n_trades_at_kill=excluded.n_trades_at_kill,
                win_rate_at_kill=excluded.win_rate_at_kill, ci_upper_at_kill=excluded.ci_upper_at_kill,
                resurrected=0, resurrected_at=NULL
            """,
            (signal, stats["n"], stats["win_rate"], stats["ci_upper"]),
        )


def check_signal_kill_switches() -> dict:
    """
    After SIGNAL_KILL_MIN_TRADES (50) active trades for an individual signal,
    if its Wilson CI upper bound sits below SIGNAL_KILL_CI_FLOOR (48%), marks
    it BUCKET_KILLED and persists the event to signal_kill_switches. Requires
    a manual resurrect_signal() call plus SIGNAL_KILL_RESURRECT_N (20) new
    active trades before it can requalify (see get_signal_kill_status).
    """
    trades = _load_closed_trades_full()
    results = {}
    for sig, col in _SIGNAL_COLS.items():
        stats = _per_signal_wilson_ci(trades, col, active_threshold=10.0)
        if stats["n"] < SIGNAL_KILL_MIN_TRADES:
            results[sig] = {
                **stats, "killed": False,
                "reason": f"Only {stats['n']} trades — need {SIGNAL_KILL_MIN_TRADES}",
            }
            continue
        killed = stats["ci_upper"] is not None and stats["ci_upper"] < SIGNAL_KILL_CI_FLOOR
        results[sig] = {**stats, "killed": killed}
        if killed:
            _persist_kill_switch(sig, stats)
    return results


def get_signal_kill_status() -> list[dict]:
    """
    All persisted kill-switch rows, each flagged with whether it's eligible
    for a manual resurrection review (SIGNAL_KILL_RESURRECT_N new active
    trades since the kill).
    """
    from db.database import get_db
    trades = _load_closed_trades_full()
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM signal_kill_switches").fetchall()]
    out = []
    for row in rows:
        sig = row["signal"]
        col = _SIGNAL_COLS.get(sig)
        current_n = (
            sum(1 for t in trades if t.get(col) is not None and abs(t[col]) >= 10.0)
            if col else 0
        )
        eligible = (
            not row["resurrected"]
            and current_n >= row["n_trades_at_kill"] + SIGNAL_KILL_RESURRECT_N
        )
        out.append({
            **row, "current_active_trades": current_n,
            "eligible_for_resurrection_review": eligible,
        })
    return out


def resurrect_signal(signal: str) -> dict:
    """Manually resurrects a killed signal (human-in-the-loop, Citadel pod model)."""
    from db.database import get_db
    with get_db() as conn:
        conn.execute(
            "UPDATE signal_kill_switches SET resurrected=1, resurrected_at=datetime('now') WHERE signal=?",
            (signal,),
        )
    return {"signal": signal, "resurrected": True}


# Added 2026-07-16 (Tier 1 of the trading-model audit — see CODEMAP.md).
# check_signal_kill_switches()/_persist_kill_switch() have existed since
# round 5 and correctly PERSIST a kill when a signal's Wilson CI proves it's
# not helping, but nothing ever READ that persisted state back into a live
# score — a signal proven harmful with 50 real trades kept getting the exact
# same weight as before. This is the lean read used by the weight-wiring in
# intraday.py: just the currently-killed signal names, not the full
# get_signal_kill_status() payload (which also computes resurrection
# eligibility for the calibration UI — more than a hot scoring path needs).
def get_active_kill_switches() -> set[str]:
    """Signal names currently killed and not yet resurrected."""
    try:
        from db.database import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT signal FROM signal_kill_switches WHERE resurrected = 0"
            ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


def calibration_readiness_status() -> dict:
    """
    Per-feature readiness check with threshold targets.

    Thresholds:
      kelly            — 50 closed trades
      dynamic_weights  — 30 closed trades
      per_signal       — 10 active trades per signal (v4 data)
      ngram_blend      — 20 each in agree + disagree cohorts
      time_session     — 20 trades per time-of-day session

    Returns a dict with 'features' list (one row per feature) and
    an overall 'pct_ready' fraction.
    """
    trades   = _load_closed_trades_full()
    n_total  = len(trades)

    # ── Kelly readiness ───────────────────────────────────────────────────────
    features = [
        {
            "feature":   "kelly_sizing",
            "threshold": 50,
            "current":   n_total,
            "ready":     n_total >= 50,
            "note":      None if n_total >= 50 else f"Need {50 - n_total} more closed trades",
        },
        {
            "feature":   "dynamic_weights",
            "threshold": 30,
            "current":   n_total,
            "ready":     n_total >= 30,
            "note":      None if n_total >= 30 else f"Need {30 - n_total} more closed trades",
        },
    ]

    # ── Per-signal readiness (10 active trades each) ──────────────────────────
    n_signals_ready = 0
    signal_details  = []
    for sig, col in _SIGNAL_COLS.items():
        stats = _per_signal_stats(trades, col, active_threshold=10.0)
        ready = stats["n"] >= 10
        if ready:
            n_signals_ready += 1
        signal_details.append(f"{sig}:{stats['n']}")

    features.append({
        "feature":   "per_signal_accuracy",
        "threshold": f"10 per signal ({len(_SIGNAL_COLS)} signals)",
        "current":   f"{n_signals_ready}/{len(_SIGNAL_COLS)} signals ready",
        "ready":     n_signals_ready == len(_SIGNAL_COLS),
        "detail":    ", ".join(signal_details),
        "note":      None if n_signals_ready == len(_SIGNAL_COLS)
                     else f"{len(_SIGNAL_COLS) - n_signals_ready} signal(s) below 10 active trades",
    })

    # ── N-gram blend readiness (20 agree + 20 disagree) ──────────────────────
    agree_n = disagree_n = 0
    for t in trades:
        ng  = t.get("ngram_signal")
        raw = t.get("composite_raw")
        if ng is None or raw is None or ng == "NONE":
            continue
        if (ng == "UP" and raw > 0) or (ng == "DOWN" and raw < 0):
            agree_n += 1
        elif (ng == "UP" and raw < 0) or (ng == "DOWN" and raw > 0):
            disagree_n += 1

    features.append({
        "feature":   "ngram_blend_weight",
        "threshold": "20 agree + 20 disagree",
        "current":   f"agree={agree_n}, disagree={disagree_n}",
        "ready":     agree_n >= 20 and disagree_n >= 20,
        "note":      None if (agree_n >= 20 and disagree_n >= 20)
                     else f"Need agree≥20 (have {agree_n}), disagree≥20 (have {disagree_n})",
    })

    # ── Time-session readiness (20 trades per session) ────────────────────────
    session_counts: dict[str, int] = {}
    for t in trades:
        key = t.get("time_of_day_label") or "UNKNOWN"
        session_counts[key] = session_counts.get(key, 0) + 1

    sessions_ready = sum(1 for v in session_counts.values() if v >= 20)
    sessions_total = len(session_counts)
    features.append({
        "feature":   "time_session_accuracy",
        "threshold": "20 per session",
        "current":   {k: v for k, v in sorted(session_counts.items())},
        "ready":     sessions_ready > 0 and sessions_ready == sessions_total,
        "note":      None if (sessions_ready > 0 and sessions_ready == sessions_total)
                     else f"{sessions_ready}/{sessions_total} session(s) have ≥20 trades",
    })

    n_ready  = sum(1 for f in features if f["ready"])
    pct_ready = round(n_ready / len(features), 2) if features else 0.0

    return {
        "total_closed_trades": n_total,
        "features":            features,
        "n_ready":             n_ready,
        "n_total_features":    len(features),
        "pct_ready":           pct_ready,
        "overall_status": (
            "fully_calibrated" if pct_ready == 1.0
            else "partially_calibrated" if pct_ready >= 0.5
            else "insufficient"
        ),
    }


# ── Low Value engine calibration (Kimi review, round 6 follow-up) ────────────
#
# Low Value's signal set (price_vs_20d_low, rsi_14, volume_spike, ...) doesn't
# map onto _SIGNAL_COLS (High Value's vwap_score/or_score/etc.), so it gets
# its own calibration axis: per lv_thesis_type, not per numeric signal column.
# Thresholds are looser-tiered per spec: 20 trades/thesis-type for a
# preliminary read, 50 for dynamic weight recalibration, 100 for empirical
# position sizing (vs. High Value's 30/50/100 on the overall pool).
LOW_VALUE_THESIS_PRELIMINARY_MIN_TRADES: int = 20
LOW_VALUE_DYNAMIC_WEIGHT_MIN_TRADES: int = 50
LOW_VALUE_EMPIRICAL_SIZING_MIN_TRADES: int = 100


def _load_closed_low_value_trades(engine: str = "low_value") -> list[dict]:
    """
    All closed trades with the lv_* columns, scoped to one engine.

    engine (added 2026-08-28 for the Automaton engine — same "default
    preserves every existing caller's behavior bit-for-bit" convention as
    _load_closed_trades' own engine param above): Automaton shares Low
    Value's exact 8-signal thesis formula and lv_* columns, but must never
    be calibrated against Low Value's own trade history (different hold
    horizon, different execution mode) — every function in this section
    takes the same param for that reason.
    """
    try:
        from db.database import get_db
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT entry_score, pnl_r, pnl_dollars, pnl_pct,
                       symbol, exit_reason, is_hypothetical,
                       lv_thesis_type, lv_news_flags, lv_news_sentiment, lv_headline_count,
                       lv_missing_signals, lv_short_interest_asof, lv_signals_json
                FROM intraday_trades
                WHERE exit_price IS NOT NULL AND engine = ?
                ORDER BY logged_at DESC
                """,
                (engine,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# Added 2026-07-16 (Tier 0 of the trading-model audit — see CODEMAP.md).
# Mirrors per_signal_accuracy_report's exact methodology (active if
# |score| >= active_threshold, then win_rate/avg_r over that active cohort)
# but for Low Value's 8 JSON-stored signals instead of High Value's dedicated
# DB columns. Before this, Low Value could report win rate per THESIS TYPE
# (thesis_type_calibration_report) but not per individual SIGNAL — meaning a
# question like "is short_interest_pct actually protective or is treating a
# squeeze as bullish wrong?" was structurally unanswerable from data even
# after thousands of trades, since the per-signal score was stored in
# lv_signals_json but never aggregated. This makes it answerable the same
# way High Value's signals already are — reads the same JSON blob
# _signals_breakdown_html (fetchers/low_value_dashboard.py) already parses
# for display, so no new logging or schema change was needed, only a new
# aggregation over data that was already being captured per trade.
def low_value_per_signal_accuracy_report(
    min_trades: int = 10,
    active_threshold: float = 10.0,
    engine: str = "low_value",
) -> dict:
    """
    Win rate and average P&L (in R, when available) per individual Low Value
    signal, extracted from lv_signals_json. Same "active if |score| >=
    active_threshold" convention as per_signal_accuracy_report — the default
    10.0 matches that function's default on the same -100..100 signal scale.

    engine: see _load_closed_low_value_trades — default preserves existing
    (Low Value) behavior; Automaton passes engine="automaton".
    """
    import json as _json
    from models.trading.low_value.thesis_tracker import SIGNAL_WEIGHTS

    trades = _load_closed_low_value_trades(engine=engine)
    n_total = len(trades)

    by_signal = []
    for sig in SIGNAL_WEIGHTS:
        active = []
        for t in trades:
            try:
                signals = _json.loads(t.get("lv_signals_json") or "{}")
            except (TypeError, ValueError):
                continue
            entry = signals.get(sig)
            if not entry:
                continue
            score = entry.get("score")
            if score is not None and abs(score) >= active_threshold:
                active.append(t)

        n = len(active)
        if n == 0:
            by_signal.append({
                "signal": sig, "n": 0, "win_rate": None, "avg_pnl_r": None,
                "note": f"Only 0 active trade(s) — need {min_trades}",
            })
            continue

        wins = sum(1 for t in active if _is_winner(t))
        win_rate = round(wins / n, 3)
        # pnl_r is always None for Low Value (log_low_value_trade never sets
        # risk_dollars — fixed $25 sizing has no ATR-based stop distance to
        # normalize against, unlike High Value). pnl_pct is populated for
        # every trade and IS comparable across Low Value trades specifically
        # because every position shares the same +50%/-50% target/stop
        # distance — so it plays the same "edge magnitude" role R plays for
        # High Value, just under a different name to avoid implying it's the
        # same unit. See compute_low_value_dynamic_weights, which is the
        # actual consumer of avg_pnl_pct.
        pnl_rs = [t["pnl_r"] for t in active if t.get("pnl_r") is not None]
        avg_pnl_r = round(sum(pnl_rs) / len(pnl_rs), 3) if pnl_rs else None
        pnl_pcts = [t["pnl_pct"] for t in active if t.get("pnl_pct") is not None]
        avg_pnl_pct = round(sum(pnl_pcts) / len(pnl_pcts), 2) if pnl_pcts else None

        note = None
        if n < min_trades:
            note = f"Only {n} active trade(s) — need {min_trades}"
        elif win_rate < 0.50:
            note = "Below 50% win rate — may be dragging the composite score"

        by_signal.append({
            "signal": sig, "n": n, "win_rate": win_rate,
            "avg_pnl_r": avg_pnl_r, "avg_pnl_pct": avg_pnl_pct, "note": note,
        })

    by_signal.sort(key=lambda x: (x.get("win_rate") if x.get("win_rate") is not None else -1), reverse=True)
    return {
        "total_closed": n_total,
        "by_signal": by_signal,
        "active_threshold": active_threshold,
        "note": f"No closed {engine} trades yet" if n_total == 0 else None,
    }


# Added 2026-07-16 (Tier 1). Low Value's equivalent of compute_dynamic_weights
# — that function is hardcoded to High Value's _SIGNAL_COLS/_load_closed_trades_full
# and can't read Low Value's JSON-stored signals at all, so this is new code
# rather than a parameterization of the existing one. Same formula, same
# fallback-to-static behavior, same min_trades floor, just sourced from
# low_value_per_signal_accuracy_report's avg_pnl_pct (Low Value's edge-
# magnitude stand-in for R — see that function's comment) instead of avg_r.
def compute_low_value_dynamic_weights(min_trades: int = 30, engine: str = "low_value") -> Optional[dict]:
    """
    Empirically-driven signal weights (Low Value or, since 2026-08-28,
    Automaton — see engine param on _load_closed_low_value_trades above).

    Formula per signal (same shape as compute_dynamic_weights):
        raw = max(0, (win_rate - 0.5) * (avg_pnl_pct / 100))
    Normalized: weight = raw / sum(all raws).
    Falls back to thesis_tracker.SIGNAL_WEIGHTS when sum of raws is zero.

    Returns None when total closed trades for this engine < min_trades.
    """
    from models.trading.low_value.thesis_tracker import SIGNAL_WEIGHTS

    trades = _load_closed_low_value_trades(engine=engine)
    if len(trades) < min_trades:
        return None

    report = low_value_per_signal_accuracy_report(min_trades=5, active_threshold=10.0, engine=engine)

    raw_weights: dict[str, float] = {}
    negative_utility: list[str] = []
    for row in report["by_signal"]:
        sig = row["signal"]
        wr = row.get("win_rate")
        avg_pct = row.get("avg_pnl_pct")
        if wr is None or avg_pct is None or row["n"] < 5:
            raw_weights[sig] = 0.0
            continue
        avg_edge = avg_pct / 100
        raw = max(0.0, (wr - 0.5) * avg_edge)
        raw_weights[sig] = raw
        if wr < 0.50 or avg_edge < 0:
            negative_utility.append(sig)

    total_raw = sum(raw_weights.values())
    if total_raw > 0:
        weights = {k: round(v / total_raw, 4) for k, v in raw_weights.items()}
        status = "dynamic"
    else:
        weights = dict(SIGNAL_WEIGHTS)
        status = "fallback_static"

    return {
        "weights": weights,
        "raw_weights": {k: round(v, 6) for k, v in raw_weights.items()},
        "negative_utility": negative_utility,
        "n_trades": len(trades),
        "status": status,
    }


def thesis_type_calibration_report(min_trades: int = LOW_VALUE_THESIS_PRELIMINARY_MIN_TRADES, engine: str = "low_value") -> dict:
    """
    Win rate and avg P&L per lv_thesis_type (EARNINGS_MISS, ANALYST_DOWNGRADE,
    REGULATORY_RISK, OPERATIONAL_CRISIS, POSITIVE_CATALYST, INSIDER_BUYING,
    TECHNICAL_OVERSOLD). Below min_trades for a given thesis type, that
    type's win_rate/avg_pnl_r are None — "preliminary" note, never faked.

    engine: see _load_closed_low_value_trades — default preserves existing
    (Low Value) behavior; Automaton passes engine="automaton".
    """
    trades = _load_closed_low_value_trades(engine=engine)
    n_total = len(trades)
    if n_total == 0:
        return {
            "total_closed": 0, "by_thesis_type": [],
            "note": f"No closed {engine} trades yet",
        }

    by_type: dict[str, list[dict]] = {}
    for t in trades:
        key = t.get("lv_thesis_type") or "UNKNOWN"
        by_type.setdefault(key, []).append(t)

    rows = []
    for thesis_type, tt in by_type.items():
        n = len(tt)
        if n < min_trades:
            rows.append({
                "thesis_type": thesis_type, "n": n, "win_rate": None, "avg_pnl_r": None,
                "note": f"Only {n} trade(s) — need {min_trades} for a preliminary read",
            })
            continue
        wins   = sum(1 for t in tt if _is_winner(t))
        pnl_rs = [t["pnl_r"] for t in tt if t.get("pnl_r") is not None]
        rows.append({
            "thesis_type": thesis_type,
            "n":            n,
            "win_rate":      round(wins / n, 3),
            "avg_pnl_r":     round(sum(pnl_rs) / len(pnl_rs), 3) if pnl_rs else None,
        })

    rows.sort(key=lambda x: (x.get("win_rate") or -1), reverse=True)
    return {"total_closed": n_total, "by_thesis_type": rows}


def low_value_calibration_readiness(engine: str = "low_value") -> dict:
    """
    Overall readiness (Low Value or, since 2026-08-28, Automaton): total
    closed trades vs. the 20/50/100 preliminary/dynamic-weight/empirical-
    sizing thresholds. Same tier discipline shared across both engines —
    engine: see _load_closed_low_value_trades.
    """
    n_total = len(_load_closed_low_value_trades(engine=engine))
    return {
        "total_closed_trades":      n_total,
        "preliminary_ready":        n_total >= LOW_VALUE_THESIS_PRELIMINARY_MIN_TRADES,
        "dynamic_weights_ready":    n_total >= LOW_VALUE_DYNAMIC_WEIGHT_MIN_TRADES,
        "empirical_sizing_ready":   n_total >= LOW_VALUE_EMPIRICAL_SIZING_MIN_TRADES,
        "calibration_quality": (
            "empirical_sizing" if n_total >= LOW_VALUE_EMPIRICAL_SIZING_MIN_TRADES
            else "dynamic_weights" if n_total >= LOW_VALUE_DYNAMIC_WEIGHT_MIN_TRADES
            else "preliminary" if n_total >= LOW_VALUE_THESIS_PRELIMINARY_MIN_TRADES
            else "insufficient"
        ),
    }


def missing_signal_impact_report(min_trades: int = LOW_VALUE_THESIS_PRELIMINARY_MIN_TRADES) -> dict:
    """
    Kimi review, round-2 follow-up: "did trades with missing data underperform
    trades with full data?" For each of the 8 Low Value signals, splits closed
    trades into "signal was missing at entry" vs "signal was present" and
    compares win rate / avg P&L — answers whether the missing-signal
    reweighting (Section 5 of low_value_trading4kimi.md) is hiding a real
    risk, or whether thinly-covered names perform fine on the signals that
    ARE available. Below min_trades in either cohort for a given signal,
    that signal's comparison is None — never faked.
    """
    import json as _json
    trades = _load_closed_low_value_trades()
    if not trades:
        return {"total_closed": 0, "by_signal": [], "note": "No closed Low Value trades yet"}

    signal_names = [
        "price_vs_20d_low", "rsi_14", "volume_spike", "insider_buying_30d",
        "short_interest_pct", "sector_relative_strength", "cash_burn_months", "news_sentiment",
    ]

    def _stats(cohort: list[dict]) -> dict:
        n = len(cohort)
        if n == 0:
            return {"n": 0, "win_rate": None, "avg_pnl_r": None}
        wins = sum(1 for t in cohort if _is_winner(t))
        pnl_rs = [t["pnl_r"] for t in cohort if t.get("pnl_r") is not None]
        return {
            "n": n, "win_rate": round(wins / n, 3),
            "avg_pnl_r": round(sum(pnl_rs) / len(pnl_rs), 3) if pnl_rs else None,
        }

    by_signal = []
    for sig in signal_names:
        missing_cohort, present_cohort = [], []
        for t in trades:
            try:
                missing_list = _json.loads(t.get("lv_missing_signals") or "[]")
            except Exception:
                missing_list = []
            (missing_cohort if sig in missing_list else present_cohort).append(t)

        missing_stats = _stats(missing_cohort)
        present_stats = _stats(present_cohort)
        ready = missing_stats["n"] >= min_trades and present_stats["n"] >= min_trades
        by_signal.append({
            "signal": sig,
            "missing": missing_stats,
            "present": present_stats,
            "ready": ready,
            "note": None if ready else f"Need {min_trades}+ trades in both cohorts (have missing={missing_stats['n']}, present={present_stats['n']})",
        })

    return {"total_closed": len(trades), "by_signal": by_signal}
