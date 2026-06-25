"""
Calibration metrics: accuracy %, Brier score, log-loss, reliability curve.
"""
from __future__ import annotations
import math
from typing import Optional
import numpy as np
from db.database import get_db


def brier_score(predictions: list[float], outcomes: list[int]) -> float:
    if not predictions:
        return float("nan")
    return float(np.mean([(p - o) ** 2 for p, o in zip(predictions, outcomes)]))


def log_loss_score(predictions: list[float], outcomes: list[int], eps: float = 1e-7) -> float:
    if not predictions:
        return float("nan")
    total = 0.0
    for p, o in zip(predictions, outcomes):
        p = max(eps, min(1 - eps, p))
        total += o * math.log(p) + (1 - o) * math.log(1 - p)
    return -total / len(predictions)


def expected_calibration_error(
    predictions: list[float], outcomes: list[int], n_bins: int = 10
) -> float:
    """
    Expected Calibration Error — weighted average absolute difference between
    mean predicted confidence and actual accuracy across n_bins buckets.
    Target: ECE < 0.05 (well-calibrated), < 0.10 (acceptable).
    """
    if not predictions:
        return float("nan")
    bins: list[list] = [[] for _ in range(n_bins)]
    for p, o in zip(predictions, outcomes):
        idx = min(int(p * n_bins), n_bins - 1)
        bins[idx].append((p, o))
    ece = 0.0
    n = len(predictions)
    for bucket in bins:
        if not bucket:
            continue
        avg_conf = sum(p for p, _ in bucket) / len(bucket)
        avg_acc  = sum(o for _, o in bucket) / len(bucket)
        ece += len(bucket) * abs(avg_conf - avg_acc)
    return round(ece / n, 4)


def reliability_curve(
    predictions: list[float], outcomes: list[int], n_bins: int = 10
) -> list[dict]:
    bins: list[list] = [[] for _ in range(n_bins)]
    for p, o in zip(predictions, outcomes):
        idx = min(int(p * n_bins), n_bins - 1)
        bins[idx].append((p, o))

    result = []
    for i, bucket in enumerate(bins):
        if not bucket:
            continue
        mean_pred = sum(p for p, _ in bucket) / len(bucket)
        mean_actual = sum(o for _, o in bucket) / len(bucket)
        result.append({
            "bin_lower":      i / n_bins,
            "bin_upper":      (i + 1) / n_bins,
            "mean_predicted": round(mean_pred, 3),
            "mean_actual":    round(mean_actual, 3),
            "count":          len(bucket),
        })
    return result


def _accuracy_block(rows: list) -> dict:
    """
    Compute accuracy stats for a slice of (prob_a, result, recommendation) rows.

    Accuracy: predicted winner (argmax of prob_a vs prob_b) == actual winner.
    Bet accuracy: only rows where recommendation != 'PASS'.
    ROI: (sum of won bets * odds - total staked) / total staked (assumes flat 1 unit, -110 odds ~1.909x).
    """
    n = len(rows)
    if n == 0:
        return {"n": 0}

    correct = 0
    bet_correct = bet_total = 0
    roi_sum = 0.0

    for r in rows:
        prob_a   = r["prob_a"]
        prob_b   = r.get("prob_b") or (1.0 - prob_a)
        result   = r["result"]           # 'a', 'b', 'draw'
        rec      = r.get("recommendation") or ""

        predicted = "a" if prob_a >= prob_b else "b"
        won = (predicted == result)
        if won:
            correct += 1

        # Bet-only accuracy (skip PASS and draws)
        if rec and rec.upper() != "PASS":
            bet_total += 1
            rec_side = "a" if rec == r.get("participant_a", "") else "b"
            if rec_side == result:
                bet_correct += 1
                roi_sum += 0.909   # win +0.909 units at -110
            else:
                roi_sum -= 1.0     # lose 1 unit

    bet_acc  = round(bet_correct / bet_total, 4) if bet_total else None
    roi      = round(roi_sum / bet_total, 4) if bet_total else None

    return {
        "n":            n,
        "correct":      correct,
        "accuracy":     round(correct / n, 4),
        "bets_placed":  bet_total,
        "bets_correct": bet_correct,
        "bet_accuracy": bet_acc,
        "roi":          roi,
    }


