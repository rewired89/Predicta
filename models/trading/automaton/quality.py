"""
Automaton medium-term company-quality model (2026-11-02, Kimi review).

Why this exists: Automaton holds ~6 months, but it was reusing Low Value's
5-day "bounced off a low" signals (RSI, distance from 20-day low, 5-day
relative strength, news). Those say nothing about whether a COMPANY is worth
owning for six months. This scores the business instead, five groups, each
-100 (bad) to +100 (good):

  value     cheap vs earnings / free cash flow (profit yield bands)
  quality   return on equity, operating margin, gross margin
  growth    revenue growth (1y, 3y), earnings growth
  safety    debt vs equity, short-term liquidity (current ratio)
  momentum  6-month and 12-minus-1-month return relative to SPY

Rules that keep it honest:
- A group with no data is EXCLUDED and the other weights are re-scaled, never
  filled with a neutral guess (same "no faking" rule as the Low Value signals).
- It ABSTAINS (no opinion, so no trade) unless at least 3 groups have data and
  at least one of them is a fundamentals group (value/quality/growth/safety).
  Price momentum alone is not "company quality".
- Every raw input used is returned, so each trade can be audited and, later,
  tuned on what was known at the time. Fundamentals come from Finnhub's current
  snapshot, so this CANNOT be back-tested point-in-time; it is a reasoned
  starting design (band edges below are judgment, not fitted), to be judged on
  forward results only.
- Finnhub field names and units (percent vs ratio) could not be verified from
  the build sandbox; several alternative names are tried and unit sanity
  checks applied. Check GET /trade/automaton/quality/{symbol} on the live site.
"""
from __future__ import annotations
from typing import Optional

GROUP_WEIGHTS = {"value": 0.20, "quality": 0.25, "growth": 0.20, "safety": 0.15, "momentum": 0.20}
FUNDAMENTAL_GROUPS = ("value", "quality", "growth", "safety")
MIN_GROUPS = 3

# Entry gate used by fetchers/automaton_runner.py
AUTOMATON_MIN_QUALITY: float = 25.0


def _band(x: Optional[float], bad: float, neutral: float, good: float) -> Optional[float]:
    """Piecewise-linear score: bad -> -100, neutral -> 0, good -> +100 (either direction)."""
    if x is None:
        return None
    if good < bad:                       # lower is better: mirror
        return _band(-x, -bad, -neutral, -good)
    if x <= bad:
        return -100.0
    if x >= good:
        return 100.0
    if x < neutral:
        return -100.0 * (neutral - x) / (neutral - bad)
    return 100.0 * (x - neutral) / (good - neutral)


def _first(m: dict, *names: str) -> Optional[float]:
    for n in names:
        v = m.get(n)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f:                       # not NaN
            return f
    return None


def _mean(vals: list) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _value(m: dict) -> tuple:
    pe = _first(m, "peTTM", "peBasicExclExtraTTM", "peNormalizedAnnual")
    ps = _first(m, "psTTM")
    pfcf = _first(m, "pfcfShareTTM", "pfcfTTM")
    net_margin = _first(m, "netProfitMarginTTM")
    inputs = {"pe": pe, "ps": ps, "p_fcf": pfcf, "net_margin_pct": net_margin}
    parts = []
    if pe is not None and pe > 0:
        parts.append(_band(100.0 / pe, 1.0, 5.0, 12.0))        # earnings yield %
    elif (pe is not None and pe <= 0) or (pe is None and net_margin is not None and net_margin < 0):
        parts.append(-60.0)                                    # loss-making: explicit penalty
    if pfcf is not None and pfcf > 0:
        parts.append(_band(100.0 / pfcf, 0.0, 4.0, 10.0))      # free-cash-flow yield %
    if not parts and ps is not None and ps > 0:
        parts.append(_band(ps, 10.0, 3.0, 0.5))                # last resort: price / sales
    return _mean(parts), inputs


def _quality(m: dict) -> tuple:
    roe = _first(m, "roeTTM", "roeRfy")
    opm = _first(m, "operatingMarginTTM", "operatingMarginAnnual")
    gm = _first(m, "grossMarginTTM", "grossMarginAnnual")
    inputs = {"roe_pct": roe, "operating_margin_pct": opm, "gross_margin_pct": gm}
    return _mean([_band(roe, 0.0, 12.0, 25.0), _band(opm, 0.0, 10.0, 25.0), _band(gm, 10.0, 35.0, 60.0)]), inputs


