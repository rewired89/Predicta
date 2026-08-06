"""
Automated paper trading runner for hypothetical signal collection.

Runs as a background thread during market hours (Mon-Fri 9:30-16:00 ET):
  - 9:35 AM ET: morning scan → logs all abs(score) >= RUNNER_MIN_SCORE as
    hypothetical trades via log_hypothetical_trade()
  - Every 30 min:  position check → closes positions that hit stop, target,
    or time limit via log_trade_exit(), using actual 5-min bar highs/lows
    so stop/target touches within the period are not missed
  - 15:50 ET:     force-close all remaining positions at current market price

Data feeds into GET /trade/calibration at 30+ closed trades.
Kimi phase-1 protocol: collect 40-60 hypothetical trades before live sizing.
"""
from __future__ import annotations
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo as _ZI
    _EASTERN = _ZI("America/New_York")
except ImportError:
    _EASTERN = None  # fallback: use UTC offset approximation

from fetchers.alpaca import get_snapshots, get_bars, get_daily_bars, place_bracket_order, get_order, close_position, readable_order_error
from fetchers.trading_logger import log_hypothetical_trade, log_trade_entry, log_trade_exit, promote_trade_to_real
from fetchers import hsip_client
from models.trading.high_value.intraday import compute_intraday_signals
from db.database import get_db
import json

log = logging.getLogger("paper_runner")

# ── Configuration ─────────────────────────────────────────────────────────────

# Single-tier large-cap universe per Kimi's advice: avoids mixing volatility
# regimes in the first 30-40 trades so calibration weights are meaningful.
# NOT deleted, NOT modified by the low-price mode below (2026-07-18) — this
# stays the exact watchlist used when ACTIVE_UNIVERSE_MODE == "large_cap".
RUNNER_SYMBOLS: list[str] = [
    "AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META", "GOOGL", "TSLA",
]

# Added 2026-07-18, direct user request: they don't want to day-trade
# $100+ names like AAPL/AMZN — they want a lower-priced universe instead,
# without losing the existing large-cap watchlist (still fully intact
# above, just not the active one by default now).
#
# ACTIVE_UNIVERSE_MODE picks which watchlist get_active_watchlist() returns:
#   "large_cap" -> RUNNER_SYMBOLS, unchanged, exactly as before this change.
#   "low_price" -> LOW_PRICE_WATCHLIST_CANDIDATES, filtered live at scan time
#                  to price < LOW_PRICE_CEILING.
# Switch back to "large_cap" any time to fully restore prior behavior — no
# code is deleted either way.
ACTIVE_UNIVERSE_MODE: str = "low_price"
LOW_PRICE_CEILING: float = 70.0

# Candidate pool for low-price mode — liquid, well-known, heavily-traded
# names chosen because day trading specifically needs tight spreads and
# fast fills (thin/illiquid names are a much worse fit for same-day holds
# than for Low Value's multi-day thesis). This list is NOT the enforcement
# mechanism, though — stock prices move, and this session has no live
# market-data access to verify which of these are actually under $70 right
# now. get_active_watchlist() re-checks every candidate's REAL current
# price via Alpaca at scan time and drops anything at or above
# LOW_PRICE_CEILING — so the $70 ceiling is enforced live, not by trusting
# this list to stay accurate as prices drift. A candidate that's since
# risen above $70 is simply excluded that day, not force-included.
LOW_PRICE_WATCHLIST_CANDIDATES: list[str] = [
    "F", "INTC", "T", "PFE", "CSCO", "BAC", "SOFI", "PLTR",
    "NIO", "SNAP", "UBER", "RIVN", "WBD", "KO", "NOK",
]

# Cast wide net for data; the trade endpoint uses 40 as the action threshold
RUNNER_MIN_SCORE: int = 20

# Data-collection sprint mode (Kimi review, round 4): temporarily lowers the
# logging threshold to accelerate the path to 100 closed trades (Kimi's math:
# ~6-12 months at the normal 20-point floor vs 4-6 weeks at 10). Explicit and
# reversible — flip back to False to return to the normal floor. Nothing is
# lost by running it: entry_score is stored per trade regardless, so post-hoc
# analysis can always re-filter to |score|>=20 even with sprint mode on.
DATA_COLLECTION_SPRINT_MODE: bool = True
SPRINT_MIN_SCORE: int = 10

# Portfolio risk control (Kimi review, structural gap A): the 8-symbol
# universe is a single correlated tech cluster, not diversified sectors — cap
# total simultaneous open positions instead of computing pairwise correlation,
# since a sector-wide move could otherwise stack N x 25%-sized positions at once.
MAX_CONCURRENT_POSITIONS: int = 3

# Market regime gate (Kimi review, structural gap E): SPY overnight gap is used
# as a volatility-regime proxy (no VIX access on the Alpaca free tier). On
# extreme days, raise the logging threshold so the paper-trading dataset isn't
# contaminated with signals fired during untradeable volatility.
REGIME_GAP_THRESHOLD_PCT: float = 2.0
REGIME_EXTREME_MIN_SCORE: int = 60

# Earnings blackout (Kimi review, structural gap D). Only dates confirmed by
# the company itself are listed — guessing dates would be worse than no filter
# at all. Most of the watchlist has not announced Q3 2026 dates yet; add them
# here as they're confirmed.
EARNINGS_BLACKOUT: dict[str, list[str]] = {
    "AAPL": ["2026-07-30"],   # confirmed by Apple — fiscal Q3 2026 release
}

# Macro event calendar (Kimi review, round 4 — logged only, never used to
# filter or size trades). FOMC decision days only, sourced directly from
# federalreserve.gov's published 2026 meeting calendar — no live FRED API call,
# avoiding an external dependency for a single boolean tag. Second day of each
# meeting (the announcement/press-conference day) is the market-moving date.
MACRO_EVENT_DATES: set[str] = {
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
}

# Market volatility regime tag (Kimi review, round 3, structural gap B — logged
# only, never used to filter trades yet). Buckets SPY's 20-day annualized
# realized vol into LOW/NORMAL/HIGH so post-hoc analysis can ask "is the model
# profitable in LOW vol but not HIGH vol" — impossible to answer retroactively
# without the tag.
VOL_REGIME_LOW_PCT: float = 12.0
VOL_REGIME_HIGH_PCT: float = 25.0

# Intraday regime escalation (Kimi review, round 3): the 9:35 AM scan only sees
# SPY's overnight gap. A stock that opens flat and then sells off intraday
# would otherwise never get flagged EXTREME. If SPY's cumulative change from
# prior close exceeds this at any 30-min check, today is reclassified EXTREME
# for the rest of the day (affects any later manual re-scan).
INTRADAY_REGIME_ESCALATION_PCT: float = 3.0

