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

def _sig_vwap(
    bars: list[dict],
    snapshot: dict,
    trend_label: str = "neutral",
    trend_confidence: str = "strong",
) -> dict:
    """
    VWAP deviation signal. Regime-conditioned: in a strong uptrend, extreme
    deviation above VWAP is momentum (not fade); in a strong downtrend, extreme
    deviation below VWAP is momentum too.

    trend_confidence gates the flip (Kimi review): _sig_trend_bias flags the
    regime "weak" when MA20/MA50 are compressed (<2% apart) — a "weak uptrend"
    shouldn't get the same full ±15/±20 conditioning as a strong one with wide
    MA separation. When confidence is "weak", fall back to the default fade
    logic instead of trusting an ambiguous regime label.
    """
    vwaps = _vwap(bars)
    if not vwaps:
        return {"value": 0, "label": "No data", "score": 0}
    vwap_now = vwaps[-1]
    price = snapshot.get("price", bars[-1]["c"] if bars else 0)
    dev_pct = (price - vwap_now) / vwap_now * 100 if vwap_now else 0
    label_lower = trend_label.lower()
    is_strong_up   = trend_confidence == "strong" and "strong uptrend" in label_lower
    is_strong_down = trend_confidence == "strong" and "downtrend" in label_lower
    if dev_pct > 1.5:
        if is_strong_up:
            score, label = 15, "Momentum: above VWAP in uptrend"
        else:
            score, label = -20, "Overextended above VWAP"
    elif dev_pct > 0.3:
        score, label = 15, "Above VWAP"
    elif dev_pct > -0.3:
        score, label = 0, "At VWAP"
    elif dev_pct > -1.5:
        score, label = -15, "Below VWAP"
    else:
        if is_strong_down:
            score, label = -15, "Momentum: below VWAP in downtrend"
        else:
            score, label = 20, "Oversold below VWAP"
    return {
        "vwap":          round(vwap_now, 4),
        "deviation_pct": round(dev_pct, 3),
        "label":         label,
        "score":         score,
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


def _sig_gap(
    snapshot: dict,
    trend_label: str = "neutral",
    trend_confidence: str = "strong",
) -> dict:
    """
    Pre-market / open gap vs previous close, regime-conditioned.
    Large gaps (>2%) are faded in all regimes — they fill ~65% of the time.
    Small gaps (0.5–2%) follow the trend: gap-down in uptrend = buy the dip (+10),
    gap-up in downtrend = dead-cat bounce to fade (-10).

    trend_confidence gates the trend-following branch (Kimi review): when
    MA20/MA50 are compressed (<2% apart, "weak" confidence), the daily trend
    label is unreliable, so small gaps fall back to the regime-neutral
    default instead of trusting an ambiguous label.
    """
    open_price = snapshot.get("open", 0)
    prev_close = snapshot.get("prev_close", 0)
    if not open_price or not prev_close:
        return {"gap_pct": 0, "direction": "none", "fill_prob": 0.5, "score": 0}
    gap_pct    = (open_price - prev_close) / prev_close * 100
    fill_prob  = 0.65 if abs(gap_pct) > 2 else 0.45 if abs(gap_pct) > 0.5 else 0.3
    is_uptrend   = trend_confidence == "strong" and "uptrend" in trend_label.lower()
    is_downtrend = trend_confidence == "strong" and "downtrend" in trend_label.lower()
    if gap_pct > 2:
        score, direction = -10, "gap_up_large"       # fade large gap up always
    elif gap_pct > 0.5:
        # Small gap up: momentum in uptrend/neutral; dead-cat bounce in downtrend
        score = -10 if is_downtrend else 10
        direction = "gap_up_small"
    elif gap_pct < -2:
        score, direction = 10, "gap_down_large"      # fade large gap down always
    elif gap_pct < -0.5:
        # Small gap down: buy-the-dip in uptrend; follow momentum otherwise
        score = 10 if is_uptrend else -10
        direction = "gap_down_small"
    else:
        score, direction = 0, "flat"
    return {
        "gap_pct":   round(gap_pct, 2),
        "direction": direction,
        "fill_prob": fill_prob,
        "score":     score,
    }


def _sig_trend_bias(daily_bars: list[dict]) -> dict:
    """
    Daily MA20 context — are we in a bullish or bearish regime?

    Also computes regime_confidence (Kimi review): "strong" when MA20/MA50 are
    separated by >=2%, "weak" when compressed. VWAP/gap use this to decide
    whether to trust the label enough to flip their conditioning logic — a
    compressed spread means the regime itself could flip on the next session,
    so downstream signals shouldn't apply full conditioning amplitude.
    """
    if len(daily_bars) < 20:
        return {
            "ma20": 0, "above_ma20": None, "score": 0, "label": "Insufficient data",
            "regime_confidence": "unknown",
        }
    closes = [b["c"] for b in daily_bars]
    ma20 = sum(closes[-20:]) / 20
    ma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else ma20
    price = closes[-1]
    above_ma20 = price > ma20
    above_ma50 = price > ma50

    mid = (ma20 + ma50) / 2 if (ma20 + ma50) else 0
    ma_spread_pct = abs(ma20 - ma50) / mid * 100 if mid else 0.0
    regime_confidence = "strong" if ma_spread_pct >= 2.0 else "weak"

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
        "ma_spread_pct": round(ma_spread_pct, 3),
        "regime_confidence": regime_confidence,
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

def _sig_liquidity(snapshot: dict) -> dict:
    """
    Spread-based liquidity filter. Day trading edge is 10–30 bps;
    a 30 bps spread consumes the entire profit margin.
    Hard reject (pass=False) at >0.3% spread. Penalty multiplier at 0.1–0.3%.
    """
    bid   = snapshot.get("bid", 0)
    ask   = snapshot.get("ask", 0)
    price = snapshot.get("price", 0)
    if not bid or not ask or not price or ask <= bid:
        return {
            "score": 0, "label": "UNKNOWN",
            "spread_pct": 0.0, "estimated_slippage_pct": 0.0, "pass": True,
        }
    spread_pct   = (ask - bid) / price * 100
    slippage_pct = spread_pct / 2  # market order hits mid-spread
    if spread_pct > 0.5:
        score, label, tradeable = -100, "UNTRADEABLE", False
    elif spread_pct > 0.3:
        score, label, tradeable = -60, "WIDE_SPREAD", False
    elif spread_pct > 0.1:
        score, label, tradeable = -20, "ELEVATED_SPREAD", True
    else:
        score, label, tradeable = 0, "LIQUID", True
    return {
        "score":                score,
        "label":                label,
        "bid":                  bid,
        "ask":                  ask,
        "spread_pct":           round(spread_pct, 4),
        "estimated_slippage_pct": round(slippage_pct, 4),
        "pass":                 tradeable,
    }


# ── Time-of-day regime ────────────────────────────────────────────────────────

def _time_of_day_modifier(bar_timestamp: str) -> dict:
    """
    Session-quality dict applied to composite score.
    Returns {"modifier": float, "label": str, "note": str}.
    Lunch chop uses 0.4× + hard zero if score < 60.
    modifier=0.0 means outside trading hours — no signal should fire.
    """
    try:
        dt = datetime.fromisoformat(bar_timestamp.replace("Z", "+00:00"))
        if _ET:
            dt_et = dt.astimezone(_ET)
        else:
            from datetime import timezone, timedelta
            dt_et = dt.astimezone(timezone(timedelta(hours=-4)))
        h = dt_et.hour + dt_et.minute / 60
        if h < 9.5 or h >= 16.0:
            return {"modifier": 0.0, "label": "MARKET_CLOSED",
                    "note": "Outside regular trading hours"}
        elif h < 10.0:
            return {"modifier": 0.7, "label": "OPEN_NOISE",
                    "note": "Opening volatility — fade extremes"}
        elif h < 11.5:
            return {"modifier": 1.0, "label": "MORNING_TREND",
                    "note": "Prime trend window"}
        elif h < 14.0:
            return {"modifier": 0.4, "label": "LUNCH_CHOP",
                    "note": "Thin volume — avoid momentum trades"}
        elif h < 15.5:
            return {"modifier": 1.0, "label": "AFTERNOON_TREND",
                    "note": "Afternoon continuation window"}
        else:
            return {"modifier": 0.6, "label": "CLOSE_REVERSAL",
                    "note": "Late positioning — watch reversals"}
    except Exception:
        return {"modifier": 1.0, "label": "UNKNOWN", "note": "Timestamp parse error"}


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

    # 2R reached: trail stop 1.5R behind current price (locks in 0.5R minimum)
    if r_multiple >= 2.0:
        trail_dist = 1.5 * risk
        new_stop = round(
            (current_price - trail_dist) if long else (current_price + trail_dist), 4
        )
        return {"action": "MODIFY_STOP", "reason": "TRAIL_1.5R", "new_stop": new_stop, "r_multiple": r_multiple}

    # 1R reached: lock stop at breakeven + 1 tick
    if r_multiple > 1.0:
        tick = current_price * 0.0001
        new_stop = round((entry + tick) if long else (entry - tick), 4)
        return {"action": "MODIFY_STOP", "reason": "BREAKEVEN_LOCK", "new_stop": new_stop, "r_multiple": r_multiple}

    current_score = current_signals.get("score", {}).get("value", 0)
    if entry_score * current_score < 0 and abs(current_score) > 40:
        return {
            "action": "EXIT", "reason": "SIGNAL_REVERSAL",
            "price": current_price, "entry_score": entry_score,
            "current_score": current_score, "r_multiple": r_multiple,
        }

    return {"action": "HOLD", "reason": "WITHIN_PARAMETERS", "r_multiple": r_multiple, "bars_held": bars_held}


# ── ATR-based trade levels ─────────────────────────────────────────────────────

def _trade_levels(
    bars: list[dict],
    snapshot: dict,
    side: str,
    trend_label: str = "neutral",
    hold_bars: int = 6,
    intraday_em: Optional[dict] = None,
    liquidity: Optional[dict] = None,
    account_value: float = 10000.0,
    risk_pct: float = 0.01,
) -> dict:
    """
    Entry / stop / target levels + position sizing.
    Stop distance: intraday expected move (1-sigma for hold period) when
    available — calibrated to hold_bars, not daily HV. Falls back to 1.5×ATR.
    Position sizing: risk 1% of account per trade; cap at 25%; reduce 10%
    if spread is elevated to offset slippage.
    """
    atr   = _atr(bars, 14)
    price = snapshot.get("price", bars[-1]["c"] if bars else 0)
    if not price:
        return {}

    # Stop distance: intraday EM is calibrated to the hold period;
    # ATR-1.5× is the safe fallback for when bars are too few.
    if intraday_em and intraday_em.get("dollars_1sigma"):
        stop_dist  = intraday_em["dollars_1sigma"]
        stop_basis = "intraday_em"
    elif atr:
        stop_dist  = 1.5 * atr
        stop_basis = "atr"
    else:
        return {}

    # Target multiples scale with market structure
    label_lower = trend_label.lower()
    t1_mult = 2.0 if "strong" in label_lower else 1.5
    t2_mult = 3.5 if "strong" in label_lower else 2.5

    if side == "long":
        entry   = round(price, 2)
        stop    = round(price - stop_dist, 2)
        target1 = round(price + t1_mult * stop_dist, 2)
        target2 = round(price + t2_mult * stop_dist, 2)
    else:
        entry   = round(price, 2)
        stop    = round(price + stop_dist, 2)
        target1 = round(price - t1_mult * stop_dist, 2)
        target2 = round(price - t2_mult * stop_dist, 2)

    risk    = abs(entry - stop)
    reward1 = abs(target1 - entry)
    rr1     = round(reward1 / risk, 2) if risk else 0

    # Position sizing
    risk_amount = account_value * risk_pct
    shares      = risk_amount / risk if risk else 0
    # Cap at 25% of account; then derive consistent shares from capped value
    position_value = min(shares * price, account_value * 0.25)
    shares         = round(position_value / price, 4) if price else 0
    position_value = round(position_value, 2)

    # Liquidity adjustment: reduce size 10% for elevated spread
    liq_label     = (liquidity or {}).get("label", "LIQUID")
    slippage_est  = (liquidity or {}).get("estimated_slippage_pct", 0.0)
    liq_adj       = 0.90 if liq_label == "ELEVATED_SPREAD" else 1.0
    if liq_adj < 1.0:
        shares         = round(shares * liq_adj, 4)
        position_value = round(position_value * liq_adj, 2)

    return {
        "side":                 side,
        "entry":                entry,
        "stop":                 stop,
        "target1":              target1,
        "target2":              target2,
        "atr":                  round(atr, 4) if atr else 0,
        "stop_basis":           stop_basis,
        "risk_per_share":       round(risk, 4),
        "rr_ratio":             rr1,
        "shares":               shares,
        "position_value":       position_value,
        "risk_dollars":         round(risk_amount, 2),
        "slippage_estimate":    slippage_est,
        "liquidity_adjustment": liq_adj,
    }


# ── Ensemble scorer ────────────────────────────────────────────────────────────

# Signal weights — 8 signals, sum to 1.00
WEIGHTS = {
    "vwap":      0.20,
    "or":        0.15,
    "rsi":       0.15,
    "relvol":    0.10,
    "gap":       0.10,
    "trend":     0.15,
    "bollinger": 0.10,
    "volsurge":  0.05,
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
        signals["vwap"]["score"]      * WEIGHTS["vwap"] +
        signals["or"]["score"]        * WEIGHTS["or"] +
        signals["rsi"]["score"]       * WEIGHTS["rsi"] +
        signals["relvol"]["score"]    * WEIGHTS["relvol"] +
        signals["gap"]["score"]       * WEIGHTS["gap"] +
        signals["trend"]["score"]     * WEIGHTS["trend"] +
        signals["bollinger"]["score"] * WEIGHTS["bollinger"] +
        signals["volsurge"]["score"]  * WEIGHTS["volsurge"]
    )
    # Normalize to -100..+100
    max_possible = sum(
        abs(s) * w for s, w in [
            (25, WEIGHTS["vwap"]), (25, WEIGHTS["or"]), (25, WEIGHTS["rsi"]),
            (20, WEIGHTS["relvol"]), (10, WEIGHTS["gap"]), (20, WEIGHTS["trend"]),
            (20, WEIGHTS["bollinger"]), (10, WEIGHTS["volsurge"]),
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
    hold_bars: int = 6,
    symbol: Optional[str] = None,
) -> dict:
    """
    Full intraday signal computation.
    hold_bars: number of 5-min bars to hold (default 6 = 30 min scalp).
    Returns signals + composite score (with liquidity & time-of-day adjustments)
    + trade levels (intraday-EM-based sizing) + intraday expected move
    + exit_template (seed dict for compute_exit_action calls).
    """
    closes = [b["c"] for b in intraday_bars]
    if not closes:
        return {"error": "No intraday bars available"}

    # ── Liquidity filter ───────────────────────────────────────────────────────
    liquidity = _sig_liquidity(snapshot)

    # ── Eight-signal ensemble ──────────────────────────────────────────────────
    # trend_sig computed first so its label + confidence can regime-condition
    # vwap and gap (confidence gates whether the conditioning flip applies —
    # see _sig_trend_bias / _sig_vwap / _sig_gap docstrings)
    trend_sig  = _sig_trend_bias(daily_bars)
    trend_label = trend_sig.get("label", "neutral")
    trend_confidence = trend_sig.get("regime_confidence", "strong")
    sigs = {
        "vwap":      _sig_vwap(intraday_bars, snapshot, trend_label, trend_confidence),
        "or":        _sig_opening_range(intraday_bars),
        "rsi":       _sig_rsi(intraday_bars),
        "relvol":    _sig_relative_volume(intraday_bars, daily_avg_volume),
        "gap":       _sig_gap(snapshot, trend_label, trend_confidence),
        "trend":     trend_sig,
        "bollinger": _sig_bollinger(intraday_bars),
        "volsurge":  _sig_volume_surge(intraday_bars),
    }

    score     = _composite(sigs)
    score_val = score["value"]
    score["composite_raw"] = score_val   # captured before any modifiers

    # ── Liquidity penalty: hard reject or score haircut ────────────────────────
    liq_label = liquidity["label"]
    if not liquidity["pass"]:
        # UNTRADEABLE or WIDE_SPREAD: kill score entirely
        score_val = 0
        score["label"]   = "UNTRADEABLE" if liq_label == "UNTRADEABLE" else "WIDE_SPREAD"
        score["reasons"] = [f"{liq_label}: {liquidity['spread_pct']:.3f}% spread"] + score["reasons"][:2]
    elif liq_label == "ELEVATED_SPREAD":
        score_val = round(score_val * 0.5, 1)
        score["reasons"] = [f"Spread penalty ({liquidity['spread_pct']:.3f}%)"] + score["reasons"]

    # ── Intraday expected move — compute before levels (used for stop sizing) ──
    intraday_em = _intraday_expected_move(closes, hold_bars)

    # ── Time-of-day modifier ───────────────────────────────────────────────────
    time_info = {"modifier": 1.0, "label": "UNKNOWN", "note": ""}
    if intraday_bars:
        ts = intraday_bars[-1].get("t", "")
        if ts:
            time_info = _time_of_day_modifier(ts)

    time_mod = time_info["modifier"]

    if time_mod == 0.0:
        # Market closed — no signals should fire
        score_val = 0
        score["label"]   = "MARKET_CLOSED"
        score["reasons"] = [time_info["note"]]
    elif time_mod < 1.0:
        score_val = round(score_val * time_mod, 1)
        score["reasons"] = [f"Time modifier ×{time_mod} ({time_info['label']})"] + score["reasons"]
        # Lunch hard zero: unless signal is conviction-level, suppress entirely
        if time_info["label"] == "LUNCH_CHOP" and abs(score_val) < 40:
            score_val        = 0
            score["label"]   = "LUNCH_SUPPRESSED"
            score["reasons"] = ["LUNCH_CHOP: score suppressed (< 40 threshold)"] + score["reasons"][1:]

    # ── N-gram pattern blend (only when symbol provided + score is active) ─────
    ngram = {"signal": "NONE", "confidence": 0}
    if symbol and abs(score_val) > 0 and len(closes) >= 4:
        try:
            from models.trading.ngram import ngram_signal, ngram_to_composite_score
            ngram = ngram_signal(symbol, closes)
            ng_score = ngram_to_composite_score(ngram)
            if ngram.get("confidence", 0) > 20:
                existing_sign = 1 if score_val > 0 else -1
                ngram_sign    = 1 if ng_score > 0 else -1 if ng_score < 0 else 0
                wr = ngram.get("historical_win_rate", 0)
                pat = ngram.get("pattern", "?")
                if ngram_sign != 0 and existing_sign == ngram_sign:
                    # Agreement: max +20% boost (confidence/500, so 100 conf → ×1.20)
                    score_val = round(score_val * (1 + ngram["confidence"] / 500), 1)
                    score["reasons"].append(f"N-gram confirms ({pat}, {wr:.0%} WR)")
                elif ngram_sign != 0 and existing_sign != ngram_sign:
                    # Disagreement: reduce 30%
                    score_val = round(score_val * 0.7, 1)
                    score["reasons"].append(f"N-gram conflicts ({pat}, {wr:.0%} WR) — confidence reduced")
        except Exception:
            pass  # Never let ngram failure break signal generation
    sigs["ngram"] = ngram

    # Reclassify label after all adjustments
    if score["label"] not in ("UNTRADEABLE", "WIDE_SPREAD", "MARKET_CLOSED", "LUNCH_SUPPRESSED"):
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

    score["value"]         = score_val
    score["time_modifier"] = time_mod
    score["time_label"]    = time_info["label"]

    side   = "long" if score_val >= 0 else "short"
    levels = _trade_levels(
        intraday_bars, snapshot, side, trend_label,
        hold_bars=hold_bars,
        intraday_em=intraday_em,
        liquidity=liquidity,
    )

    # ── exit_template: seed dict for compute_exit_action calls ────────────────
    exit_template = {
        "entry_price":  levels.get("entry"),
        "stop_price":   levels.get("stop"),
        "entry_score":  score_val,
        "side":         levels.get("side", side),
        "target1":      levels.get("target1"),
        "target2":      levels.get("target2"),
    }

    return {
        "signals":                sigs,   # includes sigs["ngram"] when symbol provided
        "score":                  score,
        "levels":                 levels,
        "liquidity":              liquidity,
        "intraday_expected_move": intraday_em,
        "exit_template":          exit_template,
    }


# ── Trade logging ─────────────────────────────────────────────────────────────

def log_intraday_trade(
    symbol: str,
    entry_time: str,
    exit_time: str,
    side: str,
    entry_price: float,
    exit_price: float,
    planned_hold_bars: int,
    actual_hold_bars: int,
    entry_score: float,
    exit_reason: str,
    slippage_entry: float = 0.0,
    slippage_exit: float = 0.0,
    pnl_dollars: Optional[float] = None,
    pnl_pct: Optional[float] = None,
) -> dict:
    """
    Persist a completed intraday trade to the intraday_trades table.
    Call after position is closed with actual execution prices.
    pnl_dollars/pnl_pct are computed from prices if not supplied.
    Returns {"id": row_id, "pnl_dollars": float} or {"error": str}.
    """
    try:
        from db.database import get_db
    except ImportError:
        return {"error": "db.database not importable"}

    if pnl_dollars is None and entry_price and exit_price:
        raw = (exit_price - entry_price) if side == "long" else (entry_price - exit_price)
        pnl_dollars = round(raw, 4)
    if pnl_pct is None and entry_price and pnl_dollars is not None:
        pnl_pct = round(pnl_dollars / entry_price * 100, 4)

    try:
        with get_db() as conn:
            cur = conn.execute(
                """
                INSERT INTO intraday_trades (
                    symbol, entry_time, exit_time, side,
                    entry_price, exit_price,
                    planned_hold_bars, actual_hold_bars,
                    entry_score, exit_reason,
                    slippage_entry, slippage_exit,
                    pnl_dollars, pnl_pct,
                    logged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    symbol, entry_time, exit_time, side,
                    entry_price, exit_price,
                    planned_hold_bars, actual_hold_bars,
                    entry_score, exit_reason,
                    slippage_entry, slippage_exit,
                    pnl_dollars, pnl_pct,
                ),
            )
            return {"id": cur.lastrowid, "symbol": symbol, "pnl_dollars": pnl_dollars}
    except Exception as e:
        return {"error": str(e)}