def _growth(m: dict) -> tuple:
    g1 = _first(m, "revenueGrowthTTMYoy", "revenueGrowthQuarterlyYoy")
    g3 = _first(m, "revenueGrowth3Y")
    eg = _first(m, "epsGrowthTTMYoy", "epsGrowth3Y")
    inputs = {"revenue_growth_1y_pct": g1, "revenue_growth_3y_pct": g3, "eps_growth_pct": eg}
    return _mean([_band(g1, -10.0, 5.0, 25.0), _band(g3, -5.0, 5.0, 20.0), _band(eg, -20.0, 5.0, 30.0)]), inputs


def _safety(m: dict) -> tuple:
    de = _first(m, "totalDebt/totalEquityQuarterly", "totalDebt/totalEquityAnnual")
    if de is not None and de > 20:       # looks like a percent (e.g. 45 = 0.45x), not a ratio
        de = de / 100.0
    cr = _first(m, "currentRatioQuarterly", "currentRatioAnnual")
    inputs = {"debt_to_equity": de, "current_ratio": cr}
    return _mean([_band(de, 3.0, 1.0, 0.2), _band(cr, 0.8, 1.5, 3.0)]), inputs


def _ret(closes: list, back: int, skip: int = 0) -> Optional[float]:
    """Return from `back` bars ago to `skip` bars ago (skip=0 -> to latest bar)."""
    if len(closes) < back + 1:
        return None
    end = closes[-1 - skip]
    start = closes[-1 - back]
    if not start or not end or start <= 0:
        return None
    return end / start - 1.0


def _momentum(daily_bars: list, bench_bars: list) -> tuple:
    c = [b.get("c") for b in daily_bars if b.get("c")]
    s = [b.get("c") for b in bench_bars if b.get("c")]
    r6, s6 = _ret(c, 126), _ret(s, 126)
    r12, s12 = _ret(c, 252, 21), _ret(s, 252, 21)
    rel6 = (r6 - s6) * 100.0 if r6 is not None and s6 is not None else None
    rel12 = (r12 - s12) * 100.0 if r12 is not None and s12 is not None else None
    inputs = {"bars": len(c), "rel_6m_pct": rel6, "rel_12m_skip1m_pct": rel12}
    return _mean([_band(rel6, -25.0, 0.0, 25.0), _band(rel12, -30.0, 0.0, 30.0)]), inputs


def score_medium_term(metrics: Optional[dict], daily_bars: list, bench_bars: list) -> dict:
    """Composite medium-term quality, or an explicit abstain. Pure function, no I/O."""
    metrics = metrics or {}
    raw = {
        "value": _value(metrics), "quality": _quality(metrics), "growth": _growth(metrics),
        "safety": _safety(metrics), "momentum": _momentum(daily_bars or [], bench_bars or []),
    }
    groups = {}
    for name, (score, inputs) in raw.items():
        groups[name] = {"score": None if score is None else round(score, 1),
                        "weight": GROUP_WEIGHTS[name], "inputs": inputs}
    avail = {n: g for n, g in groups.items() if g["score"] is not None}
    have_fund = any(n in avail for n in FUNDAMENTAL_GROUPS)
    out = {"groups": groups, "coverage": len(avail), "composite": None, "abstain": False, "abstain_reason": None}
    if len(avail) < MIN_GROUPS or not have_fund:
        out["abstain"] = True
        out["abstain_reason"] = (
            f"only {len(avail)} of 5 groups have data"
            + ("" if have_fund else " and none are company fundamentals")
            + f" (need {MIN_GROUPS}+ including at least one fundamentals group)"
        )
        return out
    wsum = sum(g["weight"] for g in avail.values())
    out["composite"] = round(sum(g["score"] * g["weight"] for g in avail.values()) / wsum, 1)
    out["plain_summary"] = _plain(out["composite"], avail)
    return out


def _plain(composite: float, avail: dict) -> str:
    best = max(avail.items(), key=lambda kv: kv[1]["score"])
    worst = min(avail.items(), key=lambda kv: kv[1]["score"])
    label = ("strong" if composite >= 40 else "decent" if composite >= 15
             else "mixed" if composite > -15 else "weak")
    return (f"Medium-term company quality looks {label} ({composite:+.0f}/100). "
            f"Best area: {best[0]} ({best[1]['score']:+.0f}). Weakest: {worst[0]} ({worst[1]['score']:+.0f}).")


def passes_gate(q: dict, side: str, min_quality: float = AUTOMATON_MIN_QUALITY) -> tuple:
    """(ok, reason). Longs need a quality score >= +min; shorts need <= -min; abstain never passes."""
    if q.get("abstain"):
        return False, f"abstain: {q.get('abstain_reason')}"
    c = q["composite"]
    if side == "long" and c < min_quality:
        return False, f"quality {c:+.0f} is below the +{min_quality:.0f} needed for a 6-month long"
    if side == "short" and c > -min_quality:
        return False, f"quality {c:+.0f} is not weak enough (needs <= -{min_quality:.0f}) for a 6-month short"
    return True, "ok"