# Macro regime tags (Kimi review, round 6 — Bridgewater "Four Boxes" + Dalio
# 3-force overlay). All values come from FRED (requires FRED_API_KEY env var;
# fails safe/empty when absent, same convention as OPENWEATHER_API_KEY).
# Read-only logging for growth/inflation/yield-curve/Fed-stance context.
# The 3-force overlay is the ONE piece that's additive to the score gate — it
# elevates the logging threshold the same way the SPY-gap regime check does,
# it never blocks or replaces that existing check.
FRED_SERIES_10Y = "GS10"
FRED_SERIES_2Y  = "GS2"
FRED_SERIES_FEDFUNDS = "FEDFUNDS"
FRED_SERIES_HY_OAS   = "BAMLH0A0HYM2"   # ICE BofA US High Yield OAS (short-term debt cycle proxy)
FRED_SERIES_DEBT_GDP = "GFDEGDQ188S"    # Federal debt held by public, % of GDP (long-term debt cycle proxy)

# Human review queue for EXTREME days (Kimi review, round 6 — D.E. Shaw hybrid
# model: models fail at regime changes, so unprecedented days get a human
# safety valve instead of auto-logging or auto-skipping). Optional, not
# mandatory — auto-skips after the timeout so it never blocks collection.
REVIEW_QUEUE_TIMEOUT_SEC: int = 300

# Hold duration per time-of-day label, in 5-min bars
_HOLD_BARS: dict[str, int] = {
    "MORNING_TREND":   12,   # 60 min — ride the morning move
    "AFTERNOON_TREND": 12,   # 60 min
    "CLOSE_REVERSAL":   6,   # 30 min — short hold near close
    "OPEN_NOISE":       6,   # 30 min, suppressed anyway
    "LUNCH_CHOP":       6,   # 30 min, suppressed anyway
}
_DEFAULT_HOLD_BARS: int = 6

def get_active_watchlist() -> list[str]:
    """
    The watchlist run_open_scan/start_runner actually use, per
    ACTIVE_UNIVERSE_MODE. "large_cap" returns RUNNER_SYMBOLS unchanged.
    "low_price" fetches a live snapshot for every LOW_PRICE_WATCHLIST_
    CANDIDATES symbol and keeps only those actually trading below
    LOW_PRICE_CEILING right now — see the constants above for why this
    can't just trust the candidate list to stay accurate as prices move.

    Fails toward an EMPTY list on a snapshot-fetch error in low_price mode,
    not toward RUNNER_SYMBOLS — silently falling back to the $100+ watchlist
    the user explicitly asked to move away from would violate their stated
    preference more than skipping a scan for a day would.
    """
    if ACTIVE_UNIVERSE_MODE != "low_price":
        return RUNNER_SYMBOLS

    from fetchers.alpaca import get_snapshots
    snaps = get_snapshots(LOW_PRICE_WATCHLIST_CANDIDATES)
    if not snaps:
        log.warning("[RUNNER] low_price mode: snapshot fetch failed for all candidates — skipping scan rather than falling back to RUNNER_SYMBOLS")
        return []
    filtered = [
        sym for sym in LOW_PRICE_WATCHLIST_CANDIDATES
        if (snaps.get(sym) or {}).get("price", 0) > 0
        and snaps[sym]["price"] < LOW_PRICE_CEILING
    ]
    if not filtered:
        log.warning(f"[RUNNER] low_price mode: 0 of {len(LOW_PRICE_WATCHLIST_CANDIDATES)} candidates are currently under ${LOW_PRICE_CEILING:.0f}")
    return filtered


# ── Internal state ────────────────────────────────────────────────────────────

_runner_thread: Optional[threading.Thread] = None
_runner_active: bool = False
_run_log: list[dict] = []           # ring buffer — last 100 events

# Intraday regime escalation state (Kimi review, round 3) — reset on new day
_intraday_regime_override: Optional[str] = None
_override_date: Optional[str] = None

# Suppression-rate instrumentation (Kimi review, round 4, structural gap 7):
# tallies every scan where regime_confidence=="weak" AND price sits inside the
# opening range — Kimi's hypothesis is the composite may collapse toward zero
# here not from lack of edge but because two conditioning signals go neutral
# simultaneously. Pure counting, no scoring change; if the rate exceeds 60%,
# that's the trigger to let Opening Range fire independently on wide ranges.
_suppression_stats: dict[str, int] = {"weak_regime_inside_or_scans": 0, "total_scans": 0}


# ── ET time helpers ───────────────────────────────────────────────────────────

def _et_now() -> datetime:
    """Current datetime in US/Eastern (handles DST via zoneinfo when available)."""
    utc = datetime.now(timezone.utc)
    if _EASTERN:
        return utc.astimezone(_EASTERN)
    # Fallback: approximate EDT (UTC-4) Apr–Oct, EST (UTC-5) otherwise
    month = utc.month
    offset = timedelta(hours=-4 if 3 < month < 11 else -5)
    return (utc + offset).replace(tzinfo=timezone(offset))


def _et_minutes() -> int:
    """Current ET time expressed as minutes since midnight."""
    now = _et_now()
    return now.hour * 60 + now.minute


def _is_market_open() -> bool:
    """True during NYSE regular session: Mon-Fri 09:30-16:00 ET."""
    now = _et_now()
    if now.weekday() >= 5:
        return False
    t = _et_minutes()
    return 570 <= t < 960    # 9:30=570, 16:00=960


def _in_scan_window() -> bool:
    """True between 09:35 and 09:59 ET — morning scan window."""
    now = _et_now()
    if now.weekday() >= 5:
        return False
    t = _et_minutes()
    return 575 <= t < 600


def _near_close() -> bool:
    """True at or after 15:50 ET — force-close window."""
    return _et_minutes() >= 950


# ── Scan helper ───────────────────────────────────────────────────────────────

def _avg_daily_vol(daily_bars: list[dict]) -> float:
    vols = [b.get("v", 0) for b in daily_bars[-20:]]
    return sum(vols) / len(vols) if vols else 1_000_000


def _spy_realized_vol_pct() -> float:
    """
    20-day annualized realized volatility of SPY daily closes (log-return
    based, stdlib math only). Feeds the LOW/NORMAL/HIGH market_vol_regime tag.
    Returns 0.0 on any error or insufficient history (fail-safe → NORMAL bucket).
    """
    try:
        import math
        daily  = get_daily_bars("SPY", days=30)
        closes = [b["c"] for b in daily if b.get("c")]
        if len(closes) < 21:
            return 0.0
        window = closes[-21:]
        rets = [
            math.log(window[i] / window[i - 1])
            for i in range(1, len(window)) if window[i - 1] > 0
        ]
        if not rets:
            return 0.0
        mean_r = sum(rets) / len(rets)
        var_r  = sum((r - mean_r) ** 2 for r in rets) / len(rets)
        return round(math.sqrt(var_r) * math.sqrt(252) * 100, 2)
    except Exception:
        return 0.0


