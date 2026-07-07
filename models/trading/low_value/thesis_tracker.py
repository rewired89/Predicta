"""
Low Value engine — signal engine (Kimi review round 6 follow-up).

Computes 8 signals for a sub-$20 contrarian candidate from DAILY bars only
(no 5-minute data — these names don't have the intraday liquidity High
Value's engine relies on). No regime-conditioning from High Value: these
stocks don't have reliable trends to condition on, per spec.

This is a "buy the fear" contrarian thesis: a low price_vs_20d_low ratio,
oversold RSI, and a capitulation volume spike are treated as BULLISH
(beaten-down + about to turn), not bearish — the opposite polarity from a
trend-following engine. Insider buying, sector-relative strength, and
positive news sentiment reinforce the same thesis; short interest is scored
as squeeze potential (positive), not risk.

Composite score: -100..+100. Missing signals (no data available, e.g. no
Finnhub fundamentals for a given micro-cap) are EXCLUDED from the weighted
average and their weight is proportionally redistributed across whatever
signals ARE available — never fabricated as a default value ("no faking",
same discipline as every calibration gate elsewhere in this codebase).
"""
from __future__ import annotations
from typing import Optional

from fetchers.openinsider import has_insider_buying
from fetchers.finra import get_short_interest_pct
from fetchers.finnhub import get_company_profile, get_basic_financials

SIGNAL_WEIGHTS: dict[str, float] = {
    "price_vs_20d_low":        0.20,
    "rsi_14":                  0.15,
    "volume_spike":            0.10,
    "insider_buying_30d":      0.20,
    "short_interest_pct":      0.10,
    "sector_relative_strength": 0.10,
    "cash_burn_months":        0.10,
    "news_sentiment":          0.05,
}
assert abs(sum(SIGNAL_WEIGHTS.values()) - 1.0) < 1e-9

ENTRY_THRESHOLD: float = 40.0

SECTOR_ETF_BY_INDUSTRY: dict[str, str] = {
    "technology": "XLK", "software": "XLK", "semiconductors": "XLK",
    "financial services": "XLF", "banking": "XLF", "insurance": "XLF",
    "energy": "XLE", "oil & gas": "XLE",
    "biotechnology": "XLV", "pharmaceuticals": "XLV", "health care": "XLV", "healthcare": "XLV",
    "industrial": "XLI", "aerospace & defense": "XLI",
    "consumer discretionary": "XLY", "retail": "XLY", "auto": "XLY",
    "consumer staples": "XLP",
    "utilities": "XLU",
    "materials": "XLB", "chemicals": "XLB", "metals & mining": "XLB",
    "real estate": "XLRE",
    "communication services": "XLC", "media": "XLC", "telecom": "XLC",
}
DEFAULT_SECTOR_ETF = "SPY"  # unknown/missing industry -> compare against the broad market


def sector_etf_for_symbol(symbol: str) -> str:
    """Finnhub finnhubIndustry -> SPDR sector ETF, falling back to SPY (broad market) when unmapped."""
    profile = get_company_profile(symbol)
    industry = (profile.get("finnhubIndustry") or "").strip().lower()
    for key, etf in SECTOR_ETF_BY_INDUSTRY.items():
        if key in industry:
            return etf
    return DEFAULT_SECTOR_ETF


def _closes(bars: list[dict]) -> list[float]:
    return [b.get("c", 0.0) for b in bars if b.get("c") is not None]


def _rsi_14(closes: list[float]) -> Optional[float]:
    """Standard 14-period RSI from daily closes. None if fewer than 15 closes."""
    if len(closes) < 15:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    window_gains  = gains[-14:]
    window_losses = losses[-14:]
    avg_gain = sum(window_gains) / 14
    avg_loss = sum(window_losses) / 14
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _pct_return(closes: list[float], n: int) -> Optional[float]:
    """N-bar return as a percent. None if not enough history."""
    if len(closes) < n + 1:
        return None
    start, end = closes[-(n + 1)], closes[-1]
    if not start:
        return None
    return round((end - start) / start * 100, 3)


