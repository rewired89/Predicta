"""
Automaton engine — autonomous-execution, self-learning paper trading runner
(2026-08-28, direct user request).

What this is, in the user's own framing: an "Automaton AI"-style strategy
(github.com/Conway-Research/automaton) for Predicta — one that scans for
underrated stocks that could move meaningfully within six months, buys them,
and sells before a thesis that stopped working costs real money — but
adapted per the user's explicit, direct answers on how far to take the
analogy:

  - Autonomous execution: YES, for PAPER trading only. Every other engine in
    this codebase (High Value, Low Value) only ever logs a hypothetical
    candidate and waits for a human to click Buy — a deliberate authority
    model (CLAUDE.md: "Predicta only analyze and predict, I do the action of
    buying and sell"), reaffirmed after an earlier same-day attempt to
    auto-place REAL orders was reverted the same day it was tried. Automaton
    does not touch that boundary: it talks to the exact same Alpaca PAPER
    endpoint every other engine uses (fetchers/alpaca.py's PAPER_BASE_URL,
    guarded by _assert_paper_mode()) — no real money is capable of moving
    through this file. What's new is that within that paper sandbox, the
    scan itself places and closes the paper order — no click required. Real
    money stays 100% human-gated via the existing execute_*/close_* +
    passcode endpoints in every other engine, unchanged.

  - Kill switch: NO. Asked directly whether the strategy should disable
    itself on a drawdown (the "earn or die" half of the real Automaton
    project), the user was explicit: "It shouldn't be killed, it should
    learn." There is no drawdown auto-pause anywhere in this file.

  - Cloning: NO. Asked directly, the user said "No clone." There is no
    capital-scaling or variant-spawning logic anywhere in this file.

  - Learning: YES — this is what the user actually wants ported from the
    real Automaton's continuous, unsupervised loop. See
    models/trading/automaton/learning.py: as Automaton's own trades close,
    it reweights its 8-signal composite toward whatever has actually been
    winning, using the exact same calibration machinery Low Value already
    had (LOW_VALUE_README.md: "not yet wired to SIGNAL_WEIGHTS") — now wired,
    scoped to Automaton's own trade history, never Low Value's.

Deliberately reuses Low Value's proven scanner, universe, news overlay, and
8-signal thesis engine (models/trading/low_value/*) rather than duplicating
them — "underrated stock, positive catalyst, currently overlooked" is the
same thesis Low Value already scores; what differs here is the hold horizon
(~6 months, not 5 trading days) and that this engine acts on its own
findings instead of only suggesting them. The universe itself
(get_daily_universe, imported from low_value_runner) is SHARED with Low
Value — same daily scan, same Alpaca/Finnhub call budget, both engines just
score/act on it independently. Sizing, exit levels, and calibration are all
scoped separately (engine='automaton'), so nothing here can corrupt Low
Value's own live calibration data or vice versa.
"""
from __future__ import annotations
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from fetchers.high_value_runner import _et_now, _et_minutes
from fetchers.alpaca import get_daily_bars, get_snapshots, get_asset_shortability, get_top_movers, get_most_active
from fetchers.trading_logger import (
    log_low_value_trade, log_trade_exit, promote_trade_to_real,
    log_automaton_scan_completed, get_automaton_scan_for_date,
)
from fetchers import hsip_client
from db.database import get_db

from fetchers.low_value_runner import get_daily_universe, _trading_days_elapsed, NEGATIVE_THESES
from models.trading.low_value.scanner import build_low_value_universe  # noqa: F401 (documents the shared source; not called directly here)
from models.trading.low_value.news_overlay import scan_universe_news
from models.trading.low_value.thesis_tracker import (
    compute_thesis_score, dominant_thesis_type, sector_etf_for_symbol, ENTRY_THRESHOLD,
)
from models.trading.automaton import learning as automaton_learning
from models.trading.shared.kelly import automaton_position_size, AUTOMATON_MAX_CONCURRENT_POSITIONS

log = logging.getLogger("automaton_runner")

ENGINE: str = "automaton"