def _vol_regime_bucket(vol_pct: float) -> str:
    """Buckets annualized realized vol % into LOW/NORMAL/HIGH."""
    if vol_pct <= 0:
        return "NORMAL"
    if vol_pct < VOL_REGIME_LOW_PCT:
        return "LOW"
    if vol_pct > VOL_REGIME_HIGH_PCT:
        return "HIGH"
    return "NORMAL"


def _reset_regime_override_if_new_day() -> None:
    """Clears _intraday_regime_override at the start of each new trading day."""
    global _intraday_regime_override, _override_date
    today = _et_now().strftime("%Y-%m-%d")
    if _override_date != today:
        _intraday_regime_override = None
        _override_date = today


def _check_intraday_regime_escalation() -> None:
    """
    Re-checks SPY's cumulative change from prior close at each 30-min position
    check (Kimi review, round 3). The 9:35 AM scan only sees the overnight gap
    — a stock that opens flat and sells off 3%+ intraday would otherwise never
    get flagged EXTREME. Sets _intraday_regime_override for the rest of today
    so a later manual re-scan (paper_runner_scan_now) honors it too.
    """
    global _intraday_regime_override
    _reset_regime_override_if_new_day()
    try:
        snap = get_snapshots(["SPY"]).get("SPY", {})
        change_pct = snap.get("change_pct", 0.0)
        if abs(change_pct) >= INTRADAY_REGIME_ESCALATION_PCT and _intraday_regime_override != "EXTREME":
            _intraday_regime_override = "EXTREME"
            log.info(
                f"[RUNNER] Intraday regime escalation — SPY cumulative change "
                f"{change_pct:+.2f}%, marking today EXTREME"
            )
            _run_log.append({
                "ts": _et_now().isoformat(), "event": "REGIME_ESCALATION",
                "note": f"SPY cumulative change {change_pct:+.2f}% — today marked "
                        f"EXTREME for remaining scans",
            })
    except Exception:
        pass


def _fetch_market_regime() -> dict:
    """
    Coarse "should we even be trading today" gate (Kimi review, structural
    gap E). Uses SPY's overnight gap as a volatility-regime proxy — no VIX
    access on the Alpaca free tier — and XLK's daily change as a simple
    sector-rotation tag for the (all-tech) watchlist. Also tags SPY's 20-day
    realized vol bucket (LOW/NORMAL/HIGH) for post-hoc analysis (round 3),
    and honors any same-day intraday escalation set by
    _check_intraday_regime_escalation().

    Also tags macro_event_today (FOMC decision days, round 4 — logged only)
    and the Bridgewater/Dalio macro tags from _fetch_macro_tags (round 6).
    long_term_force_contraction additively elevates the regime to EXTREME
    (same effect as the SPY-gap check, never a replacement for it).

    Returns {"regime": "NORMAL"|"EXTREME", "spy_gap_pct": float,
             "xlk_change_pct": float, "spy_realized_vol_pct": float,
             "market_vol_regime": "LOW"|"NORMAL"|"HIGH", "macro_event_today": bool,
             "yield_curve_slope": float|None, "fed_rate": float|None,
             "credit_spread_oas": float|None, "debt_to_gdp_pct": float|None,
             "long_term_force_contraction": bool}.
    Fails safe to NORMAL on any API error so a data hiccup never blocks the scan.
    """
    _reset_regime_override_if_new_day()
    today_str = _et_now().strftime("%Y-%m-%d")
    macro_today = _is_macro_event_day(today_str)
    macro_tags  = _fetch_macro_tags()
    try:
        snaps = get_snapshots(["SPY", "XLK"])
        spy = snaps.get("SPY", {})
        xlk = snaps.get("XLK", {})
        spy_open = spy.get("open", 0)
        spy_prev = spy.get("prev_close", 0)
        spy_gap_pct = round((spy_open - spy_prev) / spy_prev * 100, 3) if spy_prev else 0.0
        xlk_change_pct = xlk.get("change_pct", 0.0)
        regime = "EXTREME" if abs(spy_gap_pct) >= REGIME_GAP_THRESHOLD_PCT else "NORMAL"
        if _intraday_regime_override == "EXTREME":
            regime = "EXTREME"
        if macro_tags.get("long_term_force_contraction"):
            regime = "EXTREME"
        spy_vol_pct = _spy_realized_vol_pct()
        vol_regime  = _vol_regime_bucket(spy_vol_pct)
        return {
            "regime": regime, "spy_gap_pct": spy_gap_pct, "xlk_change_pct": xlk_change_pct,
            "spy_realized_vol_pct": spy_vol_pct, "market_vol_regime": vol_regime,
            "macro_event_today": macro_today,
            **macro_tags,
        }
    except Exception:
        forced_extreme = _intraday_regime_override == "EXTREME" or macro_tags.get("long_term_force_contraction")
        return {
            "regime": "EXTREME" if forced_extreme else "NORMAL",
            "spy_gap_pct": 0.0, "xlk_change_pct": 0.0,
            "spy_realized_vol_pct": 0.0, "market_vol_regime": "NORMAL",
            "macro_event_today": macro_today,
            **macro_tags,
        }


