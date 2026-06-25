"""
Pairs trading: cointegration detection and spread signal generation.

Based on Gatev et al. (2006) — the most-cited statistical arbitrage paper.
Core idea: find two stocks whose price ratio is stationary (cointegrated),
then trade the spread when it deviates beyond ±2σ from its mean.

Market-neutral: profits from relative mispricing, not directional market moves.
"""
from __future__ import annotations
import math
from typing import Optional


# ── Cointegration test ───────────────────────────────────────────────────────

def _ols_beta(x: list[float], y: list[float]) -> float:
    """OLS slope: β = Σ(xi - x̄)(yi - ȳ) / Σ(xi - x̄)²"""
    n = len(x)
    x_mean = sum(x) / n
    y_mean = sum(y) / n
    num = sum((x[i] - x_mean) * (y[i] - y_mean) for i in range(n))
    den = sum((x[i] - x_mean) ** 2 for i in range(n))
    return num / den if den else 1.0


def _adf_pvalue(residuals: list[float]) -> float:
    """
    Simplified Augmented Dickey-Fuller approximation for the spread residuals.
    Uses statsmodels if available for accuracy; falls back to a correlation-
    based proxy that gives comparable ranking (useful for screening).

    The p-value returned is from the ADF test for a unit root. Small p-value
    (<0.05) means the spread is stationary — i.e., the pair is cointegrated.
    """
    try:
        from statsmodels.tsa.stattools import adfuller
        result = adfuller(residuals, maxlag=1, regression="c", autolag=None)
        return float(result[1])  # p-value
    except ImportError:
        pass

    # Fallback: first-order autocorrelation proxy
    # A random walk has autocorr ≈ 1.0; a stationary series has lower autocorr.
    # We map autocorr → approximate p-value (heuristic, for ranking only).
    n = len(residuals)
    if n < 4:
        return 1.0
    mean = sum(residuals) / n
    diffs = [r - mean for r in residuals]
    numer = sum(diffs[i] * diffs[i - 1] for i in range(1, n))
    denom = sum(d ** 2 for d in diffs)
    autocorr = numer / denom if denom else 1.0
    # Heuristic: autocorr near 1 → p ≈ 1.0; autocorr near 0 → p ≈ 0.01
    p_proxy = max(0.0, min(1.0, autocorr ** 2))
    return p_proxy


def find_cointegrated_pairs(
    series: dict[str, list[float]],
    pvalue_threshold: float = 0.05,
) -> list[dict]:
    """
    Test all pairs in `series` for cointegration.

    Args:
        series: {symbol: [close_price, ...]} — aligned lists, same length.
        pvalue_threshold: pairs with ADF p-value below this are returned.

    Returns:
        List of dicts sorted by p-value ascending:
        [{"sym1", "sym2", "beta", "pvalue", "half_life_days"}, ...]
    """
    symbols = list(series.keys())
    results = []

    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            s1_name, s2_name = symbols[i], symbols[j]
            s1, s2 = series[s1_name], series[s2_name]
            if len(s1) < 20 or len(s2) < 20:
                continue

            # Estimate hedge ratio: s1 = β × s2 + ε
            beta = _ols_beta(s2, s1)
            spread = [s1[k] - beta * s2[k] for k in range(len(s1))]
            pvalue = _adf_pvalue(spread)

            if pvalue <= pvalue_threshold:
                results.append({
                    "sym1":           s1_name,
                    "sym2":           s2_name,
                    "beta":           round(beta, 6),
                    "pvalue":         round(pvalue, 4),
                    "half_life_days": _half_life(spread),
                })

    return sorted(results, key=lambda x: x["pvalue"])


def _half_life(spread: list[float]) -> Optional[int]:
    """
    Ornstein-Uhlenbeck half-life: how many days until the spread mean-reverts
    halfway back. Lower = faster mean reversion = better for trading.
    Estimated via OLS regression of Δspread on lagged spread.
    """
    if len(spread) < 5:
        return None
    delta = [spread[i] - spread[i - 1] for i in range(1, len(spread))]
    lagged = spread[:-1]
    beta = _ols_beta(lagged, delta)
    if beta >= 0:
        return None  # No mean reversion (positive feedback)
    hl = -math.log(2) / beta
    return int(round(hl)) if 0 < hl < 365 else None


# ── Signal generation ────────────────────────────────────────────────────────