# Scan window deliberately offset 15 minutes after Low Value's (8:00-8:14 ET,
# fetchers/low_value_runner.py) — both engines share the same
# get_daily_universe() cache/build and the same Finnhub 60-calls/min news
# budget (news_overlay.scan_universe_news), so running back-to-back rather
# than simultaneously avoids doubling load on that budget at the same moment.
AUTOMATON_SCAN_HOUR_ET: int = 8
AUTOMATON_SCAN_START_MINUTE: int = 15

# ~6 trading months (21 trading days/month x 6) — "less than 6 months" per
# the user's original ask. A starting guess, not backtested (same "no
# fabricated calibration" discipline as every other exit parameter in this
# codebase) — real Automaton trade history is what would ever justify
# changing it.
AUTOMATON_MAX_HOLD_DAYS: int = 126
AUTOMATON_TARGET_PCT: float = 0.50
AUTOMATON_STOP_PCT: float = 0.50

# Data-collection sprint mode — same rationale as Low Value's own
# LOW_VALUE_DATA_COLLECTION_SPRINT_MODE (fetchers/low_value_runner.py): at
# the real ENTRY_THRESHOLD (40), a live production scan of this same
# universe/signal engine found only ~2 qualifying candidates out of 500
# evaluated. Automaton's learning loop (models/trading/automaton/learning.py)
# needs its OWN 20/50/100 closed-trade tiers filled to ever start reweighting
# — at that qualification rate, reaching them could take months. Lossless
# and reversible: the real composite score is always stored regardless of
# which bar let a trade in, so this can be flipped off later without losing
# any history. Independent constant from Low Value's — tunable separately.
AUTOMATON_DATA_COLLECTION_SPRINT_MODE: bool = True
AUTOMATON_SPRINT_MIN_SCORE: float = 15.0

_runner_thread: Optional[threading.Thread] = None
_runner_active: bool = False
_run_log: list[dict] = []

# Real-time movers cross-check (added 2026-08-29, direct user request: "we
# need Predicta to get that data" about what's actually moving today).
# get_top_movers/get_most_active are Alpaca's own free screener endpoints
# (/v1beta1/screener/stocks/{movers,most-actives}) — same Alpaca key already
# configured for everything else in this codebase, no new credentials, and
# already proven working code (models/trading/screener.py's run_screener,
# use_movers=True). They were never wired into Low Value's or Automaton's
# universe before this — build_low_value_universe only ever asks "is this
# symbol cheap/liquid/not bankrupt," never "is this symbol actually moving
# TODAY, and in which direction." This adds today's real gainers, losers,
# and most-active-by-volume symbols on top of that filtered universe as
# extra candidates each scan, purely additive — the existing universe is
# untouched, this can only ever add symbols, never remove any.
AUTOMATON_MOVERS_CROSS_CHECK: bool = True
AUTOMATON_MOVERS_LIMIT: int = 50
AUTOMATON_MOST_ACTIVE_LIMIT: int = 30


def _todays_movers_symbols() -> dict:
    """
    Today's real Alpaca gainers/losers/most-active symbols, deduped. Fails
    safe to an empty set on any API error (get_top_movers/get_most_active
    already fail safe themselves) — a movers-API hiccup degrades to "scan
    the core universe only," same as before this feature existed, never a
    scan failure.
    """
    try:
        movers = get_top_movers(AUTOMATON_MOVERS_LIMIT)
        active = get_most_active(AUTOMATON_MOST_ACTIVE_LIMIT)
    except Exception as exc:
        log.warning(f"[AUTOMATON] Movers cross-check fetch failed: {exc}")
        return {"symbols": set(), "gainers": 0, "losers": 0, "most_active": 0}

    gainers = {g.get("symbol") for g in movers.get("gainers", []) if g.get("symbol")}
    losers = {l.get("symbol") for l in movers.get("losers", []) if l.get("symbol")}
    most_active = {a.get("symbol") for a in active if a.get("symbol")}
    return {
        "symbols": gainers | losers | most_active,
        "gainers": len(gainers), "losers": len(losers), "most_active": len(most_active),
    }

_scan_lock = threading.Lock()
_scan_in_progress: bool = False
_scan_thread: Optional[threading.Thread] = None
_last_scan_started_at: Optional[str] = None
_last_scan_completed_at: Optional[str] = None
_last_scan_trade_ids: list[int] = []
_last_scan_error: Optional[str] = None