def _fetch_macro_tags() -> dict:
    """
    Bridgewater "Four Boxes" + Dalio 3-force overlay (Kimi review, round 6).
    All values are FRED's most recently published figures (lagging, not
    real-time) — logged for post-hoc regime analysis. Fails safe to all-None
    when FRED_API_KEY is absent or any request fails; never blocks the scan.

    long_term_force_contraction: True when credit spread OR debt/GDP sits
    above its historical mean by the Dalio-specified numbers of std devs
    (HY-OAS: 2 std devs over a ~1yr daily window; debt/GDP: 1 std dev over a
    ~5yr quarterly window). Read-only — see _fetch_market_regime for how this
    elevates (not replaces) the existing SPY-gap threshold.

    long_term_force_expansion (added 2026-07-17, Tier 2 of the trading-model
    audit): shadow-logged only, never read by _fetch_market_regime or
    anything that changes live behavior. This overlay only ever penalizes —
    a genuinely calm/favorable macro backdrop was never recorded anywhere,
    so there was no way to later check whether a symmetric "loosen up on a
    good regime" adjustment would have helped or hurt. Deliberately NOT a
    full mirror of long_term_force_contraction: credit spread has a
    legitimate, well-established symmetric reading (anomalously TIGHT HY-OAS
    is conventionally read as calm/risk-on, the mirror of anomalously wide
    being stress), so that half is mirrored (2 std devs below mean). Debt/GDP
    is deliberately NOT mirrored — Dalio's own long-term debt cycle framing
    is asymmetric (a slow multi-decade rise, a sharp deleveraging fall), so
    "debt/GDP anomalously low" is not a real force the same way "anomalously
    high" is a real stress signal; inventing a symmetric debt/GDP case would
    be exactly the fabricate-a-number-because-it-looks-balanced mistake this
    whole audit has been trying to get away from. Log only what's real.
    """
    from fetchers.fred import get_latest_value, get_series_stats
    try:
        gs10 = get_latest_value(FRED_SERIES_10Y)
        gs2  = get_latest_value(FRED_SERIES_2Y)
        yield_curve_slope = round(gs10 - gs2, 3) if (gs10 is not None and gs2 is not None) else None
        fed_rate = get_latest_value(FRED_SERIES_FEDFUNDS)

        hy_stats   = get_series_stats(FRED_SERIES_HY_OAS, n_obs=252)
        debt_stats = get_series_stats(FRED_SERIES_DEBT_GDP, n_obs=20)

        hy_contraction = (
            hy_stats["mean"] is not None and hy_stats["std"] is not None
            and hy_stats["latest"] is not None
            and hy_stats["latest"] > hy_stats["mean"] + 2 * hy_stats["std"]
        )
        debt_contraction = (
            debt_stats["mean"] is not None and debt_stats["std"] is not None
            and debt_stats["latest"] is not None
            and debt_stats["latest"] > debt_stats["mean"] + 1 * debt_stats["std"]
        )
        hy_expansion = (
            hy_stats["mean"] is not None and hy_stats["std"] is not None
            and hy_stats["latest"] is not None
            and hy_stats["latest"] < hy_stats["mean"] - 2 * hy_stats["std"]
        )
        return {
            "yield_curve_slope": yield_curve_slope,
            "fed_rate": fed_rate,
            "credit_spread_oas": hy_stats["latest"],
            "debt_to_gdp_pct": debt_stats["latest"],
            "long_term_force_contraction": bool(hy_contraction or debt_contraction),
            "long_term_force_expansion": bool(hy_expansion),
        }
    except Exception:
        return {
            "yield_curve_slope": None, "fed_rate": None,
            "credit_spread_oas": None, "debt_to_gdp_pct": None,
            "long_term_force_contraction": False,
            "long_term_force_expansion": False,
        }


def _is_earnings_blackout(symbol: str, date_str: str) -> bool:
    """True if symbol has a manually-confirmed earnings date matching today."""
    return date_str in EARNINGS_BLACKOUT.get(symbol, [])


def _is_macro_event_day(date_str: str) -> bool:
    """True if date_str is a known FOMC decision day."""
    return date_str in MACRO_EVENT_DATES


def _record_suppression_stat(result: dict) -> None:
    """
    Tallies every scan into _suppression_stats, flagging the "weak regime +
    inside opening range" state Kimi's round-4 review asked us to monitor
    (Section 8's suppression-rate question). Counts ALL scans that produced a
    result, not just ones that cleared the score threshold, so the rate
    reflects the full population.
    """
    _suppression_stats["total_scans"] += 1
    sigs = result.get("signals", {})
    trend_conf = sigs.get("trend", {}).get("regime_confidence")
    or_label   = sigs.get("or", {}).get("label")
    if trend_conf == "weak" and or_label == "Inside range":
        _suppression_stats["weak_regime_inside_or_scans"] += 1


def get_suppression_stats() -> dict:
    """
    Read-only view of _suppression_stats plus the computed rate. Kimi's
    threshold: if >60% of scans hit "weak regime + inside opening range",
    consider letting Opening Range fire independently when the range is
    unusually wide (>1.5x average) — not yet built, this is the instrumentation
    to decide whether that fix is warranted.
    """
    total = _suppression_stats["total_scans"]
    flagged = _suppression_stats["weak_regime_inside_or_scans"]
    return {
        "total_scans": total,
        "weak_regime_inside_or_scans": flagged,
        "suppression_rate": round(flagged / total, 3) if total else None,
    }


def _queue_for_review(
    symbol: str, side: str, score_value: float,
    result: dict, levels: dict, hold_bars: int, regime_tags: dict,
) -> int:
    """
    Queues a qualifying signal for human review instead of auto-logging it
    (Kimi review, round 6 — D.E. Shaw hybrid model, used only on EXTREME
    days). Stores the full signal/levels payload so approval can replay the
    ORIGINAL model call into log_hypothetical_trade rather than re-fetching
    market data that may have moved since.
    """
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO review_queue
                (symbol, side, score_value, signals_json, levels_json,
                 hold_bars, regime_tags_json, status, queued_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'AWAITING_REVIEW', datetime('now'))
            """,
            (symbol, side, score_value, json.dumps(result), json.dumps(levels),
             hold_bars, json.dumps(regime_tags)),
        )
        return cur.lastrowid


def check_review_queue_timeouts() -> int:
    """
    Auto-skips any AWAITING_REVIEW row older than REVIEW_QUEUE_TIMEOUT_SEC
    (5 min) — the safety valve is optional, not a blocking step, so an
    un-reviewed signal defaults to the conservative outcome (skip) rather
    than sitting forever. Returns count of rows timed out.
    """
    with get_db() as conn:
        cur = conn.execute(
            f"""
            UPDATE review_queue SET
                status = 'SKIPPED', resolved_at = datetime('now'), resolved_by = 'auto_timeout'
            WHERE status = 'AWAITING_REVIEW'
              AND queued_at <= datetime('now', '-{REVIEW_QUEUE_TIMEOUT_SEC} seconds')
            """
        )
        return cur.rowcount


def get_review_queue(status: Optional[str] = None, days: int = 7) -> list[dict]:
    """Recent review-queue rows, optionally filtered by status."""
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM review_queue WHERE status = ? "
                "AND queued_at >= datetime('now', ? || ' days') ORDER BY queued_at DESC",
                (status, f"-{days}"),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM review_queue WHERE queued_at >= datetime('now', ? || ' days') "
                "ORDER BY queued_at DESC",
                (f"-{days}",),
            ).fetchall()
    return [dict(r) for r in rows]


def approve_review(review_id: int) -> dict:
    """
    Manually approves a queued signal — replays the originally-stored
    signal/levels payload into log_hypothetical_trade (using the ORIGINAL
    model call, not a re-fetch) and marks the queue row APPROVED.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM review_queue WHERE id = ? AND status = 'AWAITING_REVIEW'",
            (review_id,),
        ).fetchone()
    if not row:
        return {"error": f"Review {review_id} not found or already resolved"}

    row = dict(row)
    result      = json.loads(row["signals_json"])
    levels      = json.loads(row["levels_json"])
    regime_tags = json.loads(row["regime_tags_json"]) if row.get("regime_tags_json") else {}

    trade_id = log_hypothetical_trade(
        symbol=row["symbol"], side=row["side"], score_value=row["score_value"],
        signals=result, levels=levels, hold_bars=row["hold_bars"] or _DEFAULT_HOLD_BARS,
        model_version="v4", regime_tags=regime_tags,
    )
    with get_db() as conn:
        conn.execute(
            "UPDATE review_queue SET status='APPROVED', resolved_at=datetime('now'), "
            "resolved_by='manual', trade_id=? WHERE id=?",
            (trade_id, review_id),
        )
    _run_log.append({
        "ts": _et_now().isoformat(), "event": "REVIEW_APPROVED",
        "sym": row["symbol"], "review_id": review_id, "tid": trade_id,
    })
    return {"review_id": review_id, "approved": True, "trade_id": trade_id}


