"""
Intraday signal engine — Simons-inspired ensemble approach.
Each signal is weak on its own; the combination is the edge.

Signals computed:
  1. VWAP deviation        — price vs cumulative VWAP (mean reversion / momentum)
  2. Opening range         — breakout above/below first-15min range
  3. RSI-9                 — short-period momentum oscillator
  4. Relative volume       — time-of-day-adjusted vol vs historical average
  5. Gap fill              — pre-market gap direction and fill probability
  6. ATR stop levels       — entry / stop / target price levels
  7. Trend bias            — daily MA context (are we above/below MA20?)
  8. Bollinger %B          — position within bands (overbought/oversold)
  9. Volume surge          — abnormal volume in last N bars

Post-composite adjustments:
  - Liquidity filter       — wide spread penalty or hard reject (>0.3%)
  - Time-of-day modifier   — reduce confidence during lunch chop / open noise
"""
from __future__ import annotations
import math
from datetime import datetime
from typing import Optional

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _ET = _ZoneInfo("America/New_York")
except ImportError:
    _ET = None  # fallback: use UTC-4 approximation


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


def _intraday_vol_curve(bar_timestamp: str) -> float:
    """
    Estimate what fraction of daily volume has typically occurred by this
    bar's timestamp. Volume follows a U-shape: heavy at open/close, thin
    at lunch. Returns expected fraction (0–1); defaults to 1.0 on error.
    """
    try:
        dt = datetime.fromisoformat(bar_timestamp.replace("Z", "+00:00"))
        if _ET:
            dt_et = dt.astimezone(_ET)
        else:
            from datetime import timezone, timedelta
            dt_et = dt.astimezone(timezone(timedelta(hours=-4)))
        h = dt_et.hour + dt_et.minute / 60
        if h < 10.5:    # 9:30–10:30: first-hour ramp (~25% of day)
            return max(0.01, 0.25 * (h - 9.5))
        elif h < 14.0:  # 10:30–14:00: steady mid-session (+15%)
            return 0.25 + 0.15 * (h - 10.5) / 3.5
        else:           # 14:00–16:00: close surge (+35%)
            return min(1.0, 0.40 + 0.35 * (h - 14.0) / 2.0)
    except Exception:
        return 1.0


def _sig_relative_volume(bars: list[dict], daily_avg_volume: float) -> dict:
    """
    Volume today vs historical average, adjusted for intraday time-of-day
    seasonality. Raw cumulative volume understates opening-hour activity
    (only ~25% of day done) and overstates afternoon volume without adjustment.
    """
    today_volume = sum(b.get("v", 0) for b in bars)
    expected_pct = 1.0
    if bars:
        ts = bars[-1].get("t", "")
        if ts:
            expected_pct = _intraday_vol_curve(ts)
    expected_vol = daily_avg_volume * expected_pct if daily_avg_volume > 0 else 0
    rel_vol = today_volume / expected_vol if expected_vol > 0 else 1.0
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
        "expected_pct_of_day": round(expected_pct, 3),
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


# ── Liquidity filter ──────────────────────────────────────────────────────────

def _liquidity_score(snapshot: dict) -> dict:
    """
    Spread-based liquidity filter. Day trading edge is 10–30 bps;
    a 30 bps spread consumes the entire profit margin.
    Hard reject at >0.3% spread. Applies a penalty multiplier at 0.1–0.3%.
    """
    bid   = snapshot.get("bid", 0)
    ask   = snapshot.get("ask", 0)
    price = snapshot.get("price", 0)
    if not bid or not ask or not price or ask <= bid:
        return {"score": 0, "label": "UNKNOWN", "spread_pct": 0.0, "estimated_slippage_pct": 0.0}
    spread_pct  = (ask - bid) / price * 100
    slippage_pct = spread_pct / 2  # market order hits mid-spread
    if spread_pct > 0.5:
        score, label = -100, "UNTRADEABLE"
    elif spread_pct > 0.3:
        score, label = -60, "WIDE_SPREAD"
    elif spread_pct > 0.1:
        score, label = -20, "ELEVATED_SPREAD"
    else:
        score, label = 0, "LIQUID"
    return {
        "score": score,
        "label": label,
        "bid": bid,
        "ask": ask,
        "spread_pct": round(spread_pct, 4),
        "estimated_slippage_pct": round(slippage_pct, 4),
    }


# ── Time-of-day regime ────────────────────────────────────────────────────────