# Same outer-watchdog treatment as low_value_runner.py's MAX_SCAN_SECONDS —
# run_automaton_scan has no internal time budget of its own.
MAX_SCAN_SECONDS: float = 900.0


def _seconds_since(iso_ts: Optional[str]) -> Optional[float]:
    if not iso_ts:
        return None
    try:
        started = datetime.fromisoformat(iso_ts)
        return (datetime.now(started.tzinfo) - started).total_seconds()
    except Exception:
        return None


def _clear_stale_scan() -> None:
    global _scan_in_progress, _last_scan_error, _last_scan_completed_at
    if not _scan_in_progress:
        return
    elapsed = _seconds_since(_last_scan_started_at)
    if elapsed is not None and elapsed > MAX_SCAN_SECONDS:
        log.error(
            f"[AUTOMATON] Scan watchdog: still 'in progress' after {elapsed:.0f}s "
            f"(budget {MAX_SCAN_SECONDS:.0f}s) — treating as hung/failed, clearing the flag"
        )
        _scan_in_progress = False
        _last_scan_completed_at = _et_now().isoformat()
        _last_scan_error = (
            f"Watchdog: scan exceeded {MAX_SCAN_SECONDS:.0f}s without completing "
            "(a dependency call likely hung past its own timeout) — treated as failed, safe to retry"
        )


def _in_scan_window() -> bool:
    """True during the 15-minute Automaton scan window on weekdays."""
    now = _et_now()
    if now.weekday() >= 5:
        return False
    t = _et_minutes()
    scan_start = AUTOMATON_SCAN_HOUR_ET * 60 + AUTOMATON_SCAN_START_MINUTE
    return scan_start <= t < scan_start + 15


def _load_open_positions() -> list[dict]:
    """All open Automaton positions — always real (alpaca_order_id set) once
    _auto_execute_entry succeeds; a row can stay hypothetical only if that
    autonomous order placement itself failed (see its docstring)."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, side, entry_price, entry_time, lv_thesis_type,
                   qty, alpaca_order_id
            FROM intraday_trades
            WHERE engine = ? AND exit_time IS NULL
            """,
            (ENGINE,),
        ).fetchall()
    return [dict(r) for r in rows]


def _auto_execute_entry(trade_id: int, symbol: str, side: str, qty: float) -> dict:
    """
    Places a real Alpaca PAPER order for a just-logged Automaton candidate —
    no human click, by design (see this module's docstring). Deliberately a
    self-contained duplicate of the fractional/shortability guard logic in
    fetchers.low_value_runner.execute_low_value_trade (which has a real,
    documented bug history — 2026-08-06 — around exactly this logic) rather
    than a shared refactor: a bug in Automaton's autonomous path must never
    be able to regress Low Value's already-hardened, human-triggered one.

    Returns {"status": "SUBMITTED", ...} on success, or {"error": ...} — a
    failure leaves the trade_id row hypothetical (never retried automatically;
    still valid data for learning.py, since the entry_score was real).
    """
    from fetchers.alpaca import place_order, get_asset_fractionability, friendly_order_error

    alpaca_side = "buy" if side == "long" else "sell"

    if side == "short":
        qty = float(int(qty))  # floor to whole shares — fractional shorting isn't supported by Alpaca
        if qty < 1:
            return {"error": f"Position size rounds to 0 whole shares — too small to short {symbol}."}
    elif qty != int(qty):
        frac = get_asset_fractionability(symbol)
        if frac.get("fractionable") is not True:
            qty = float(int(qty))
            if qty < 1:
                return {"error": f"{symbol} doesn't support fractional-share orders and the position size is too small for 1 whole share."}

    order_result = place_order(
        symbol=symbol, qty=qty, side=alpaca_side, order_type="market", time_in_force="day",
    )
    if "error" in order_result:
        return {"error": friendly_order_error(order_result)}

    alpaca_order_id = order_result.get("id")
    promote_trade_to_real(trade_id, alpaca_order_id)
    log.info(f"[AUTOMATON] Autonomous {alpaca_side.upper()} {symbol} tid={trade_id} order_id={alpaca_order_id}")

    hsip_client.attest_transaction(
        decision_type=alpaca_side,
        strategy_id="automaton_learning",
        model_version="v1",
        payload={
            "predicta_trade_id": trade_id, "alpaca_order_id": alpaca_order_id,
            "symbol": symbol, "side": alpaca_side, "qty": qty,
            "placed_at": _et_now().isoformat(), "autonomous": True,
        },
    )
    return {"status": "SUBMITTED", "alpaca_order_id": alpaca_order_id, "symbol": symbol, "side": alpaca_side, "qty": qty}