def skip_review(review_id: int) -> dict:
    """Manually skips a queued signal (reviewer judged it not tradeable)."""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE review_queue SET status='SKIPPED', resolved_at=datetime('now'), "
            "resolved_by='manual' WHERE id=? AND status='AWAITING_REVIEW'",
            (review_id,),
        )
    if cur.rowcount == 0:
        return {"error": f"Review {review_id} not found or already resolved"}
    return {"review_id": review_id, "skipped": True}


def run_open_scan(
    symbols: Optional[list[str]] = None,
    min_score: int = RUNNER_MIN_SCORE,
) -> list[int]:
    """
    Run intraday signal computation on each symbol. Log all signals where
    abs(score) >= min_score as hypothetical trades (is_hypothetical=1,
    no Alpaca order placed). Returns list of trade_ids created.
    Throttles Alpaca API calls to stay under the free-tier limit.

    Applies three pre-trade gates (Kimi review) before logging any entry:
      market regime gate  — on EXTREME days (SPY gap >= 2%), raise the
                             effective threshold to REGIME_EXTREME_MIN_SCORE
                             so the dataset isn't contaminated with signals
                             fired during untradeable volatility
      portfolio cap       — stop logging once (already-open + logged-this-scan)
                             positions reach MAX_CONCURRENT_POSITIONS, since
                             the 8-symbol universe is one correlated tech cluster
      earnings blackout    — skip any symbol with a manually-confirmed earnings
                             date matching today (EARNINGS_BLACKOUT)
    """
    syms = symbols or get_active_watchlist()
    trade_ids: list[int] = []

    base_min_score = min(min_score, SPRINT_MIN_SCORE) if DATA_COLLECTION_SPRINT_MODE else min_score
    market_regime = _fetch_market_regime()
    effective_min_score = (
        REGIME_EXTREME_MIN_SCORE if market_regime["regime"] == "EXTREME" else base_min_score
    )
    if market_regime["regime"] == "EXTREME":
        log.info(
            f"[RUNNER] EXTREME market regime — SPY gap {market_regime['spy_gap_pct']:+.2f}%, "
            f"raising min_score to {effective_min_score}"
        )
        _run_log.append({
            "ts": _et_now().isoformat(), "event": "REGIME_GATE",
            "note": f"EXTREME regime (SPY gap {market_regime['spy_gap_pct']:+.2f}%) — "
                    f"min_score raised to {effective_min_score}",
        })

    today_str     = _et_now().strftime("%Y-%m-%d")
    existing_open = len(_load_open_positions())
    snapshots     = get_snapshots(syms)

    for sym in syms:
        if _is_earnings_blackout(sym, today_str):
            _run_log.append({
                "ts": _et_now().isoformat(), "event": "EARNINGS_BLACKOUT_SKIP", "sym": sym,
            })
            continue

        snap = snapshots.get(sym, {})
        if not snap or snap.get("price", 0) <= 0:
            continue
        try:
            time.sleep(0.40)
            intraday = get_bars(sym, timeframe="5Min", limit=78)
            time.sleep(0.40)
            daily    = get_daily_bars(sym, days=60)

            if not intraday:
                continue

            avg_v  = _avg_daily_vol(daily)
            result = compute_intraday_signals(
                intraday, daily, snap, avg_v, symbol=sym
            )
            if "error" in result:
                continue

            _record_suppression_stat(result)

            score_val = result["score"]["value"]
            if abs(score_val) < effective_min_score:
                continue

            if existing_open + len(trade_ids) >= MAX_CONCURRENT_POSITIONS:
                _run_log.append({
                    "ts": _et_now().isoformat(), "event": "PORTFOLIO_CAP_SKIP", "sym": sym,
                    "note": f"Cap ({MAX_CONCURRENT_POSITIONS}) reached — skipping",
                })
                continue

            levels   = result.get("levels", {})
            time_lbl = result["score"].get("time_label", "UNKNOWN")
            hold_b   = _HOLD_BARS.get(time_lbl, _DEFAULT_HOLD_BARS)
            side     = levels.get("side", "long")

            if market_regime["regime"] == "EXTREME":
                # D.E. Shaw hybrid model (round 6): unprecedented days get an
                # optional human safety valve instead of auto-logging. Not a
                # blocking step — auto-skips after REVIEW_QUEUE_TIMEOUT_SEC.
                review_id = _queue_for_review(sym, side, score_val, result, levels, hold_b, market_regime)
                _run_log.append({
                    "ts": _et_now().isoformat(), "event": "REVIEW_QUEUED",
                    "sym": sym, "score": score_val, "side": side, "review_id": review_id,
                })
                log.info(f"[RUNNER] Queued for review (EXTREME day): {sym} score={score_val} review_id={review_id}")
                continue

            # Predicta only ever analyzes and suggests here — it never places
            # a real order on its own. This scan logs a hypothetical
            # candidate only; a real order is placed exclusively when a
            # human clicks "Buy" on this candidate in the dashboard, which
            # calls execute_high_value_trade() below. Do not reintroduce
            # automatic real-order placement in this function.
            tid = log_hypothetical_trade(
                symbol       = sym,
                side         = side,
                score_value  = score_val,
                signals      = result,       # full compute_intraday_signals() result
                levels       = levels,
                hold_bars    = hold_b,
                model_version= "v4",
                regime_tags  = market_regime,
            )
            trade_ids.append(tid)
            _run_log.append({
                "ts":    _et_now().isoformat(),
                "event": "ENTRY",
                "sym":   sym,
                "score": score_val,
                "side":  side,
                "label": time_lbl,
                "tid":   tid,
            })
            log.info(
                f"[RUNNER] Logged hypothetical {side.upper()} {sym} "
                f"score={score_val} label={time_lbl} tid={tid}"
            )
        except Exception as exc:
            log.warning(f"[RUNNER] scan error for {sym}: {exc}")

    if len(_run_log) > 100:
        _run_log[:] = _run_log[-100:]
    return trade_ids


