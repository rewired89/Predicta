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


# Hard floor before ANY calibration output (empirical win rate, veto, dynamic
# weights) is allowed to influence a live decision (Kimi review, round 2): the
# base ensemble's edge hasn't been validated yet, so acting on 30-50 trade
# calibration data risks tuning noise rather than a real signal.
MIN_TRADES_FOR_ANY_CALIBRATION = 100


def calibration_globally_active() -> bool:
    """
    True once total closed trades >= MIN_TRADES_FOR_ANY_CALIBRATION (100).
    Callers (e.g. kelly_from_signals) must check this before using
    calibrated_win_rate() or veto_decision() for a real decision — below the
    floor, fall back to static behavior instead.
    """
    return len(_load_closed_trades()) >= MIN_TRADES_FOR_ANY_CALIBRATION


def _wilson_ci(wins: int, n: int, confidence: float = 0.80) -> tuple[float, float]:
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


def veto_decision(score: float, min_trades: int = 30, ci_floor: float = 0.48) -> dict:
    """
    Statistically-gated trade veto (Kimi review): replaces a naive point-estimate
    check (e.g. "win rate < 45%") with an 80% confidence-interval test, and
    refuses to veto at all below min_trades in the score's bucket.

    Rationale: a 45% observed win rate at n=15 could easily be a true 55% rate
    with bad variance, or a true 35% rate with good variance — vetoing on the
    point estimate alone is noise responding to noise. This only vetoes when
    the bucket's 80% CI upper bound sits entirely below ci_floor, which in
    practice requires roughly 25-30+ trades in the bucket to ever trigger.

    Returns: {veto, reason, n, win_rate, ci_lower, ci_upper, min_trades}
    """
    trades = _load_closed_trades()
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
    ci_lower, ci_upper = _wilson_ci(wins, n)

    veto = ci_upper < ci_floor
    reason = (
        f"80% CI upper bound {ci_upper:.1%} < {ci_floor:.0%} floor — edge statistically gone"
        if veto else
        f"80% CI [{ci_lower:.1%}, {ci_upper:.1%}] does not confirm the edge is gone"
    )
    return {
        "veto": veto, "reason": reason, "n": n, "win_rate": win_rate,
        "ci_lower": round(ci_lower, 3), "ci_upper": round(ci_upper, 3),
        "min_trades": min_trades,
    }


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


def _load_closed_trades_full() -> list[dict]:
    """All closed trades with v4 per-signal score columns."""
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
                WHERE exit_price IS NOT NULL
                ORDER BY logged_at DESC
                """
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
