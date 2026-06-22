"""
Calibration metrics: Brier score, log-loss, reliability curve, CLV tracking.
"""
from __future__ import annotations
import math
from typing import Optional
import numpy as np
from db.database import get_db


def brier_score(predictions: list[float], outcomes: list[int]) -> float:
    """Mean squared error between predicted probs and binary outcomes."""
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


def reliability_curve(
    predictions: list[float], outcomes: list[int], n_bins: int = 10
) -> list[dict]:
    """
    Bucket predictions into deciles, return mean predicted vs actual win rate per bucket.
    """
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
            "bin_lower": i / n_bins,
            "bin_upper": (i + 1) / n_bins,
            "mean_predicted": mean_pred,
            "mean_actual": mean_actual,
            "count": len(bucket),
        })
    return result


def compute_metrics_from_db(method: Optional[str] = None) -> dict:
    """Pull predictions + outcomes from DB and compute full calibration metrics."""
    with get_db() as conn:
        query = """
            SELECT p.prob_a, o.result
            FROM predictions p
            JOIN outcomes o ON p.match_id = o.match_id
        """
        params: tuple = ()
        if method:
            query += " WHERE p.method = ?"
            params = (method,)
        rows = conn.execute(query, params).fetchall()

    if not rows:
        return {"error": "No matched prediction/outcome pairs found."}

    preds = [r["prob_a"] for r in rows]
    actuals = [1 if r["result"] == "a" else 0 for r in rows]

    bs = brier_score(preds, actuals)
    ll = log_loss_score(preds, actuals)
    curve = reliability_curve(preds, actuals)

    return {
        "n": len(preds),
        "brier_score": bs,
        "log_loss": ll,
        "reliability_curve": curve,
    }