# ── Position exit checker ─────────────────────────────────────────────────────

def _load_open_positions() -> list[dict]:
    """Return all open hypothetical trades from DB."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, side, entry_price, entry_time,
                   stop_price, target1_price, target_price, planned_hold_bars
            FROM intraday_trades
            WHERE is_hypothetical = 1 AND exit_time IS NULL
            """,
        ).fetchall()
    return [dict(r) for r in rows]


def _load_open_real_positions() -> list[dict]:
    """
    Real (is_hypothetical=0) open positions — created only when a human
    clicks Buy on a candidate in the dashboard (execute_high_value_trade),
    never by the autonomous scan. Tracked separately from
    _load_open_positions() because these must never
    be run through _check_exit()'s local bar-touch heuristic: a real bracket
    order's stop/target fills at Alpaca itself, so the actual order/leg
    status (see check_real_position_exits) is the ground truth, not a
    locally re-derived estimate.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, side, entry_price, entry_time, qty,
                   stop_price, target_price, planned_hold_bars, alpaca_order_id
            FROM intraday_trades
            WHERE is_hypothetical = 0 AND exit_time IS NULL AND alpaca_order_id IS NOT NULL
            """,
        ).fetchall()
    return [dict(r) for r in rows]


def check_real_position_exits(force_close: bool = False) -> dict:
    """
    Reconcile real (human-bought) paper positions against Alpaca's own order
    status, and attest the closing transaction to HSIP — the audited "move"
    is the real close, not a locally-estimated one. This only ever records
    what already happened at the broker against a stop/target/hold-window
    the human already agreed to at buy time — it never opens a new position
    or makes a fresh buy/sell judgment call.

    Two paths:
      - Normal tick: ask Alpaca whether the bracket's stop or target leg has
        actually filled (get_order) — if so, record the real fill price/time.
      - force_close=True (EOD, or a position past its planned hold bars):
        actually liquidate the position at Alpaca (close_position) rather
        than just logging an estimate, then record the current snapshot
        price as the exit price — the same approximation convention the
        existing hypothetical FORCE_CLOSE_EOD path already uses, since a
        market liquidation's exact fill isn't returned synchronously.
    """
    positions = _load_open_real_positions()
    if not positions:
        return {"checked": 0, "closed": 0}

    now_utc = datetime.now(timezone.utc)
    closed = 0

    for pos in positions:
        sym = pos["symbol"]
        try:
            entry_dt = datetime.fromisoformat(pos["entry_time"])
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            elapsed = int((now_utc - entry_dt).total_seconds() / 300)
        except Exception:
            elapsed = 0
        planned = pos.get("planned_hold_bars") or _DEFAULT_HOLD_BARS

        exit_price = None
        exit_reason = None

        if force_close or elapsed >= planned:
            snap = get_snapshots([sym]).get(sym, {})
            current_price = snap.get("price", 0)
            if current_price <= 0:
                continue
            result = close_position(sym)
            if "error" in result:
                log.warning(f"[RUNNER] Real close failed for {sym} tid={pos['id']}: {result['error']}")
                continue
            exit_price = current_price
            exit_reason = "FORCE_CLOSE_EOD" if force_close else "TIME_STOP"
        else:
            order = get_order(pos["alpaca_order_id"])
            if "error" in order:
                continue
            for leg in order.get("legs") or []:
                if leg.get("status") == "filled":
                    exit_price = float(leg.get("filled_avg_price") or 0) or None
                    exit_reason = "TARGET_HIT" if leg.get("type") == "limit" else "STOP_LOSS"
                    break
            if exit_price is None:
                continue

        try:
            alpaca_side = "buy" if pos["side"] == "long" else "sell"
            close_side = "sell" if alpaca_side == "buy" else "buy"
            result = log_trade_exit(
                trade_id         = pos["id"],
                exit_price       = exit_price,
                exit_reason      = exit_reason,
                actual_hold_bars = elapsed,
                slippage_exit    = 0.0,
            )
            closed += 1
            hsip_client.attest_transaction(
                decision_type = close_side,
                strategy_id   = "high_value_intraday",
                model_version = "v4",
                payload = {
                    "predicta_trade_id": pos["id"],
                    "alpaca_order_id":   pos["alpaca_order_id"],
                    "symbol":            sym,
                    "side":              close_side,
                    "qty":               pos.get("qty"),
                    "exit_price":        exit_price,
                    "exit_reason":       exit_reason,
                    "closed_at":         _et_now().isoformat(),
                },
            )
            _run_log.append({
                "ts": _et_now().isoformat(), "event": "REAL_EXIT", "sym": sym,
                "reason": exit_reason, "pnl": result.get("pnl_dollars"), "tid": pos["id"],
            })
            log.info(f"[RUNNER] Real position closed {sym} tid={pos['id']} reason={exit_reason} exit={exit_price}")
        except Exception as exc:
            log.warning(f"[RUNNER] Real exit-logging error {sym} tid={pos['id']}: {exc}")

    return {"checked": len(positions), "closed": closed}


# ── Human-triggered manual execution ────────────────────────────────────────
#
# These are the ONLY two functions in this file that ever place or close a
# real order. Both require an explicit human click (POST /trade/execute/:id
# and POST /trade/close/:id in app.py) — the autonomous scan above never
# calls either. Predicta analyzes and suggests; the human decides to buy or
# sell.

