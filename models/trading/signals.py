"""
Trading signal computation from OHLCV price history.
All indicators derived from raw price data — no external deps beyond numpy/pandas.

Signals produced:
  trend       : direction + strength from moving average alignment
  momentum    : RSI(14), rate of change
  volatility  : ATR(14), historical vol, Bollinger Bands
  volume      : relative volume vs 20-day avg
  expected_move: ±% range for next session (1σ based on HV)
  support_resistance: key price levels
  score       : composite bull/bear score -100 to +100
"""
from __future__ import annotations
import math
from typing import Optional
import numpy as np


def compute_signals(history: list[dict]) -> dict:
    """
    Compute all trading signals from OHLCV history list.
    history: list of {date, open, high, low, close, volume} dicts, oldest first.
    Returns a signals dict ready for the AI agent and UI.
    """
    if len(history) < 14:
        return {"error": "Need at least 14 days of history"}

    closes  = np.array([d["close"]  for d in history], dtype=float)
    highs   = np.array([d["high"]   for d in history], dtype=float)
    lows    = np.array([d["low"]    for d in history], dtype=float)
    volumes = np.array([d["volume"] for d in history], dtype=float)

    result = {}

    # ── Trend (moving average alignment) ─────────────────────────────────────
    result["trend"] = _trend(closes)

    # ── Momentum (RSI + Rate of Change) ──────────────────────────────────────
    result["momentum"] = _momentum(closes)

    # ── Volatility (ATR + Historical Vol + Bollinger) ─────────────────────────
    result["volatility"] = _volatility(closes, highs, lows)

    # ── Volume ────────────────────────────────────────────────────────────────
    result["volume"] = _volume(volumes)

    # ── Expected move (next session) ─────────────────────────────────────────
    result["expected_move"] = _expected_move(closes)

    # ── Support / Resistance ──────────────────────────────────────────────────
    result["support_resistance"] = _support_resistance(closes, highs, lows)

    # ── Composite score ───────────────────────────────────────────────────────
    result["score"] = _composite_score(result)

    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sma(arr: np.ndarray, n: int) -> float:
    return float(arr[-n:].mean()) if len(arr) >= n else float(arr.mean())


def _trend(closes: np.ndarray) -> dict:
    price = closes[-1]
    signals = []
    direction_votes = 0

    for period, weight in [(20, 1), (50, 2), (200, 3)]:
        if len(closes) >= period:
            ma = _sma(closes, period)
            above = price > ma
            pct_diff = (price - ma) / ma * 100
            signals.append({
                "ma": period,
                "value": round(ma, 4),
                "price_vs_ma": round(pct_diff, 2),
                "above": above,
            })
            direction_votes += weight if above else -weight

    max_votes = sum(w for _, w in [(20,1),(50,2),(200,3)] if len(closes) >= p for p, w in [(20,1),(50,2),(200,3)] if _ == p)
    max_votes = 6  # 1+2+3
    strength = abs(direction_votes) / max_votes * 100

    if direction_votes > 0:
        direction = "bullish"
    elif direction_votes < 0:
        direction = "bearish"
    else:
        direction = "neutral"

    return {
        "direction": direction,
        "strength":  round(strength, 1),
        "votes":     direction_votes,
        "mas":       signals,
    }


def _rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-(period+1):])
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(float(100 - 100 / (1 + rs)), 2)