def compute_metrics_from_db(method: Optional[str] = None, sport: Optional[str] = None) -> dict:
    """
    Pull predictions + outcomes from DB and compute full accuracy + calibration metrics.

    Returns:
      overall       — accuracy across all resolved predictions
      by_sport      — accuracy broken down per sport
      by_confidence — accuracy broken down by data_confidence level
      by_method     — accuracy broken down by prediction method
      brier_score   — mean squared error (lower = better; 0.25 = random, 0.0 = perfect)
      log_loss      — lower is better; ln(2) ≈ 0.693 = random
      reliability_curve — calibration deciles (mean predicted vs actual)
      benchmark     — comparison to known baselines
    """
    with get_db() as conn:
        query = """
            SELECT
                p.prob_a, p.prob_b, p.prob_draw, p.method,
                o.result,
                m.sport,
                m.participant_a, m.participant_b,
                s_conf.signal_text  AS data_confidence,
                s_rec.signal_text   AS recommendation
            FROM predictions p
            JOIN outcomes o  ON p.match_id = o.match_id
            JOIN matches m   ON m.id = p.match_id
            LEFT JOIN signals s_conf ON (
                s_conf.match_id = p.match_id AND s_conf.signal_name = 'data_confidence'
            )
            LEFT JOIN signals s_rec ON (
                s_rec.match_id  = p.match_id AND s_rec.signal_name  = 'recommendation'
            )
        """
        params: list = []
        conditions: list[str] = []
        if method:
            conditions.append("p.method = ?")
            params.append(method)
        if sport:
            conditions.append("m.sport = ?")
            params.append(sport)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        rows = [dict(r) for r in conn.execute(query, params).fetchall()]

    if not rows:
        return {
            "n": 0,
            "message": "No resolved predictions yet. Record outcomes via POST /matches/{id}/outcome to start tracking accuracy.",
        }

    preds   = [r["prob_a"] for r in rows]
    actuals = [1 if r["result"] == "a" else 0 for r in rows]

    # Overall accuracy
    overall = _accuracy_block(rows)

    # By sport
    sports = sorted({r["sport"] for r in rows})
    by_sport = {
        sp: _accuracy_block([r for r in rows if r["sport"] == sp])
        for sp in sports
    }

    # By confidence
    confs = sorted({r.get("data_confidence") or "unknown" for r in rows})
    by_confidence = {
        cf: _accuracy_block([r for r in rows if (r.get("data_confidence") or "unknown") == cf])
        for cf in confs
    }

    # By method
    methods = sorted({r["method"] for r in rows})
    by_method = {
        mt: _accuracy_block([r for r in rows if r["method"] == mt])
        for mt in methods
    }

    bs   = brier_score(preds, actuals)
    ll   = log_loss_score(preds, actuals)
    crv  = reliability_curve(preds, actuals)
    ece  = expected_calibration_error(preds, actuals)

    # ECE-based Kelly multiplier: well-calibrated models deserve more aggressive sizing
    if math.isnan(ece):
        kelly_mult = 0.25
    elif ece < 0.02:
        kelly_mult = 1.0
    elif ece < 0.05:
        kelly_mult = 0.5
    else:
        kelly_mult = 0.25

    # Benchmark comparison
    n = overall["n"]
    acc = overall["accuracy"]
    benchmarks = [
        {"model": "Random (coin flip)",            "accuracy": 0.500, "note": "baseline"},
        {"model": "Pinnacle closing line",          "accuracy": 0.560, "note": "best public sportsbook"},
        {"model": "ATP Elo (tennis)",               "accuracy": 0.670, "note": "academic benchmark, ATP only"},
        {"model": "Betfair exchange implied prob",  "accuracy": 0.555, "note": "market consensus"},
        {"model": "Predicta (this model)",          "accuracy": round(acc, 4),
         "note": f"{n} resolved predictions",
         "vs_random":   f"{(acc - 0.50)*100:+.1f}pp vs random",
         "vs_pinnacle": f"{(acc - 0.56)*100:+.1f}pp vs Pinnacle"},
    ]

    return {
        "overall":           overall,
        "by_sport":          by_sport,
        "by_confidence":     by_confidence,
        "by_method":         by_method,
        "brier_score":       round(bs, 4) if not math.isnan(bs) else None,
        "brier_benchmark":   {"random": 0.25, "perfect": 0.0,
                               "good_model": "< 0.20"},
        "log_loss":          round(ll, 4) if not math.isnan(ll) else None,
        "log_loss_benchmark":{"random": 0.693, "perfect": 0.0,
                               "good_model": "< 0.55"},
        "reliability_curve": crv,
        "ece":                        round(ece, 4) if not math.isnan(ece) else None,
        "ece_benchmark":              {"well_calibrated": "< 0.05", "acceptable": "< 0.10"},
        "recommended_kelly_adjustment": {
            "multiplier": kelly_mult,
            "note": (
                "ECE < 0.02 → full Kelly; ECE 0.02–0.05 → half Kelly; ECE > 0.05 → quarter Kelly. "
                "Always combine with base quarter-Kelly stake from kelly.py."
            ),
        },
        "benchmarks":                 benchmarks,
    }