def _time_of_day_modifier(bar_timestamp: str) -> float:
    """
    Multiplier (0.5–1.0) applied to composite score based on session quality.
    Lunch chop (11:30–14:00 ET) kills momentum signals.
    Returns 1.0 on parse failure so missing timestamps are safe.
    """
    try:
        dt = datetime.fromisoformat(bar_timestamp.replace("Z", "+00:00"))
        if _ET:
            dt_et = dt.astimezone(_ET)
        else:
            from datetime import timezone, timedelta
            dt_et = dt.astimezone(timezone(timedelta(hours=-4)))
        h = dt_et.hour + dt_et.minute / 60
        if 9.5 <= h < 10.0:
            return 0.7   # opening noise: fade extremes
        elif 10.0 <= h < 11.5:
            return 1.0   # prime trend window
        elif 11.5 <= h < 14.0:
            return 0.5   # lunch chop: severe penalty
        elif 14.0 <= h < 15.5:
            return 1.0   # afternoon continuation
        else:
            return 0.6   # close positioning / reversals
    except Exception:
        return 1.0


# ── Intraday expected move ────────────────────────────────────────────────────

def _intraday_expected_move(closes: list[float], hold_bars: int = 6) -> dict:
    """
    Expected price move for a specific hold period using per-bar volatility.
    At 5-min bars: hold_bars=6 = 30-min scalp, hold_bars=12 = 1-hour hold.
    Daily HV / sqrt(252) overestimates intraday moves by including overnight
    gaps; per-bar vol is pure intraday noise — correct for stop placement.
    """
    if len(closes) < 10:
        return {}
    log_rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            log_rets.append(math.log(closes[i] / closes[i - 1]))
    if not log_rets:
        return {}
    mean_r  = sum(log_rets) / len(log_rets)
    var_r   = sum((r - mean_r) ** 2 for r in log_rets) / len(log_rets)
    bar_vol = math.sqrt(var_r)
    hold_vol = bar_vol * math.sqrt(hold_bars)
    price    = closes[-1]
    return {
        "hold_bars":          hold_bars,
        "hold_minutes":       hold_bars * 5,
        "pct_1sigma":         round(hold_vol * 100, 3),
        "dollars_1sigma":     round(price * hold_vol, 4),
        "bar_vol_pct":        round(bar_vol * 100, 3),
        "suggested_stop_pct": round(hold_vol * 100, 3),
    }


# ── Exit rules (active position management) ───────────────────────────────────

def compute_exit_action(
    entry: float,
    stop: float,
    target1: float,
    current_price: float,
    bars_held: int,
    current_signals: dict,
    entry_score: float,
) -> dict:
    """
    Active exit logic for day trading. Call after each bar while position is open.
    Returns {"action": "EXIT"|"MODIFY_STOP"|"HOLD", "reason": ..., ...}.

    Three exit triggers:
      TIME_STOP      — no progress after 10 bars (50 min at 5-min bars)
      BREAKEVEN_LOCK — move stop to entry+1 tick after 1R profit
      SIGNAL_REVERSAL — composite score flipped sign vs entry direction
    """
    risk = abs(entry - stop)
    if risk == 0:
        return {"action": "HOLD", "reason": "ZERO_RISK"}
    long = stop < entry
    profit = (current_price - entry) if long else (entry - current_price)
    r_multiple = round(profit / risk, 2)

    if bars_held > 10 and abs(profit) < 0.3 * risk:
        return {"action": "EXIT", "reason": "TIME_STOP", "price": current_price, "r_multiple": r_multiple}

    if r_multiple > 1.0:
        tick = current_price * 0.0001
        new_stop = round((entry + tick) if long else (entry - tick), 4)
        return {"action": "MODIFY_STOP", "reason": "BREAKEVEN_LOCK", "new_stop": new_stop, "r_multiple": r_multiple}

    current_score = current_signals.get("score", {}).get("value", 0)
    if entry_score * current_score < 0 and abs(current_score) >= 20:
        return {
            "action": "EXIT", "reason": "SIGNAL_REVERSAL",
            "price": current_price, "entry_score": entry_score,
            "current_score": current_score, "r_multiple": r_multiple,
        }

    return {"action": "HOLD", "reason": "WITHIN_PARAMETERS", "r_multiple": r_multiple, "bars_held": bars_held}


# ── ATR-based trade levels ─────────────────────────────────────────────────────