def run_automaton_scan(symbols: Optional[list[str]] = None) -> list[int]:
    """
    Scans the (Low-Value-shared) daily universe PLUS today's real Alpaca
    movers/most-active symbols (see AUTOMATON_MOVERS_CROSS_CHECK above —
    additive only, never narrows the core universe), scores each candidate
    with Automaton's own learned weights (models.trading.automaton.learning.
    effective_weights — starts identical to the static Low Value weights,
    reweights itself once enough Automaton trades have closed), logs and
    IMMEDIATELY autonomously executes any candidate crossing the effective
    entry bar. Returns the trade_ids created (whether or not execution
    succeeded — a failed autonomous order still leaves real calibration data).

    An explicit `symbols` override (e.g. a manual scan-now call with a
    specific list) is used exactly as given — the movers cross-check only
    applies to the normal daily-universe path, never silently expands a
    caller-specified list.
    """
    if symbols is not None:
        syms = symbols
        movers_info = None
    else:
        core = set(get_daily_universe())
        movers_info = _todays_movers_symbols() if AUTOMATON_MOVERS_CROSS_CHECK else {"symbols": set(), "gainers": 0, "losers": 0, "most_active": 0}
        syms = list(core | movers_info["symbols"])
        _run_log.append({
            "ts": _et_now().isoformat(), "event": "UNIVERSE_BUILT",
            "core_count": len(core), "movers_added": len(movers_info["symbols"] - core),
            "gainers": movers_info["gainers"], "losers": movers_info["losers"], "most_active": movers_info["most_active"],
            "total_count": len(syms),
        })
    if not syms:
        return []

    open_positions = _load_open_positions()
    existing_open = len(open_positions)
    already_open_symbols = {p["symbol"] for p in open_positions}
    news_by_symbol = scan_universe_news(syms)
    weights = automaton_learning.effective_weights()
    trade_ids: list[int] = []
    min_score = AUTOMATON_SPRINT_MIN_SCORE if AUTOMATON_DATA_COLLECTION_SPRINT_MODE else ENTRY_THRESHOLD

    for sym in syms:
        if sym in already_open_symbols:
            continue
        if existing_open + len(trade_ids) >= AUTOMATON_MAX_CONCURRENT_POSITIONS:
            _run_log.append({
                "ts": _et_now().isoformat(), "event": "PORTFOLIO_CAP_SKIP", "sym": sym,
                "note": f"Cap ({AUTOMATON_MAX_CONCURRENT_POSITIONS}) reached — skipping",
            })
            break
        try:
            daily_bars = get_daily_bars(sym, days=25)
            if not daily_bars:
                continue
            sector_etf = sector_etf_for_symbol(sym)
            sector_bars = get_daily_bars(sector_etf, days=25)
            news_result = news_by_symbol.get(sym)

            thesis = compute_thesis_score(sym, daily_bars, sector_bars, news_result, weights=weights)
            composite = thesis.get("composite")
            if composite is None or abs(composite) < min_score:
                continue

            side = "long" if composite > 0 else "short"
            entry_price = daily_bars[-1].get("c")
            if not entry_price or entry_price <= 0:
                continue

            if side == "short":
                shortability = get_asset_shortability(sym)
                if shortability.get("shortable") is False:
                    _run_log.append({
                        "ts": _et_now().isoformat(), "event": "SHORT_BLOCKED_NOT_SHORTABLE", "sym": sym,
                        "note": f"Composite {composite} signaled SHORT but Alpaca reports {sym} is not shortable — skipped, not logged.",
                    })
                    continue

            thesis_type = dominant_thesis_type(thesis, news_result)
            tid = log_low_value_trade(
                symbol=sym, side=side, entry_price=entry_price,
                score_value=composite, thesis_result=thesis, thesis_type=thesis_type,
                news_result=news_result, hold_days=AUTOMATON_MAX_HOLD_DAYS,
                engine=ENGINE, position_dollars=automaton_position_size(entry_price)["position_size"],
            )
            trade_ids.append(tid)

            qty = automaton_position_size(entry_price)["shares"]
            exec_result = _auto_execute_entry(tid, sym, side, qty)
            if "error" in exec_result:
                _run_log.append({
                    "ts": _et_now().isoformat(), "event": "ENTRY_EXECUTE_FAILED", "sym": sym,
                    "score": composite, "side": side, "tid": tid, "note": exec_result["error"],
                })
                log.warning(f"[AUTOMATON] Autonomous execute failed for {sym} tid={tid}: {exec_result['error']}")
            else:
                _run_log.append({
                    "ts": _et_now().isoformat(), "event": "ENTRY_EXECUTED", "sym": sym,
                    "score": composite, "side": side, "thesis_type": thesis_type, "tid": tid,
                })
                log.info(f"[AUTOMATON] {side.upper()} {sym} composite={composite} thesis={thesis_type} tid={tid} — executed autonomously")
        except Exception as exc:
            log.warning(f"[AUTOMATON] scan error for {sym}: {exc}")

    if len(_run_log) > 100:
        _run_log[:] = _run_log[-100:]

    # Durable marker (see db/schema.sql: automaton_scan_log) that a scan
    # happened for this ET date regardless of how many candidates qualified
    # — logged even at trade_ids=[] so a zero-candidate day doesn't look
    # indistinguishable from "never ran" to _runner_loop's catch-up check.
    log_automaton_scan_completed(_et_now().strftime("%Y-%m-%d"), len(trade_ids))
    return trade_ids