def _score_price_vs_20d_low(daily_bars: list[dict]) -> Optional[tuple[float, dict]]:
    closes = _closes(daily_bars)
    if len(closes) < 20:
        return None
    low_20d = min(closes[-20:])
    close   = closes[-1]
    if low_20d <= 0:
        return None
    ratio = (close - low_20d) / low_20d
    # 0 (at the low) -> 100; 0.30+ (30% above the low) -> 0. Never negative —
    # being far from the low just means less contrarian opportunity, not bearish.
    raw = max(0.0, 1.0 - (ratio / 0.30))
    return round(raw * 100, 2), {"ratio": round(ratio, 4), "20d_low": low_20d, "close": close}


def _score_rsi_14(daily_bars: list[dict]) -> Optional[tuple[float, dict]]:
    rsi = _rsi_14(_closes(daily_bars))
    if rsi is None:
        return None
    # RSI 30 (oversold) -> +40, RSI 10 -> +80, RSI 50 -> 0, RSI 70 (overbought) -> -40
    score = max(-100.0, min(100.0, (50.0 - rsi) * 2.0))
    return round(score, 2), {"rsi_14": rsi}


def _score_volume_spike(daily_bars: list[dict]) -> Optional[tuple[float, dict]]:
    if len(daily_bars) < 21:
        return None
    vols = [b.get("v", 0) for b in daily_bars]
    today_vol = vols[-1]
    avg_20d   = sum(vols[-21:-1]) / 20
    if avg_20d <= 0:
        return None
    spike = today_vol / avg_20d
    # Magnitude-only signal (capitulation intensity) — never negative on its own;
    # direction comes from price_vs_20d_low / rsi_14 in the same composite.
    score = max(0.0, min(100.0, (spike - 1.0) * 40.0))
    return round(score, 2), {"spike_ratio": round(spike, 3)}


def _score_insider_buying(symbol: str) -> tuple[float, dict]:
    bought = has_insider_buying(symbol, days=30)
    return (100.0 if bought else 0.0), {"insider_buying_30d": bought}


def _score_short_interest(symbol: str) -> Optional[tuple[float, dict]]:
    pct = get_short_interest_pct(symbol)
    if pct is None:
        return None
    # Squeeze-potential framing (contrarian, not risk-flag): 10% short interest
    # -> +40, 25%+ -> capped at +100.
    score = max(0.0, min(100.0, pct * 4.0))
    return round(score, 2), {"short_interest_pct": pct}


def _score_sector_relative_strength(symbol_bars: list[dict], sector_bars: list[dict]) -> Optional[tuple[float, dict]]:
    sym_ret    = _pct_return(_closes(symbol_bars), 5)
    sector_ret = _pct_return(_closes(sector_bars), 5)
    if sym_ret is None or sector_ret is None:
        return None
    relative = sym_ret - sector_ret
    score = max(-100.0, min(100.0, relative * 10.0))
    return round(score, 2), {"symbol_5d_return_pct": sym_ret, "sector_5d_return_pct": sector_ret, "relative_pp": round(relative, 3)}


def _score_cash_burn(symbol: str) -> Optional[tuple[float, dict]]:
    """
    cash_burn_months = cash_and_equivalents / avg_monthly_burn, from Finnhub's
    basic-financials 'metric' series. None when Finnhub doesn't cover the
    symbol's fundamentals (common for micro-caps) — excluded from the
    weighted average, never estimated.
    """
    metrics = get_basic_financials(symbol)
    if not metrics:
        return None
    cash = metrics.get("cashAndEquivalents") or metrics.get("cashPerSharePerYearly")
    fcf_yearly = metrics.get("freeCashFlowPerShareTTM")
    # Finnhub's free tier doesn't expose a direct "avg monthly burn" metric;
    # only compute this signal when both cash and a burn-rate proxy exist —
    # otherwise return None rather than guessing.
    burn_monthly = metrics.get("monthlyBurnRate")
    if cash is None or not burn_monthly or burn_monthly <= 0:
        return None
    months = cash / burn_monthly
    # 6 months runway -> 0, 12 -> +60, 24+ -> capped +100, <3 -> negative (bankruptcy risk)
    score = max(-100.0, min(100.0, (months - 6.0) * 10.0))
    return round(score, 2), {"cash_burn_months": round(months, 1)}