def _momentum(closes: np.ndarray) -> dict:
    rsi = _rsi(closes)
    roc5  = round((closes[-1] / closes[-6]  - 1) * 100, 2) if len(closes) >= 6  else None
    roc20 = round((closes[-1] / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else None

    if rsi >= 70:
        rsi_signal = "overbought"
    elif rsi <= 30:
        rsi_signal = "oversold"
    elif rsi >= 55:
        rsi_signal = "bullish"
    elif rsi <= 45:
        rsi_signal = "bearish"
    else:
        rsi_signal = "neutral"

    return {
        "rsi":        rsi,
        "rsi_signal": rsi_signal,
        "roc_5d":     roc5,
        "roc_20d":    roc20,
    }


def _atr(closes: np.ndarray, highs: np.ndarray, lows: np.ndarray, period: int = 14) -> float:
    trs = []
    for i in range(1, min(period + 1, len(closes))):
        tr = max(
            highs[-i] - lows[-i],
            abs(highs[-i] - closes[-i-1]),
            abs(lows[-i]  - closes[-i-1]),
        )
        trs.append(tr)
    return float(np.mean(trs)) if trs else 0.0


def _volatility(closes: np.ndarray, highs: np.ndarray, lows: np.ndarray) -> dict:
    atr = _atr(closes, highs, lows)
    atr_pct = round(atr / closes[-1] * 100, 2)

    # Historical volatility (20-day annualised)
    if len(closes) >= 21:
        log_returns = np.diff(np.log(closes[-21:]))
        hv_daily = float(log_returns.std())
        hv_annual = round(hv_daily * math.sqrt(252) * 100, 2)
    else:
        hv_annual = None

    # Bollinger Bands (20, 2σ)
    bb = {}
    if len(closes) >= 20:
        ma20  = _sma(closes, 20)
        std20 = float(closes[-20:].std())
        bb = {
            "upper": round(ma20 + 2 * std20, 4),
            "mid":   round(ma20, 4),
            "lower": round(ma20 - 2 * std20, 4),
            "pct_b": round((closes[-1] - (ma20 - 2*std20)) / (4 * std20) * 100, 1) if std20 > 0 else 50,
        }

    return {
        "atr":        round(atr, 4),
        "atr_pct":    atr_pct,
        "hv_annual":  hv_annual,
        "bollinger":  bb,
    }


def _volume(volumes: np.ndarray) -> dict:
    current = int(volumes[-1])
    avg20   = float(volumes[-20:].mean()) if len(volumes) >= 20 else float(volumes.mean())
    rel_vol = round(current / avg20, 2) if avg20 > 0 else 1.0

    if rel_vol >= 2.0:
        signal = "very_high"
    elif rel_vol >= 1.5:
        signal = "high"
    elif rel_vol >= 0.8:
        signal = "normal"
    else:
        signal = "low"

    return {
        "current":  current,
        "avg_20d":  int(avg20),
        "relative": rel_vol,
        "signal":   signal,
    }


def _expected_move(closes: np.ndarray, days_ahead: int = 1) -> dict:
    """±1σ expected move for next N trading sessions based on 20-day HV."""
    if len(closes) < 21:
        return {}
    log_returns = np.diff(np.log(closes[-21:]))
    daily_vol   = float(log_returns.std())
    move_vol    = daily_vol * math.sqrt(days_ahead)
    price       = closes[-1]
    return {
        "days":        days_ahead,
        "pct_1sigma":  round(move_vol * 100, 2),
        "upper_1sigma": round(price * (1 + move_vol), 4),
        "lower_1sigma": round(price * (1 - move_vol), 4),
        "pct_2sigma":  round(move_vol * 2 * 100, 2),
        "upper_2sigma": round(price * (1 + move_vol * 2), 4),
        "lower_2sigma": round(price * (1 - move_vol * 2), 4),
        "prob_up":     round(50 + (log_returns.mean() / move_vol * 15 if move_vol > 0 else 0), 1),
    }


def _support_resistance(closes: np.ndarray, highs: np.ndarray, lows: np.ndarray) -> dict:
    """Simple swing high/low support and resistance levels."""
    window = min(20, len(closes) // 3)
    if window < 3:
        return {}

    price = closes[-1]
    recent_highs = highs[-60:] if len(highs) >= 60 else highs
    recent_lows  = lows[-60:]  if len(lows)  >= 60 else lows

    resistance = round(float(recent_highs.max()), 4)
    support    = round(float(recent_lows.min()),  4)

    # Nearest levels
    mid_resistance = round(float(np.percentile(recent_highs, 75)), 4)
    mid_support    = round(float(np.percentile(recent_lows,  25)), 4)

    pct_to_resistance = round((resistance - price) / price * 100, 2)
    pct_to_support    = round((price - support)    / price * 100, 2)

    return {
        "resistance":       resistance,
        "mid_resistance":   mid_resistance,
        "support":          support,
        "mid_support":      mid_support,
        "pct_to_resistance": pct_to_resistance,
        "pct_to_support":   pct_to_support,
    }


def _composite_score(signals: dict) -> dict:
    """
    Composite bull/bear score from -100 (max bearish) to +100 (max bullish).
    Weighted average of trend, momentum, volatility position.
    """
    score = 0.0
    reasons = []

    # Trend (40% weight)
    trend = signals.get("trend", {})
    if trend.get("direction") == "bullish":
        pts = trend.get("strength", 50) * 0.4
        score += pts
        reasons.append(f"Trend bullish ({trend.get('strength', 0):.0f}% strength)")
    elif trend.get("direction") == "bearish":
        pts = trend.get("strength", 50) * 0.4
        score -= pts
        reasons.append(f"Trend bearish ({trend.get('strength', 0):.0f}% strength)")

    # Momentum RSI (30% weight)
    mom = signals.get("momentum", {})
    rsi = mom.get("rsi", 50)
    rsi_contrib = (rsi - 50) / 50 * 30
    score += rsi_contrib
    reasons.append(f"RSI {rsi} ({mom.get('rsi_signal','neutral')})")

    # Rate of change 20d (20% weight)
    roc20 = mom.get("roc_20d")
    if roc20 is not None:
        roc_contrib = max(min(roc20 * 2, 20), -20)
        score += roc_contrib
        reasons.append(f"20d return {roc20:+.1f}%")

    # Bollinger %B (10% weight)
    bb = signals.get("volatility", {}).get("bollinger", {})
    pct_b = bb.get("pct_b")
    if pct_b is not None:
        bb_contrib = (pct_b - 50) / 50 * 10
        score += bb_contrib

    score = round(max(-100, min(100, score)), 1)

    if score >= 60:
        label = "Strong Buy"
        color = "green"
    elif score >= 20:
        label = "Buy"
        color = "green"
    elif score <= -60:
        label = "Strong Sell"
        color = "red"
    elif score <= -20:
        label = "Sell"
        color = "red"
    else:
        label = "Neutral"
        color = "amber"

    return {
        "value":   score,
        "label":   label,
        "color":   color,
        "reasons": reasons,
    }