def check_automaton_exits() -> dict:
    """
    Daily exit check — same rules as Low Value's check_low_value_exits
    (target/stop/thesis-resolved/time), scoped to engine='automaton' and
    AUTOMATON_MAX_HOLD_DAYS. Every open Automaton position is normally real
    (auto-executed at entry), so this always liquidates the live Alpaca
    paper position before recording the close — the `if alpaca_order_id`
    guard only matters for the rare row whose autonomous entry order itself
    failed (see _auto_execute_entry), which has nothing to liquidate.
    """
    open_positions = _load_open_positions()
    if not open_positions:
        return {"checked": 0, "closed": []}

    symbols = [p["symbol"] for p in open_positions]
    snaps = get_snapshots(symbols)
    news_by_symbol = scan_universe_news(symbols, days=2)
    now_et = _et_now()
    closed = []

    for pos in open_positions:
        sym = pos["symbol"]
        entry_price = pos["entry_price"]
        side = pos["side"]
        current_price = (snaps.get(sym) or {}).get("price") or 0
        if not entry_price or not current_price:
            continue

        pct_move = (current_price - entry_price) / entry_price
        if side == "short":
            pct_move = -pct_move

        exit_reason = None
        if pct_move >= AUTOMATON_TARGET_PCT:
            exit_reason = "TARGET"
        elif pct_move <= -AUTOMATON_STOP_PCT:
            exit_reason = "STOP"
        elif pos.get("lv_thesis_type") in NEGATIVE_THESES:
            fresh_flags = (news_by_symbol.get(sym) or {}).get("flags", [])
            if "POSITIVE_CATALYST" in fresh_flags:
                exit_reason = "THESIS_RESOLVED"
        if exit_reason is None:
            days_held = _trading_days_elapsed(pos["entry_time"], now_et)
            if days_held >= AUTOMATON_MAX_HOLD_DAYS:
                exit_reason = "TIME"

        if exit_reason:
            if pos.get("alpaca_order_id"):
                from fetchers.alpaca import close_position
                liq = close_position(sym)
                if "error" in liq:
                    log.warning(f"[AUTOMATON] Real close failed for {sym} tid={pos['id']}: {liq['error']}")
                    continue

            result = log_trade_exit(pos["id"], current_price, exit_reason)
            closed.append({"symbol": sym, "trade_id": pos["id"], "exit_reason": exit_reason, **result})
            _run_log.append({
                "ts": now_et.isoformat(), "event": "EXIT", "sym": sym,
                "reason": exit_reason, "tid": pos["id"],
            })
            log.info(f"[AUTOMATON] Closed {sym} tid={pos['id']} reason={exit_reason}")

            if pos.get("alpaca_order_id"):
                alpaca_side = "buy" if side == "long" else "sell"
                close_side = "sell" if alpaca_side == "buy" else "buy"
                hsip_client.attest_transaction(
                    decision_type=close_side,
                    strategy_id="automaton_learning",
                    model_version="v1",
                    payload={
                        "predicta_trade_id": pos["id"], "alpaca_order_id": pos["alpaca_order_id"],
                        "symbol": sym, "side": close_side, "qty": pos.get("qty"),
                        "exit_price": current_price, "exit_reason": exit_reason,
                        "closed_at": now_et.isoformat(), "autonomous": True,
                    },
                )

    return {"checked": len(open_positions), "closed": closed}


