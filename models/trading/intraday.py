"""
Intraday signal engine — Simons-inspired ensemble approach.
Each signal is weak on its own; the combination is the edge.

Signals computed:
  1. VWAP deviation      — price vs cumulative VWAP (mean reversion / momentum)
  2. Opening range       — breakout above/below first-15min range
  3. RSI-9               — short-period momentum oscillator
  4. Relative volume     — today's vol vs 20-day avg (confirmation)
  5. Gap fill            — pre-market gap direction and fill probability
  6. ATR stop levels     — entry / stop / target price levels
  7. Trend bias          — daily MA context (are we above/below MA20?)
  8. Bollinger %B        — position within bands (overbought/oversold)
  9. Price vs prev close — directional bias + strength
 10. Volume surge        — abnormal volume in last N bars
"""
from __future__ import annotations
import math
from typing import Optional


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return [values[-1]] * len(values) if values else []
    k = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    # Pad front so output length matches input
    pad = len(values) - len(result)
    return [result[0]] * pad + result


def _rsi(closes: list[float], period: int = 9) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def _vwap(bars: list[dict]) -> list[float]:
    """Cumulative VWAP from first bar of session."""
    cum_pv = 0.0
    cum_v = 0.0
    result = []
    for b in bars:
        typical = (b["h"] + b["l"] + b["c"]) / 3
        vol = b.get("v", 0)
        cum_pv += typical * vol
        cum_v += vol
        result.append(cum_pv / cum_v if cum_v > 0 else typical)
    return result


def _atr(bars: list[dict], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        high = bars[i]["h"]
        low = bars[i]["l"]
        prev_close = bars[i - 1]["c"]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs[-period:]) / min(len(trs), period)


def _bollinger(closes: list[float], period: int = 20) -> dict:
    if len(closes) < period:
        mid = closes[-1] if closes else 0
        return {"upper": mid, "mid": mid, "lower": mid, "pct_b": 0.5}
    window = closes[-period:]
    mid = sum(window) / period
    std = math.sqrt(sum((c - mid) ** 2 for c in window) / period)
    upper = mid + 2 * std
    lower = mid - 2 * std
    price = closes[-1]
    pct_b = (price - lower) / (upper - lower) if upper != lower else 0.5
    return {
        "upper": round(upper, 4),
        "mid": round(mid, 4),
        "lower": round(lower, 4),
        "pct_b": round(pct_b, 3),
    }


# ── Individual signals ────────────────────────────────────────────────────────

def _sig_vwap(bars: list[dict], snapshot: dict) -> dict:
    vwaps = _vwap(bars)
    if not vwaps:
        return {"value": 0, "label": "No data", "score": 0}
    vwap_now = vwaps[-1]
    price = snapshot.get("price", bars[-1]["c"] if bars else 0)
    dev_pct = (price - vwap_now) / vwap_now * 100 if vwap_now else 0
    # Above VWAP = bullish, below = bearish
    # Extreme deviation (>1%) suggests overextension — mean reversion risk
    if dev_pct > 1.5:
        score, label = -20, "Overextended above VWAP"
    elif dev_pct > 0.3:
        score, label = 15, "Above VWAP"
    elif dev_pct > -0.3:
        score, label = 0, "At VWAP"
    elif dev_pct > -1.5:
        score, label = -15, "Below VWAP"
    else:
        score, label = 20, "Oversold below VWAP"
    return {
        "vwap": round(vwap_now, 4),
        "deviation_pct": round(dev_pct, 3),
        "label": label,
        "score": score,
    }


def _sig_opening_range(bars: list[dict]) -> dict:
    """First 15 minutes = opening range. Breakout direction = day bias."""
    or_bars = bars[:3] if len(bars) >= 3 else bars  # 3 × 5-min = 15 min
    if not or_bars:
        return {"or_high": 0, "or_low": 0, "breakout": "none", "score": 0}
    or_high = max(b["h"] for b in or_bars)
    or_low = min(b["l"] for b in or_bars)
    price = bars[-1]["c"] if bars else 0
    or_range = or_high - or_low
    if price > or_high:
        pct_above = (price - or_high) / or_range * 100 if or_range else 0
        score = min(25, int(pct_above * 5))
        label = "Bullish breakout"
    elif price < or_low:
        pct_below = (or_low - price) / or_range * 100 if or_range else 0
        score = -min(25, int(pct_below * 5))
        label = "Bearish breakdown"
    else:
        score = 0
        label = "Inside range"
    return {
        "or_high": round(or_high, 4),
        "or_low": round(or_low, 4),
        "or_range": round(or_range, 4),
        "label": label,
        "score": score,
    }