def pairs_signal(
    sym1: str,
    sym2: str,
    beta: float,
    series: dict[str, list[float]],
    lookback: int = 60,
    entry_z: float = 2.0,
    exit_z: float = 0.5,
) -> dict:
    """
    Generate a pairs trade signal using the z-score of the spread.

    Long spread  (zscore < -entry_z): buy sym1, short sym2
    Short spread (zscore >  entry_z): short sym1, buy sym2
    Exit zone    (|zscore| < exit_z): close existing position

    Args:
        sym1, sym2: symbol names matching keys in `series`
        beta: hedge ratio from find_cointegrated_pairs
        series: {symbol: [close_price, ...]}
        lookback: rolling window for spread mean/std computation
        entry_z: z-score threshold to enter (default 2.0σ)
        exit_z: z-score threshold to exit (default 0.5σ)

    Returns:
        {action, zscore, sym1, sym2, long_sym, short_sym, confidence,
         spread_mean, spread_std, current_spread}
    """
    s1 = series.get(sym1, [])
    s2 = series.get(sym2, [])
    if not s1 or not s2:
        return {"action": "ERROR", "error": f"Missing data for {sym1} or {sym2}"}

    window = min(lookback, len(s1), len(s2))
    s1_w = s1[-window:]
    s2_w = s2[-window:]

    spread = [s1_w[k] - beta * s2_w[k] for k in range(window)]
    spread_mean = sum(spread) / len(spread)
    variance = sum((v - spread_mean) ** 2 for v in spread) / len(spread)
    spread_std = math.sqrt(variance) if variance > 0 else 1.0

    current_spread = spread[-1]
    zscore = (current_spread - spread_mean) / spread_std

    base = {
        "sym1":            sym1,
        "sym2":            sym2,
        "beta":            round(beta, 6),
        "zscore":          round(zscore, 3),
        "current_spread":  round(current_spread, 4),
        "spread_mean":     round(spread_mean, 4),
        "spread_std":      round(spread_std, 4),
    }

    abs_z = abs(zscore)

    if zscore < -entry_z:
        # Spread below mean → sym1 cheap relative to sym2
        confidence = min(abs_z / (entry_z + 1.0), 1.0)
        return {**base, "action": "LONG_SPREAD",
                "long_sym": sym1, "short_sym": sym2,
                "confidence": round(confidence, 3),
                "note": f"Spread at {zscore:.2f}σ below mean — buy {sym1}, short {sym2}"}
    elif zscore > entry_z:
        # Spread above mean → sym1 expensive relative to sym2
        confidence = min(abs_z / (entry_z + 1.0), 1.0)
        return {**base, "action": "SHORT_SPREAD",
                "long_sym": sym2, "short_sym": sym1,
                "confidence": round(confidence, 3),
                "note": f"Spread at {zscore:.2f}σ above mean — short {sym1}, buy {sym2}"}
    elif abs_z < exit_z:
        return {**base, "action": "EXIT_ZONE",
                "note": f"Spread at {zscore:.2f}σ — within exit zone, close any open position"}
    else:
        return {**base, "action": "NONE",
                "note": f"Spread at {zscore:.2f}σ — inside entry threshold, no action"}


def compute_pairs_levels(
    long_sym: str,
    short_sym: str,
    series: dict[str, list[float]],
    beta: float,
    zscore: float,
    spread_std: float,
    account_value: float = 10_000.0,
    risk_pct: float = 0.01,
    exit_z: float = 0.5,
) -> dict:
    """
    Compute position sizes and risk levels for a pairs trade.

    Dollar-neutral: long_value ≈ short_value.
    Risk is defined as: spread moving to stop_z instead of reverting to exit_z.

    Returns:
        {long_sym, short_sym, long_price, short_price,
         long_qty, short_qty, long_value, short_value,
         stop_z, target_z, risk_dollars, expected_r}
    """
    long_price  = series[long_sym][-1]
    short_price = series[short_sym][-1]

    if not long_price or not short_price:
        return {"error": "Missing current price for position sizing"}

    # Dollar risk budget
    risk_dollars = account_value * risk_pct

    # Stop: spread widens another 1σ beyond current z (mean-reversion fails)
    stop_z   = abs(zscore) + 1.0
    # Target: spread mean-reverts to exit zone
    target_z = exit_z

    # Expected spread move in dollars at stop vs target
    # For long spread: PnL ≈ (beta × short_price_move - long_price_move)
    # Simplified: risk per spread unit = spread_std × 1σ stop extension
    spread_risk_per_unit = spread_std * 1.0  # 1σ adverse move
    if spread_risk_per_unit <= 0:
        return {"error": "Spread std is zero, cannot size position"}

    # Number of spread units: risk_dollars / dollars_at_risk_per_unit
    # 1 spread unit = 1 share of long_sym, beta shares of short_sym
    # Approximate dollar risk per spread unit ≈ spread_risk_per_unit
    units = risk_dollars / spread_risk_per_unit
    units = max(1.0, round(units, 1))

    long_qty  = round(units)
    short_qty = round(units * beta)

    long_value  = round(long_qty * long_price, 2)
    short_value = round(short_qty * short_price, 2)

    # Expected R: (current_z - target_z) / (stop_z - current_z)
    current_abs_z = abs(zscore)
    reward = max(current_abs_z - target_z, 0)
    risk   = max(stop_z - current_abs_z, 0.01)
    expected_r = round(reward / risk, 2)

    return {
        "long_sym":    long_sym,
        "short_sym":   short_sym,
        "long_price":  round(long_price, 4),
        "short_price": round(short_price, 4),
        "long_qty":    long_qty,
        "short_qty":   short_qty,
        "long_value":  long_value,
        "short_value": short_value,
        "beta":        round(beta, 4),
        "stop_z":      round(stop_z, 2),
        "target_z":    round(target_z, 2),
        "risk_dollars": round(risk_dollars, 2),
        "expected_r":  expected_r,
    }