# ── Human override (safety valve, not the primary path) ─────────────────────
#
# Automaton buys and sells on its own — that's the point. These two exist
# only so a human can intervene on a SINGLE position if they want to (manual
# early close), or retry a specific candidate whose autonomous entry order
# failed — neither is a kill switch on the strategy itself, which keeps
# scanning and learning regardless.

def execute_automaton_trade(trade_id: int) -> dict:
    """Manual retry for a candidate whose autonomous entry order failed at scan time (see _auto_execute_entry)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, symbol, side, qty FROM intraday_trades WHERE id = ? AND engine = ? AND is_hypothetical = 1 AND exit_time IS NULL",
            (trade_id, ENGINE),
        ).fetchone()
    if not row:
        return {"error": "Candidate not found, already executed, or already closed."}
    pos = dict(row)
    if not pos["qty"] or pos["qty"] <= 0:
        return {"error": "This candidate has no valid position size."}
    return _auto_execute_entry(trade_id, pos["symbol"], pos["side"], pos["qty"])


def close_automaton_trade(trade_id: int) -> dict:
    """
    Human-triggered early close of a single real Automaton position — not a
    kill switch on the strategy, just this one position.

    Fixed 2026-09-10 (real user report: "Sell Now doesn't work"): the entry
    order is placed autonomously at 8:15 ET, before the 9:30 ET market open
    — a plain "day" market order submitted pre-market queues as
    accepted/pending and does not actually fill until the open. If a human
    clicks Sell Now in that gap, Alpaca has no real position for the symbol
    yet, and DELETE /v2/positions/{symbol} 404s. This used to surface as a
    raw, unhelpful HTTPError string (close_low_value_trade already got a
    friendly 404 message for the identical race on 2026-08-06; this function
    never did). Now checks the entry order's actual fill status first: if
    it hasn't filled, cancels the pending order instead of trying to close a
    position that doesn't exist yet — so Sell Now genuinely backs the human
    out, rather than just failing with a clearer error.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, symbol, side, qty, entry_price, alpaca_order_id FROM intraday_trades WHERE id = ? AND engine = ? AND is_hypothetical = 0 AND exit_time IS NULL",
            (trade_id, ENGINE),
        ).fetchone()
    if not row:
        return {"error": "Real open position not found, or already closed."}
    pos = dict(row)
    symbol = pos["symbol"]

    from fetchers.alpaca import close_position, get_order, cancel_order

    if pos.get("alpaca_order_id"):
        order_info = get_order(pos["alpaca_order_id"])
        status = order_info.get("status") if isinstance(order_info, dict) else None
        if status and status not in ("filled", "partially_filled"):
            cancel_result = cancel_order(pos["alpaca_order_id"])
            if "error" in cancel_result:
                return {"error": f"{symbol}'s entry order hasn't filled yet (status={status}) and couldn't be canceled: {cancel_result['error']}"}
            exit_result = log_trade_exit(trade_id, pos.get("entry_price") or 0, "CANCELED_UNFILLED")
            log.info(f"[AUTOMATON MANUAL] Human canceled unfilled entry order for {symbol} tid={trade_id} (was {status})")
            return {"status": "CANCELED", "predicta_trade_id": trade_id, "symbol": symbol,
                     "note": "Entry order hadn't filled yet (pre-market/queued) — canceled instead of closed.", **exit_result}

    snap = get_snapshots([symbol]).get(symbol, {})
    current_price = snap.get("price", 0)
    if current_price <= 0:
        return {"error": f"Could not get a current price for {symbol} — try again."}

    result = close_position(symbol)
    if "error" in result:
        if result.get("status_code") == 404:
            return {"error": f"Alpaca has no open position for {symbol} yet — the entry order likely hasn't filled. Check Orders in your Alpaca paper dashboard, then try Sell again once it shows filled."}
        return {"error": result["error"]}

    exit_result = log_trade_exit(trade_id, current_price, "MANUAL_CLOSE")
    alpaca_side = "buy" if pos["side"] == "long" else "sell"
    close_side = "sell" if alpaca_side == "buy" else "buy"
    hsip_client.attest_transaction(
        decision_type=close_side, strategy_id="automaton_learning", model_version="v1",
        payload={
            "predicta_trade_id": trade_id, "alpaca_order_id": pos.get("alpaca_order_id"),
            "symbol": symbol, "side": close_side, "qty": pos.get("qty"),
            "exit_price": current_price, "exit_reason": "MANUAL_CLOSE",
            "closed_at": _et_now().isoformat(), "autonomous": False,
        },
    )
    log.info(f"[AUTOMATON MANUAL] Human closed {symbol} tid={trade_id} exit={current_price}")
    return {"status": "CLOSED", "predicta_trade_id": trade_id, "symbol": symbol, "exit_price": current_price, **exit_result}