def _sig_rsi(bars: list[dict]) -> dict:
    closes = [b["c"] for b in bars]
    rsi = _rsi(closes, 9)
    if rsi >= 75:
        score, label = -25, "Overbought"
    elif rsi >= 60:
        score, label = 10, "Bullish"
    elif rsi >= 45:
        score, label = 5, "Neutral-Bullish"
    elif rsi >= 35:
        score, label = -5, "Neutral-Bearish"
    elif rsi >= 25:
        score, label = -10, "Bearish"
    else:
        score, label = 25, "Oversold"
    return {"rsi9": rsi, "label": label, "score": score}


def _sig_relative_volume(bars: list[dict], daily_avg_volume: float) -> dict:
    """Volume today vs historical average — confirms signal strength."""
    today_volume = sum(b.get("v", 0) for b in bars)
    rel_vol = today_volume / daily_avg_volume if daily_avg_volume > 0 else 1.0
    if rel_vol >= 3.0:
        score, label = 20, "Extreme volume"
    elif rel_vol >= 2.0:
        score, label = 15, "High volume"
    elif rel_vol >= 1.2:
        score, label = 5, "Above average"
    elif rel_vol >= 0.8:
        score, label = 0, "Normal volume"
    else:
        score, label = -10, "Low volume"
    return {
        "today_volume": today_volume,
        "avg_volume": round(daily_avg_volume),
        "rel_vol": round(rel_vol, 2),
        "label": label,
        "score": score,
    }


def _sig_gap(snapshot: dict) -> dict:
    """Pre-market / open gap vs previous close."""
    open_price = snapshot.get("open", 0)
    prev_close = snapshot.get("prev_close", 0)
    if not open_price or not prev_close:
        return {"gap_pct": 0, "direction": "none", "fill_prob": 0.5, "score": 0}
    gap_pct = (open_price - prev_close) / prev_close * 100
    # Gaps >2% fill ~65% of the time intraday (mean reversion)
    # Gaps <0.5% are noise
    fill_prob = 0.65 if abs(gap_pct) > 2 else 0.45 if abs(gap_pct) > 0.5 else 0.3
    if gap_pct > 2:
        score, direction = -10, "gap_up"   # fading gap up
    elif gap_pct > 0.5:
        score, direction = 10, "gap_up"
    elif gap_pct < -2:
        score, direction = 10, "gap_down"  # fading gap down
    elif gap_pct < -0.5:
        score, direction = -10, "gap_down"
    else:
        score, direction = 0, "flat"
    return {
        "gap_pct": round(gap_pct, 2),
        "direction": direction,
        "fill_prob": fill_prob,
        "score": score,
    }


def _sig_trend_bias(daily_bars: list[dict]) -> dict:
    """Daily MA20 context — are we in a bullish or bearish regime?"""
    if len(daily_bars) < 20:
        return {"ma20": 0, "above_ma20": None, "score": 0, "label": "Insufficient data"}
    closes = [b["c"] for b in daily_bars]
    ma20 = sum(closes[-20:]) / 20
    ma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else ma20
    price = closes[-1]
    above_ma20 = price > ma20
    above_ma50 = price > ma50
    if above_ma20 and above_ma50:
        score, label = 20, "Strong uptrend"
    elif above_ma20:
        score, label = 10, "Short-term uptrend"
    elif above_ma50:
        score, label = -5, "Below MA20 but above MA50"
    else:
        score, label = -20, "Downtrend"
    return {
        "ma20": round(ma20, 4),
        "ma50": round(ma50, 4),
        "above_ma20": above_ma20,
        "above_ma50": above_ma50,
        "label": label,
        "score": score,
    }


def _sig_bollinger(bars: list[dict]) -> dict:
    closes = [b["c"] for b in bars]
    bb = _bollinger(closes, 20)
    pct_b = bb["pct_b"]
    if pct_b > 1.0:
        score, label = -20, "Above upper band — overbought"
    elif pct_b > 0.8:
        score, label = -10, "Near upper band"
    elif pct_b > 0.5:
        score, label = 10, "Upper half"
    elif pct_b > 0.2:
        score, label = -5, "Lower half"
    elif pct_b > 0.0:
        score, label = 15, "Near lower band"
    else:
        score, label = 20, "Below lower band — oversold"
    return {**bb, "label": label, "score": score}


def _sig_volume_surge(bars: list[dict]) -> dict:
    """Check if last 3 bars have a volume spike vs session average."""
    if len(bars) < 6:
        return {"surge": False, "score": 0, "label": "Not enough bars"}
    vols = [b.get("v", 0) for b in bars]
    avg = sum(vols[:-3]) / max(len(vols) - 3, 1)
    recent_avg = sum(vols[-3:]) / 3
    ratio = recent_avg / avg if avg > 0 else 1.0
    surge = ratio >= 2.0
    score = 10 if surge else 0
    return {
        "surge": surge,
        "recent_avg_vol": round(recent_avg),
        "session_avg_vol": round(avg),
        "ratio": round(ratio, 2),
        "label": "Volume surge" if surge else "Normal",
        "score": score,
    }


