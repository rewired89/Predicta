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


def _macd(closes: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD(12,26,9) with correct EMA warm-up from bar `fast` through bar `slow`."""
    if len(closes) < slow + signal + 1:
        return {
            "macd": 0.0, "signal_line": 0.0, "histogram": 0.0,
            "direction": "neutral", "crossover": "none",
        }
    k_fast = 2 / (fast + 1)
    k_slow = 2 / (slow + 1)
    k_sig  = 2 / (signal + 1)

    # Warm EMA12 from bar fast through bar slow-1 before MACD line starts
    ema_f = float(closes[:fast].mean())
    for v in closes[fast:slow]:
        ema_f = float(v) * k_fast + ema_f * (1 - k_fast)
    ema_s = float(closes[:slow].mean())

    macd_vals: list[float] = []
    for v in closes[slow:]:
        ema_f = float(v) * k_fast + ema_f * (1 - k_fast)
        ema_s = float(v) * k_slow + ema_s * (1 - k_slow)
        macd_vals.append(ema_f - ema_s)

    if len(macd_vals) < signal + 1:
        return {
            "macd": 0.0, "signal_line": 0.0, "histogram": 0.0,
            "direction": "neutral", "crossover": "none",
        }

    sig_line = sum(macd_vals[:signal]) / signal
    hists: list[float] = []
    for v in macd_vals[signal:]:
        sig_line = v * k_sig + sig_line * (1 - k_sig)
        hists.append(v - sig_line)

    hist_now  = hists[-1]
    hist_prev = hists[-2] if len(hists) >= 2 else 0.0
    direction = "bullish" if hist_now > 0 else "bearish" if hist_now < 0 else "neutral"
    if hist_now > 0 and hist_prev <= 0:
        crossover = "bullish"
    elif hist_now < 0 and hist_prev >= 0:
        crossover = "bearish"
    else:
        crossover = "none"
    return {
        "macd":        round(macd_vals[-1], 6),
        "signal_line": round(sig_line, 6),
        "histogram":   round(hist_now, 6),
        "direction":   direction,
        "crossover":   crossover,
    }


def _momentum(closes: np.ndarray) -> dict:
    rsi   = _rsi(closes)
    roc5  = round((closes[-1] / closes[-6]  - 1) * 100, 2) if len(closes) >= 6  else None
    roc20 = round((closes[-1] / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else None
    macd  = _macd(closes)

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
        "macd":       macd,
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
    # z-score of recent drift vs vol — 1 std dev shift ≈ 16pp probability swing
    z_score = float(log_returns.mean()) / daily_vol if daily_vol > 0 else 0.0
    prob_up = round(max(20.0, min(80.0, 50.0 + z_score * 16)), 1)
    return {
        "days":        days_ahead,
        "pct_1sigma":  round(move_vol * 100, 2),
        "upper_1sigma": round(price * (1 + move_vol), 4),
        "lower_1sigma": round(price * (1 - move_vol), 4),
        "pct_2sigma":  round(move_vol * 2 * 100, 2),
        "upper_2sigma": round(price * (1 + move_vol * 2), 4),
        "lower_2sigma": round(price * (1 - move_vol * 2), 4),
        "prob_up":     prob_up,
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

    Regime-adaptive weights: high-vol (HV>30%) favours mean-reversion signals
    (RSI, Bollinger); low-vol (HV<15%) favours trend-following.

    RSI uses non-linear mapping with a 30–70 dead zone — no signal in the
    noisy middle; acceleration only at genuine extremes (overbought/oversold).

    Bollinger %B uses mean-reversion direction: lower band = oversold = bullish,
    upper band = overbought = bearish.
    """
    reasons = []

    # ── Volatility regime → adaptive weights (all sum to 1.0) ────────────────
    hv = signals.get("volatility", {}).get("hv_annual")
    if hv is not None:
        if hv > 30:
            w = {"trend": 0.15, "rsi": 0.30, "roc": 0.20, "bb": 0.15, "macd": 0.20}
            reasons.append(f"High-vol regime (HV={hv:.0f}%): mean-reversion weights")
        elif hv < 15:
            w = {"trend": 0.40, "rsi": 0.15, "roc": 0.15, "bb": 0.10, "macd": 0.20}
            reasons.append(f"Low-vol regime (HV={hv:.0f}%): trend-following weights")
        else:
            w = {"trend": 0.30, "rsi": 0.25, "roc": 0.15, "bb": 0.10, "macd": 0.20}
    else:
        w = {"trend": 0.30, "rsi": 0.25, "roc": 0.15, "bb": 0.10, "macd": 0.20}

    score = 0.0

    # ── Trend: direction × strength → raw -100..+100, scaled by weight ────────
    trend = signals.get("trend", {})
    direction = trend.get("direction", "neutral")
    strength  = trend.get("strength", 50)
    if direction == "bullish":
        score += strength * w["trend"]
        reasons.append(f"Trend bullish ({strength:.0f}% strength)")
    elif direction == "bearish":
        score -= strength * w["trend"]
        reasons.append(f"Trend bearish ({strength:.0f}% strength)")

    # ── RSI: non-linear, dead zone 30–70 ──────────────────────────────────────
    # Overbought (>70) → mean-reversion → bearish; oversold (<30) → bullish.
    # 40–60 band is noise — contributes nothing.
    mom = signals.get("momentum", {})
    rsi = mom.get("rsi", 50)
    if rsi >= 70:
        rsi_raw = -min(100.0, (rsi - 70) * 5)   # 70→0, 80→-50, 90→-100
    elif rsi <= 30:
        rsi_raw = min(100.0, (30 - rsi) * 5)    # 30→0, 20→50, 10→100
    else:
        rsi_raw = 0.0
    score += rsi_raw * w["rsi"]
    reasons.append(f"RSI {rsi} ({mom.get('rsi_signal', 'neutral')})")

    # ── Rate of change 20d ─────────────────────────────────────────────────────
    roc20 = mom.get("roc_20d")
    if roc20 is not None:
        roc_raw = max(-100.0, min(100.0, roc20 * 5))  # ±20% return → ±100
        score += roc_raw * w["roc"]
        reasons.append(f"20d return {roc20:+.1f}%")

    # ── Bollinger %B: mean-reversion (lower band = oversold = bullish) ─────────
    # pct_b is 0-100: 0 = price at lower band, 100 = price at upper band.
    bb = signals.get("volatility", {}).get("bollinger", {})
    pct_b = bb.get("pct_b")
    if pct_b is not None:
        bb_raw = -(pct_b - 50) / 50 * 100  # invert: lower band → +100, upper → -100
        score += bb_raw * w["bb"]

    # ── MACD: crossover = ±100, sustained direction = ±50 ────────────────────
    macd_data = signals.get("momentum", {}).get("macd", {})
    crossover  = macd_data.get("crossover", "none")
    macd_dir   = macd_data.get("direction", "neutral")
    if crossover == "bullish":
        macd_raw = 100.0
        reasons.append("MACD bullish crossover")
    elif crossover == "bearish":
        macd_raw = -100.0
        reasons.append("MACD bearish crossover")
    elif macd_dir == "bullish":
        macd_raw = 50.0
        reasons.append("MACD bullish")
    elif macd_dir == "bearish":
        macd_raw = -50.0
        reasons.append("MACD bearish")
    else:
        macd_raw = 0.0
    score += macd_raw * w["macd"]

    score = round(max(-100.0, min(100.0, score)), 1)

    if score >= 60:
        label, color = "Strong Buy", "green"
    elif score >= 20:
        label, color = "Buy", "green"
    elif score <= -60:
        label, color = "Strong Sell", "red"
    elif score <= -20:
        label, color = "Sell", "red"
    else:
        label, color = "Neutral", "amber"

    return {"value": score, "label": label, "color": color, "reasons": reasons}