def _past_scan_window() -> bool:
    """True once today's normal 8:15-8:29 ET window has already closed (weekdays only)."""
    now = _et_now()
    if now.weekday() >= 5:
        return False
    window_end = AUTOMATON_SCAN_HOUR_ET * 60 + AUTOMATON_SCAN_START_MINUTE + 15
    return _et_minutes() >= window_end


def _runner_loop() -> None:
    """
    Background thread body. Sleeps 60s between ticks; scans once per day,
    normally in the 8:15-8:29 ET window.

    Catch-up behavior (added 2026-08-28, real production issue — the exact
    "scans never started" confusion Low Value hit before it): today_scanned
    is seeded from the DB-persisted automaton_scan_log, not just an
    in-memory None, so a restart mid-day doesn't forget a scan that already
    ran earlier today. And the scan trigger fires on EITHER being inside
    today's normal window OR today's window having already passed with
    still no completed scan on record — so a process that starts (or
    restarts, e.g. a Railway redeploy) AFTER 8:29 ET catches up immediately
    on its next tick instead of silently waiting until tomorrow morning.
    """
    global _runner_active
    today_scanned: Optional[str] = None
    log.info("[AUTOMATON] Background loop started")

    while _runner_active:
        try:
            now_et = _et_now()
            today = now_et.strftime("%Y-%m-%d")
            if today_scanned != today:
                # Seed/refresh from the durable marker once per new day —
                # cheap DB lookup, only matters right after a restart or at
                # the first tick of a new calendar date.
                persisted = get_automaton_scan_for_date(today)
                if persisted:
                    today_scanned = today
            should_scan = (
                now_et.weekday() < 5
                and today_scanned != today
                and (_in_scan_window() or _past_scan_window())
            )
            if should_scan:
                log.info(f"[AUTOMATON] Daily scan for {today}")
                exit_result = check_automaton_exits()
                ids = run_automaton_scan()
                today_scanned = today
                log.info(f"[AUTOMATON] Scan complete — {len(ids)} trades logged")
                _log_scan_event("scheduled", ids, exit_result)
        except Exception as exc:
            log.error(f"[AUTOMATON] Loop error: {exc}")
        time.sleep(60)

    log.info("[AUTOMATON] Background loop exited")


def start_runner() -> bool:
    """Start the background Automaton runner thread. Returns True if started fresh, False if already running."""
    global _runner_thread, _runner_active
    if _runner_active and _runner_thread and _runner_thread.is_alive():
        return False
    _runner_active = True
    _runner_thread = threading.Thread(target=_runner_loop, daemon=True, name="automaton-runner")
    _runner_thread.start()
    log.info("[AUTOMATON] Started")
    return True


def stop_runner() -> None:
    """Signal the Automaton runner loop to stop on its next tick — pauses the daily scan/exit tick, NOT a kill switch on the strategy's learned state (weights/history are untouched and pick up again on restart)."""
    global _runner_active
    _runner_active = False
    log.info("[AUTOMATON] Stop signalled")