# ── ATR-based trade levels ─────────────────────────────────────────────────────

def _trade_levels(bars: list[dict], snapshot: dict, side: str) -> dict:
    """
    Calculate entry / stop / target levels based on ATR.
    side: "long" or "short"
    """
    atr = _atr(bars, 14)
    price = snapshot.get("price", bars[-1]["c"] if bars else 0)
    if not atr or not price:
        return {}
    if side == "long":
        entry = round(price, 2)
        stop = round(price - 1.5 * atr, 2)
        target1 = round(price + 1.5 * atr, 2)
        target2 = round(price + 2.5 * atr, 2)
    else:
        entry = round(price, 2)
        stop = round(price + 1.5 * atr, 2)
        target1 = round(price - 1.5 * atr, 2)
        target2 = round(price - 2.5 * atr, 2)
    risk = abs(entry - stop)
    reward1 = abs(target1 - entry)
    rr1 = round(reward1 / risk, 2) if risk else 0
    return {
        "side": side,
        "entry": entry,
        "stop": stop,
        "target1": target1,
        "target2": target2,
        "atr": round(atr, 4),
        "risk_per_share": round(risk, 4),
        "rr_ratio": rr1,
    }


# ── Ensemble scorer ────────────────────────────────────────────────────────────

# Signal weights (sum to 100 conceptually, but we normalize)
WEIGHTS = {
    "vwap":     0.20,
    "or":       0.15,
    "rsi":      0.15,
    "relvol":   0.10,
    "gap":      0.10,
    "trend":    0.15,
    "bollinger":0.10,
    "volsurge": 0.05,
}

SCORE_LABELS = [
    (60,  "Strong Buy"),
    (20,  "Buy"),
    (-20, "Neutral"),
    (-60, "Sell"),
    (-101,"Strong Sell"),
]


def _composite(signals: dict) -> dict:
    raw = (
        signals["vwap"]["score"]     * WEIGHTS["vwap"] +
        signals["or"]["score"]       * WEIGHTS["or"] +
        signals["rsi"]["score"]      * WEIGHTS["rsi"] +
        signals["relvol"]["score"]   * WEIGHTS["relvol"] +
        signals["gap"]["score"]      * WEIGHTS["gap"] +
        signals["trend"]["score"]    * WEIGHTS["trend"] +
        signals["bollinger"]["score"]* WEIGHTS["bollinger"] +
        signals["volsurge"]["score"] * WEIGHTS["volsurge"]
    )
    # Normalize to -100..+100
    max_possible = sum(
        abs(s) * w for s, w in [
            (25, WEIGHTS["vwap"]), (25, WEIGHTS["or"]), (25, WEIGHTS["rsi"]),
            (20, WEIGHTS["relvol"]), (10, WEIGHTS["gap"]), (20, WEIGHTS["trend"]),
            (20, WEIGHTS["bollinger"]), (10, WEIGHTS["volsurge"])
        ]
    )
    value = round(raw / max_possible * 100) if max_possible else 0
    label = next(l for threshold, l in SCORE_LABELS if value >= threshold)
    reasons = []
    for name, sig in signals.items():
        if abs(sig.get("score", 0)) >= 10:
            reasons.append(sig.get("label", name))
    return {"value": value, "label": label, "reasons": reasons[:4]}


# ── Main entry point ──────────────────────────────────────────────────────────

def compute_intraday_signals(
    intraday_bars: list[dict],
    daily_bars: list[dict],
    snapshot: dict,
    daily_avg_volume: float = 0,
) -> dict:
    """
    Full intraday signal computation.
    Returns all signals + composite score + trade levels.
    """
    closes = [b["c"] for b in intraday_bars]
    if not closes:
        return {"error": "No intraday bars available"}

    sigs = {
        "vwap":     _sig_vwap(intraday_bars, snapshot),
        "or":       _sig_opening_range(intraday_bars),
        "rsi":      _sig_rsi(intraday_bars),
        "relvol":   _sig_relative_volume(intraday_bars, daily_avg_volume),
        "gap":      _sig_gap(snapshot),
        "trend":    _sig_trend_bias(daily_bars),
        "bollinger":_sig_bollinger(intraday_bars),
        "volsurge": _sig_volume_surge(intraday_bars),
    }

    score = _composite(sigs)
    side = "long" if score["value"] >= 0 else "short"
    levels = _trade_levels(intraday_bars, snapshot, side)

    return {
        "signals": sigs,
        "score": score,
        "levels": levels,
    }