def _trade_levels(bars: list[dict], snapshot: dict, side: str, trend_label: str = "neutral") -> dict:
    """
    Calculate entry / stop / target levels based on ATR.
    side: "long" or "short"
    trend_label: from _sig_trend_bias — widens stop/target in strong trends
    so normal noise doesn't stop out trend trades prematurely.
    """
    atr = _atr(bars, 14)
    price = snapshot.get("price", bars[-1]["c"] if bars else 0)
    if not atr or not price:
        return {}

    # Market structure adjusts ATR multiples
    label_lower = trend_label.lower()
    if "strong" in label_lower:
        stop_mult, t1_mult, t2_mult = 2.0, 2.0, 3.5   # trend trade: wider stop, bigger target
    elif "below" in label_lower or "downtrend" in label_lower:
        stop_mult, t1_mult, t2_mult = 1.5, 1.5, 2.5   # mean reversion: symmetric
    else:
        stop_mult, t1_mult, t2_mult = 1.5, 1.5, 2.5   # default

    if side == "long":
        entry   = round(price, 2)
        stop    = round(price - stop_mult * atr, 2)
        target1 = round(price + t1_mult * atr, 2)
        target2 = round(price + t2_mult * atr, 2)
    else:
        entry   = round(price, 2)
        stop    = round(price + stop_mult * atr, 2)
        target1 = round(price - t1_mult * atr, 2)
        target2 = round(price - t2_mult * atr, 2)

    risk    = abs(entry - stop)
    reward1 = abs(target1 - entry)
    rr1     = round(reward1 / risk, 2) if risk else 0
    return {
        "side":          side,
        "entry":         entry,
        "stop":          stop,
        "target1":       target1,
        "target2":       target2,
        "atr":           round(atr, 4),
        "risk_per_share": round(risk, 4),
        "rr_ratio":      rr1,
        "stop_mult":     stop_mult,
        "target_mult":   t2_mult,
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
    Returns signals + composite score (with liquidity & time-of-day adjustments)
    + ATR trade levels + intraday expected move.
    """
    closes = [b["c"] for b in intraday_bars]
    if not closes:
        return {"error": "No intraday bars available"}

    # ── Liquidity filter ───────────────────────────────────────────────────────
    liquidity = _liquidity_score(snapshot)

    # ── Eight-signal ensemble ──────────────────────────────────────────────────
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
    score_val = score["value"]

    # ── Liquidity penalty: wide spread kills or reduces edge ───────────────────
    liq_label = liquidity["label"]
    if liq_label == "UNTRADEABLE":
        score_val = -100
        score["reasons"] = [f"UNTRADEABLE: {liquidity['spread_pct']:.3f}% spread"] + score["reasons"][:2]
    elif liq_label in ("WIDE_SPREAD", "ELEVATED_SPREAD"):
        score_val = round(score_val * 0.5, 1)
        score["reasons"] = [f"Spread penalty ({liquidity['spread_pct']:.3f}%)"] + score["reasons"]

    # ── Time-of-day modifier: reduce score during chop periods ────────────────
    time_mod = 1.0
    if intraday_bars:
        ts = intraday_bars[-1].get("t", "")
        if ts:
            time_mod = _time_of_day_modifier(ts)
            if time_mod < 1.0:
                score_val = round(score_val * time_mod, 1)
                score["reasons"] = [f"Time penalty ×{time_mod}"] + score["reasons"]

    # Reclassify label after adjustments
    if score_val >= 60:
        score["label"] = "Strong Buy"
    elif score_val >= 20:
        score["label"] = "Buy"
    elif score_val <= -60:
        score["label"] = "Strong Sell"
    elif score_val <= -20:
        score["label"] = "Sell"
    else:
        score["label"] = "Neutral"
    score["value"] = score_val
    score["time_modifier"] = time_mod

    side = "long" if score_val >= 0 else "short"
    trend_label = sigs["trend"].get("label", "neutral")
    levels = _trade_levels(intraday_bars, snapshot, side, trend_label)

    # ── Intraday expected move (for stop sizing reference) ─────────────────────
    intraday_em = _intraday_expected_move(closes)
    if levels and "estimated_slippage_pct" not in levels:
        levels["estimated_slippage_pct"] = liquidity.get("estimated_slippage_pct", 0.0)

    return {
        "signals": sigs,
        "score": score,
        "levels": levels,
        "liquidity": liquidity,
        "intraday_expected_move": intraday_em,
    }
