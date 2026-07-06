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
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo as _ZI
    _EASTERN = _ZI("America/New_York")
except ImportError:
    _EASTERN = None  # fallback: use UTC offset approximation

from fetchers.alpaca import get_snapshots, get_bars, get_daily_bars
from fetchers.trading_logger import log_hypothetical_trade, log_trade_exit
from models.trading.intraday import compute_intraday_signals
from db.database import get_db

log = logging.getLogger("paper_runner")

# ── Configuration ─────────────────────────────────────────────────────────────

# Single-tier large-cap universe per Kimi's advice: avoids mixing volatility
# regimes in the first 30-40 trades so calibration weights are meaningful.
RUNNER_SYMBOLS: list[str] = [
    "AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META", "GOOGL", "TSLA",
]

# Cast wide net for data; the trade endpoint uses 40 as the action threshold
RUNNER_MIN_SCORE: int = 20

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

# Earnings blackout (Kimi review, structural gap D — low priority). Empty by
# default; add "SYMBOL": ["YYYY-MM-DD", ...] entries manually as real earnings
# dates are confirmed. No live earnings-calendar API is wired in yet, so this
# is a mechanism, not a populated calendar.
EARNINGS_BLACKOUT: dict[str, list[str]] = {}

# Hold duration per time-of-day label, in 5-min bars
_HOLD_BARS: dict[str, int] = {
    "MORNING_TREND":   12,   # 60 min — ride the morning move
    "AFTERNOON_TREND": 12,   # 60 min
    "CLOSE_REVERSAL":   6,   # 30 min — short hold near close
    "OPEN_NOISE":       6,   # 30 min, suppressed anyway
    "LUNCH_CHOP":       6,   # 30 min, suppressed anyway
}
_DEFAULT_HOLD_BARS: int = 6

# ── Internal state ────────────────────────────────────────────────────────────

_runner_thread: Optional[threading.Thread] = None
_runner_active: bool = False
_run_log: list[dict] = []           # ring buffer — last 100 events


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


def _fetch_market_regime() -> dict:
    """
    Coarse "should we even be trading today" gate (Kimi review, structural
    gap E). Uses SPY's overnight gap as a volatility-regime proxy — no VIX
    access on the Alpaca free tier — and XLK's daily change as a simple
    sector-rotation tag for the (all-tech) watchlist.

    Returns {"regime": "NORMAL"|"EXTREME", "spy_gap_pct": float, "xlk_change_pct": float}.
    Fails safe to NORMAL/0.0 on any API error so a data hiccup never blocks
    the whole scan.
    """
    try:
        snaps = get_snapshots(["SPY", "XLK"])
        spy = snaps.get("SPY", {})
        xlk = snaps.get("XLK", {})
        spy_open = spy.get("open", 0)
        spy_prev = spy.get("prev_close", 0)
        spy_gap_pct = round((spy_open - spy_prev) / spy_prev * 100, 3) if spy_prev else 0.0
        xlk_change_pct = xlk.get("change_pct", 0.0)
        regime = "EXTREME" if abs(spy_gap_pct) >= REGIME_GAP_THRESHOLD_PCT else "NORMAL"
        return {"regime": regime, "spy_gap_pct": spy_gap_pct, "xlk_change_pct": xlk_change_pct}
    except Exception:
        return {"regime": "NORMAL", "spy_gap_pct": 0.0, "xlk_change_pct": 0.0}


def _is_earnings_blackout(symbol: str, date_str: str) -> bool:
    """True if symbol has a manually-confirmed earnings date matching today."""
    return date_str in EARNINGS_BLACKOUT.get(symbol, [])


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
    syms = symbols or RUNNER_SYMBOLS
    trade_ids: list[int] = []

    market_regime = _fetch_market_regime()
    effective_min_score = (
        REGIME_EXTREME_MIN_SCORE if market_regime["regime"] == "EXTREME" else min_score
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

            # End-of-day force close at 15:50 ET
            elif _near_close() and today_scanned == today:
                log.info("[RUNNER] EOD force-close")
                check_and_close_positions(force_close=True)
                # Sleep until well past close so this doesn't re-trigger
                time.sleep(15 * 60)
                continue

            # Mid-day position check every 30 min
            elif (
                today_scanned == today
                and time.monotonic() - last_check_mono >= CHECK_INTERVAL_SEC
            ):
                summary = check_and_close_positions()
                log.info(f"[RUNNER] Position check — {summary}")
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
    _runner_thread = threading.Thread(
        target   = _runner_loop,
        args     = (symbols or RUNNER_SYMBOLS, min_score),
        daemon   = True,
        name     = "paper-runner",
    )
    _runner_thread.start()
    log.info(f"[RUNNER] Started — symbols={symbols or RUNNER_SYMBOLS} min_score={min_score}")
    return True


def stop_runner() -> None:
    """Signal the runner loop to stop on its next tick."""
    global _runner_active
    _runner_active = False
    log.info("[RUNNER] Stop signalled")


def get_runner_status() -> dict:
    """
    Current runner state for the /trade/paper-runner/status endpoint.
    Returns activity flag, config, open position count, and last 20 log events.
    """
    open_pos = _load_open_positions()
    return {
        "active":         _runner_active and bool(_runner_thread and _runner_thread.is_alive()),
        "symbols":        RUNNER_SYMBOLS,
        "min_score":      RUNNER_MIN_SCORE,
        "market_open":    _is_market_open(),
        "et_now":         _et_now().isoformat(),
        "open_positions": len(open_pos),
        "open_symbols":   [p["symbol"] for p in open_pos],
        "recent_log":     list(reversed(_run_log[-20:])),
    }