def _score_news_sentiment(news_result: Optional[dict]) -> Optional[tuple[float, dict]]:
    if not news_result or not news_result.get("headline_count"):
        return None
    sentiment = news_result.get("sentiment", 0.0)
    return round(sentiment * 100, 2), {"sentiment": sentiment, "headline_count": news_result.get("headline_count"), "flags": news_result.get("flags", [])}


def label_for_score(score: float) -> str:
    if score >= 70:
        return "STRONG_BUY"
    if score >= ENTRY_THRESHOLD:
        return "BUY"
    if score <= -70:
        return "STRONG_SELL"
    if score <= -ENTRY_THRESHOLD:
        return "SELL"
    return "NEUTRAL"


def dominant_thesis_type(thesis_result: dict, news_result: Optional[dict]) -> str:
    """
    Single thesis-type tag for per-thesis-type win-rate calibration (spec:
    "20 trades per thesis type"). Preference order: a news category flag
    (the most specific, event-driven thesis) > insider buying > a generic
    technical-oversold bucket when only price/RSI/volume signals fired.
    """
    flags = (news_result or {}).get("flags") or []
    if flags:
        return flags[0]
    insider = thesis_result.get("signals", {}).get("insider_buying_30d", {}).get("detail", {})
    if insider.get("insider_buying_30d"):
        return "INSIDER_BUYING"
    return "TECHNICAL_OVERSOLD"


def compute_thesis_score(
    symbol: str,
    daily_bars: list[dict],
    sector_bars: list[dict],
    news_result: Optional[dict] = None,
) -> dict:
    """
    Full 8-signal composite for one Low Value candidate.

    daily_bars / sector_bars: Alpaca get_daily_bars() output for the symbol
    and its sector ETF (see sector_etf_for_symbol). news_result: news_overlay.
    score_symbol_news() output, or None if not yet scanned.

    Returns {composite: float, label: str, entry_eligible: bool,
             signals: {name: {score, detail}}, missing_signals: list[str]}.
    """
    raw_results: dict[str, Optional[tuple[float, dict]]] = {
        "price_vs_20d_low":         _score_price_vs_20d_low(daily_bars),
        "rsi_14":                   _score_rsi_14(daily_bars),
        "volume_spike":             _score_volume_spike(daily_bars),
        "insider_buying_30d":       _score_insider_buying(symbol),
        "short_interest_pct":       _score_short_interest(symbol),
        "sector_relative_strength": _score_sector_relative_strength(daily_bars, sector_bars),
        "cash_burn_months":         _score_cash_burn(symbol),
        "news_sentiment":           _score_news_sentiment(news_result),
    }

    available = {name: res for name, res in raw_results.items() if res is not None}
    missing   = [name for name, res in raw_results.items() if res is None]

    if not available:
        return {
            "composite": None, "label": "NEUTRAL", "entry_eligible": False,
            "signals": {}, "missing_signals": missing,
            "note": "No signals computable — insufficient data, no faking.",
        }

    weight_sum = sum(SIGNAL_WEIGHTS[name] for name in available)
    composite = sum(SIGNAL_WEIGHTS[name] * score for name, (score, _detail) in available.items()) / weight_sum
    composite = round(composite, 2)

    return {
        "composite": composite,
        "label": label_for_score(composite),
        "entry_eligible": abs(composite) >= ENTRY_THRESHOLD,
        "signals": {name: {"score": score, "detail": detail} for name, (score, detail) in available.items()},
        "missing_signals": missing,
    }