def execute_high_value_trade(trade_id: int) -> dict:
    """
    Places a real Alpaca paper bracket order for an existing hypothetical
    candidate, using exactly the entry/stop/target levels already stored on
    that row — what the human actually saw on the dashboard before clicking
    Buy, not a value re-computed after the fact. Promotes the same row to
    real in place (promote_trade_to_real) rather than creating a duplicate,
    and attests the transaction to HSIP.

    Fixed 2026-08-06 (companion to the same-day Low Value fractional-order
    fix): Alpaca's bracket order class (used here for every trade, not just
    some) never accepts a fractional quantity, regardless of whether the
    underlying symbol is individually fractionable — unlike Low Value's
    plain market order, there's no "check if this symbol allows it" case;
    it's a hard rule for every bracket order. Kelly-sized qty is floored to
    a whole share before submitting; if that floors to 0, the trade is
    rejected with a clear reason instead of a guaranteed Alpaca-side 422.
    """
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, symbol, side, entry_price, qty, stop_price, target_price, planned_hold_bars
            FROM intraday_trades
            WHERE id = ? AND is_hypothetical = 1 AND exit_time IS NULL
            """,
            (trade_id,),
        ).fetchone()
    if not row:
        return {"error": "Candidate not found, already executed, or already closed."}

    pos = dict(row)
    symbol = pos["symbol"]
    qty = pos["qty"]
    stop = pos["stop_price"]
    target = pos["target_price"]
    if not qty or qty <= 0 or not stop or not target:
        return {"error": "This candidate is missing trade levels and cannot be executed."}

    qty = float(int(qty))  # bracket orders never accept fractional quantities on Alpaca
    if qty < 1:
        return {
            "error": (
                f"Position size ({pos['qty']} shares) rounds to 0 whole shares at "
                f"${pos['entry_price']:,.2f}/share — too small for a bracket order."
            )
        }

    alpaca_side = "buy" if pos["side"] == "long" else "sell"
    order_result = place_bracket_order(
        symbol      = symbol,
        qty         = qty,
        side        = alpaca_side,
        entry_price = None,   # market entry — faster fill
        take_profit = target,
        stop_loss   = stop,
    )
    if "error" in order_result:
        return {"error": readable_order_error(order_result)}

    alpaca_order_id = order_result.get("id")
    promote_trade_to_real(trade_id, alpaca_order_id)
    log.info(f"[MANUAL] Human executed BUY {symbol} tid={trade_id} order_id={alpaca_order_id}")

    hsip_client.attest_transaction(
        decision_type = alpaca_side,
        strategy_id   = "high_value_intraday_manual",
        model_version = "v4",
        payload = {
            "predicta_trade_id": trade_id,
            "alpaca_order_id":   alpaca_order_id,
            "symbol":            symbol,
            "side":              alpaca_side,
            "qty":               qty,
            "entry_price":       pos.get("entry_price"),
            "stop_price":        stop,
            "target_price":      target,
            "placed_at":         _et_now().isoformat(),
        },
    )
    return {
        "status":            "SUBMITTED",
        "predicta_trade_id": trade_id,
        "alpaca_order_id":   alpaca_order_id,
        "symbol":            symbol,
        "side":              alpaca_side,
        "qty":               qty,
        "stop":              stop,
        "target":            target,
    }


def close_high_value_trade(trade_id: int) -> dict:
    """
    Human clicked Sell on a real, still-open position — liquidates it at
    Alpaca immediately (fetchers.alpaca.close_position, which also cancels
    the still-open bracket legs), records the real close, and attests it.
    """
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, symbol, side, qty, alpaca_order_id
            FROM intraday_trades
            WHERE id = ? AND is_hypothetical = 0 AND exit_time IS NULL
            """,
            (trade_id,),
        ).fetchone()
    if not row:
        return {"error": "Real open position not found, or already closed."}

    pos = dict(row)
    symbol = pos["symbol"]
    snap = get_snapshots([symbol]).get(symbol, {})
    current_price = snap.get("price", 0)
    if current_price <= 0:
        return {"error": f"Could not get a current price for {symbol} — try again."}

    result = close_position(symbol)
    if "error" in result:
        return {"error": result["error"]}

    exit_result = log_trade_exit(
        trade_id    = trade_id,
        exit_price  = current_price,
        exit_reason = "MANUAL_CLOSE",
    )
    alpaca_side = "buy" if pos["side"] == "long" else "sell"
    close_side  = "sell" if alpaca_side == "buy" else "buy"
    hsip_client.attest_transaction(
        decision_type = close_side,
        strategy_id   = "high_value_intraday_manual",
        model_version = "v4",
        payload = {
            "predicta_trade_id": trade_id,
            "alpaca_order_id":   pos.get("alpaca_order_id"),
            "symbol":            symbol,
            "side":              close_side,
            "qty":               pos.get("qty"),
            "exit_price":        current_price,
            "exit_reason":       "MANUAL_CLOSE",
            "closed_at":         _et_now().isoformat(),
        },
    )
    log.info(f"[MANUAL] Human closed {symbol} tid={trade_id} exit={current_price}")
    return {
        "status":             "CLOSED",
        "predicta_trade_id":  trade_id,
        "symbol":             symbol,
        "exit_price":         current_price,
        **exit_result,
    }


def _check_exit(
    trade: dict,
    recent_bars: list[dict],
    current_price: float,
    now_utc: datetime,
) -> Optional[tuple[str, float, int]]:
    """
    Evaluate exit conditions for one open position.

    Checks actual bar highs/lows so a stop or target touched *within* the
    30-min check interval is not missed (just using snapshot price would miss
    intra-period touches). Priority: stop > target2 > target1 > time.

    Returns (reason, exit_price, elapsed_bars) or None if no exit yet.
    """
    entry_price = trade.get("entry_price") or 0
    if entry_price <= 0 or current_price <= 0:
        return None

    stop    = trade.get("stop_price")
    target1 = trade.get("target1_price")
    target2 = trade.get("target_price")
    side    = trade.get("side") or "long"
    planned = trade.get("planned_hold_bars") or _DEFAULT_HOLD_BARS

    try:
        entry_dt = datetime.fromisoformat(trade["entry_time"])
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=timezone.utc)
        elapsed = int((now_utc - entry_dt).total_seconds() / 300)
    except Exception:
        elapsed = 0

    bars = recent_bars or []

    if side == "long":
        if stop and any(b["l"] <= stop for b in bars):
            return ("STOP_LOSS", stop, elapsed)
        if target2 and any(b["h"] >= target2 for b in bars):
            return ("TARGET_2_HIT", target2, elapsed)
        if target1 and any(b["h"] >= target1 for b in bars):
            return ("TARGET_1_HIT", target1, elapsed)
    else:   # short
        if stop and any(b["h"] >= stop for b in bars):
            return ("STOP_LOSS", stop, elapsed)
        if target2 and any(b["l"] <= target2 for b in bars):
            return ("TARGET_2_HIT", target2, elapsed)
        if target1 and any(b["l"] <= target1 for b in bars):
            return ("TARGET_1_HIT", target1, elapsed)

    if elapsed >= planned:
        return ("TIME_STOP", current_price, elapsed)

    return None