def _log_scan_event(triggered_by: str, trade_ids: list[int], exit_result: dict) -> None:
    """Shared by the scheduled loop and manual scan-now — see low_value_runner.py's identically-named helper for why this exists (2026-09-07, direct user request for a per-engine monthly audit trail)."""
    from fetchers.trading_logger import log_scan_event
    closed_list = exit_result.get("closed", [])
    log_scan_event(
        engine="automaton", triggered_by=triggered_by,
        symbols_scanned=len(get_daily_universe(force_refresh=False)),
        trade_ids_logged=trade_ids,
        positions_checked=exit_result.get("checked", 0),
        positions_closed=len(closed_list),
        closed_pnl_dollars=sum(c.get("pnl_dollars") or 0.0 for c in closed_list),
        trade_ids_closed=[c.get("trade_id") for c in closed_list],
    )


def _scan_worker(symbols: Optional[list[str]]) -> None:
    """
    Changed 2026-09-07, direct user request: a manual scan-now now also
    checks existing open positions first (matching what the scheduled daily
    tick already did), logging the combined result to trade_scan_log.
    """
    global _scan_in_progress, _last_scan_completed_at, _last_scan_trade_ids, _last_scan_error
    _last_scan_error = None
    try:
        exit_result = check_automaton_exits()
        ids = run_automaton_scan(symbols=symbols)
        _last_scan_trade_ids = ids
        _log_scan_event("manual", ids, exit_result)
    except Exception as exc:
        _last_scan_error = str(exc)
        log.error(f"[AUTOMATON] Async scan failed: {exc}")
    finally:
        _last_scan_completed_at = _et_now().isoformat()
        _scan_in_progress = False


def trigger_scan_async(symbols: Optional[list[str]] = None) -> dict:
    """Fire-and-forget scan trigger — same reasoning as low_value_runner's trigger_scan_async (a scan is a multi-minute, many-API-call operation and must never block the HTTP request)."""
    global _scan_in_progress, _scan_thread, _last_scan_started_at
    with _scan_lock:
        _clear_stale_scan()
        if _scan_in_progress:
            return {"status": "already_running", "started_at": _last_scan_started_at}
        _scan_in_progress = True
        _last_scan_started_at = _et_now().isoformat()
        _scan_thread = threading.Thread(target=_scan_worker, args=(symbols,), daemon=True, name="automaton-scan")
        _scan_thread.start()
    return {"status": "started", "started_at": _last_scan_started_at}


def get_runner_status() -> dict:
    """Current Automaton runner state for GET /trade/automaton/runner/status."""
    _clear_stale_scan()
    open_pos = _load_open_positions()
    from models.trading.automaton.learning import AUTOMATON_LEARNING_ENABLED
    return {
        "engine": ENGINE,
        "learning_enabled": AUTOMATON_LEARNING_ENABLED,
        "active": _runner_active and bool(_runner_thread and _runner_thread.is_alive()),
        "autonomous_execution": True,
        "kill_switch": False,
        "cloning": False,
        "scan_hour_et": AUTOMATON_SCAN_HOUR_ET,
        "scan_start_minute_et": AUTOMATON_SCAN_START_MINUTE,
        "max_hold_trading_days": AUTOMATON_MAX_HOLD_DAYS,
        "max_positions": AUTOMATON_MAX_CONCURRENT_POSITIONS,
        "entry_threshold": ENTRY_THRESHOLD,
        "data_collection_sprint_mode": AUTOMATON_DATA_COLLECTION_SPRINT_MODE,
        "sprint_min_score": AUTOMATON_SPRINT_MIN_SCORE if AUTOMATON_DATA_COLLECTION_SPRINT_MODE else None,
        "movers_cross_check": AUTOMATON_MOVERS_CROSS_CHECK,
        "open_positions": len(open_pos),
        "open_symbols": [p["symbol"] for p in open_pos],
        "et_now": _et_now().isoformat(),
        "recent_log": list(reversed(_run_log[-20:])),
        "scan_in_progress": _scan_in_progress,
        "last_scan_started_at": _last_scan_started_at,
        "last_scan_completed_at": _last_scan_completed_at,
        "last_scan_trade_ids": _last_scan_trade_ids,
        "last_scan_error": _last_scan_error,
    }