def check_and_close_positions(force_close: bool = False) -> dict:
    """
    Check all open hypothetical positions against recent Alpaca bar data.
    Closes any that hit stop, target, or time limit; updates DB via log_trade_exit.
    force_close=True exits everything at current market price (end-of-day call).
    Returns {"checked": N, "closed": M}.
    """
    positions = _load_open_positions()
    if not positions:
        return {"checked": 0, "closed": 0}

    symbols   = list({p["symbol"] for p in positions})
    snapshots = get_snapshots(symbols)
    now_utc   = datetime.now(timezone.utc)
    closed    = 0

    for pos in positions:
        sym           = pos["symbol"]
        snap          = snapshots.get(sym, {})
        current_price = snap.get("price", 0)
        if current_price <= 0:
            continue

        if force_close:
            reason, price, elapsed = "FORCE_CLOSE_EOD", current_price, 0
        else:
            try:
                time.sleep(0.20)
                recent = get_bars(sym, timeframe="5Min", limit=6)
            except Exception:
                recent = []
            exit_info = _check_exit(pos, recent, current_price, now_utc)
            if not exit_info:
                continue
            reason, price, elapsed = exit_info

        try:
            result = log_trade_exit(
                trade_id         = pos["id"],
                exit_price       = price,
                exit_reason      = reason,
                actual_hold_bars = elapsed,
                slippage_exit    = 0.0,
            )
            closed += 1
            _run_log.append({
                "ts":     _et_now().isoformat(),
                "event":  "EXIT",
                "sym":    sym,
                "reason": reason,
                "pnl":    result.get("pnl_dollars"),
                "pnl_r":  result.get("pnl_r"),
                "tid":    pos["id"],
            })
            log.info(
                f"[RUNNER] Closed {sym} tid={pos['id']} reason={reason} "
                f"pnl_$={result.get('pnl_dollars')} pnl_r={result.get('pnl_r')}"
            )
        except Exception as exc:
            log.warning(f"[RUNNER] exit error {sym} tid={pos['id']}: {exc}")

    if len(_run_log) > 100:
        _run_log[:] = _run_log[-100:]
    return {"checked": len(positions), "closed": closed}


# ── Background scheduler loop ─────────────────────────────────────────────────

def _runner_loop(symbols: list[str], min_score: int) -> None:
    """
    Background thread body. Sleeps 60s between ticks; on each tick decides
    whether to scan, check positions, or force-close end-of-day.
    """
    global _runner_active

    today_scanned: Optional[str] = None
    last_check_mono: float       = 0.0
    CHECK_INTERVAL_SEC: float    = 30 * 60   # 30 minutes

    log.info("[RUNNER] Background loop started")

    while _runner_active:
        try:
            check_review_queue_timeouts()  # cheap DB check, runs every tick regardless of market hours

            now_et  = _et_now()
            today   = now_et.strftime("%Y-%m-%d")

            if now_et.weekday() >= 5 or not _is_market_open():
                time.sleep(60)
                continue

            # Morning scan — once per trading day, inside 9:35-9:59 window
            if _in_scan_window() and today_scanned != today:
                log.info(f"[RUNNER] Morning scan for {today}")
                ids           = run_open_scan(symbols=symbols, min_score=min_score)
                today_scanned = today
                last_check_mono = time.monotonic()
                log.info(f"[RUNNER] Scan complete — {len(ids)} trades logged")

            # End-of-day force close at 15:50 ET. This also covers any real
            # position a human bought via the dashboard's Buy button earlier
            # that day — High Value's whole model is "never held overnight,"
            # a rule already agreed to the moment the human clicked Buy on a
            # same-day engine, not a new autonomous decision. See
            # check_real_position_exits' docstring.
            elif _near_close() and today_scanned == today:
                log.info("[RUNNER] EOD force-close")
                check_and_close_positions(force_close=True)
                check_real_position_exits(force_close=True)
                # Sleep until well past close so this doesn't re-trigger
                time.sleep(15 * 60)
                continue

            # Mid-day position check every 30 min
            elif (
                today_scanned == today
                and time.monotonic() - last_check_mono >= CHECK_INTERVAL_SEC
            ):
                _check_intraday_regime_escalation()
                summary = check_and_close_positions()
                log.info(f"[RUNNER] Position check — {summary}")
                # No-op if there are no real (human-bought) open positions —
                # this only ever reconciles/records what Alpaca's own
                # bracket order already executed against the stop/target the
                # human approved at buy time; it never opens a new position.
                real_summary = check_real_position_exits()
                if real_summary["checked"]:
                    log.info(f"[RUNNER] Real position check — {real_summary}")
                last_check_mono = time.monotonic()

        except Exception as exc:
            log.error(f"[RUNNER] Loop error: {exc}")

        time.sleep(60)

    log.info("[RUNNER] Background loop exited")


def start_runner(
    symbols: Optional[list[str]] = None,
    min_score: int = RUNNER_MIN_SCORE,
) -> bool:
    """
    Start the background paper runner thread (daemon, will not block shutdown).
    Returns True if started fresh, False if already running.
    """
    global _runner_thread, _runner_active
    if _runner_active and _runner_thread and _runner_thread.is_alive():
        return False
    _runner_active = True
    active_symbols = symbols or get_active_watchlist()
    _runner_thread = threading.Thread(
        target   = _runner_loop,
        args     = (active_symbols, min_score),
        daemon   = True,
        name     = "paper-runner",
    )
    _runner_thread.start()
    log.info(f"[RUNNER] Started — symbols={active_symbols} min_score={min_score}")
    return True


def stop_runner() -> None:
    """Signal the runner loop to stop on its next tick."""
    global _runner_active
    _runner_active = False
    log.info("[RUNNER] Stop signalled")


def get_runner_status() -> dict:
    """
    Current runner state for the /trade/paper-runner/status endpoint.
    Returns activity flag, config, open position count, last 20 log events,
    data-collection-sprint-mode state, and the weak-regime/opening-range
    suppression stats (Kimi review, round 4).
    """
    open_pos = _load_open_positions()
    pending_review = len(get_review_queue(status="AWAITING_REVIEW", days=1))
    return {
        "active":         _runner_active and bool(_runner_thread and _runner_thread.is_alive()),
        "universe_mode":  ACTIVE_UNIVERSE_MODE,
        "symbols":        get_active_watchlist(),
        "large_cap_symbols": RUNNER_SYMBOLS,
        "low_price_candidates": LOW_PRICE_WATCHLIST_CANDIDATES,
        "low_price_ceiling": LOW_PRICE_CEILING,
        "min_score":      RUNNER_MIN_SCORE,
        "data_collection_sprint_mode": DATA_COLLECTION_SPRINT_MODE,
        "sprint_min_score": SPRINT_MIN_SCORE if DATA_COLLECTION_SPRINT_MODE else None,
        "hsip_attestation_enabled": hsip_client.HSIP_ENABLED,
        "market_open":    _is_market_open(),
        "et_now":         _et_now().isoformat(),
        "open_positions": len(open_pos),
        "open_symbols":   [p["symbol"] for p in open_pos],
        "open_real_positions": len(_load_open_real_positions()),
        "suppression_stats": get_suppression_stats(),
        "pending_review":  pending_review,
        "recent_log":     list(reversed(_run_log[-20:])),
    }
