"""
Low Value engine — dashboard renderer (Kimi review round 6 follow-up).

Separate from the High Value dashboard (app.py's trade_dashboard()) by
design — "Two tabs, two engines. No mixing." Returns a standalone HTML page;
app.py's GET /trade/low-value/dashboard just calls render_low_value_dashboard()
and wraps it in an HTMLResponse.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

from db.database import get_db
from fetchers.low_value_runner import (
    get_runner_status, _trading_days_elapsed, _et_now,
    LOW_VALUE_TARGET_PCT, LOW_VALUE_STOP_PCT, LOW_VALUE_HOLD_DAYS,
)
from fetchers.trading_logger import get_universe_snapshots
from fetchers.finnhub import get_cached_company_name
from models.trading.shared.signal_calibration import (
    thesis_type_calibration_report, low_value_calibration_readiness,
    low_value_per_signal_accuracy_report,
)

LOW_VALUE_PHASE_TARGETS: dict[str, int] = {
    "insufficient": 20, "preliminary": 50, "dynamic_weights": 100, "empirical_sizing": 100,
}


def _display_name(symbol: str) -> str:
    """'FIGS' -> 'FIGS · FIGS, Inc.', or just 'FIGS' if no cached company name exists (e.g. Yahoo, not Finnhub, was the market-cap source for this symbol) — see get_cached_company_name's docstring for why this never makes a live call."""
    name = get_cached_company_name(symbol)
    return f"{symbol} · {name}" if name else symbol


def _pnl_color(val) -> str:
    if val is None:
        return "#64748b"
    return "#22c55e" if val > 0 else ("#ef4444" if val < 0 else "#64748b")


def _pnl_sign(val) -> str:
    if val is None:
        return "—"
    return f"+${val:,.2f}" if val >= 0 else f"-${abs(val):,.2f}"


# Added 2026-07-16 (per Kimi's review — "communication crisis, not a modeling
# crisis"): the +50%/-50% target/stop and the 5-trading-day time limit were
# always computed and enforced live (fetchers/low_value_runner.py:
# check_low_value_exits), but never surfaced anywhere a user could see them —
# a position card showed entry price and score, nothing about what would
# actually make the model sell. These two helpers turn entry_price/side into
# the real dollar levels check_low_value_exits() checks against, and the
# time-limit progress, so the dashboard and chat brief can show exactly what
# check_low_value_exits() is enforcing instead of leaving it implicit.
#
# The -50% level is deliberately labeled "Catastrophe Cap," not "stop-loss"
# (Kimi's framing, adopted as-is): per Kimi's simulation, a thesis-wrong
# position bleeding ~2%/day exits via the 5-day time limit around -10%, long
# before ever reaching -50% — so the 5-day time limit is the position's real,
# primary risk control, and the -50% level is a rarely-triggered backstop for
# a much sharper single-day move. Showing it as "stop-loss" implied it was
# doing routine risk management it mostly isn't; the distance itself is NOT
# changed here (zero resolved trades exist yet to calibrate a better number
# against — see CLAUDE.md's Low Value section), only how it's labeled/shown.
def _target_stop_prices(entry_price: float, side: str) -> dict:
    if side == "short":
        target = entry_price * (1 - LOW_VALUE_TARGET_PCT)
        catastrophe_cap = entry_price * (1 + LOW_VALUE_STOP_PCT)
    else:
        target = entry_price * (1 + LOW_VALUE_TARGET_PCT)
        catastrophe_cap = entry_price * (1 - LOW_VALUE_STOP_PCT)
    return {"target_price": round(target, 2), "catastrophe_cap_price": round(catastrophe_cap, 2)}


def _time_exit_progress(entry_time_iso: str) -> dict:
    days_held = _trading_days_elapsed(entry_time_iso, _et_now())
    days_held = min(days_held, LOW_VALUE_HOLD_DAYS)
    return {
        "days_held": days_held,
        "hold_limit_days": LOW_VALUE_HOLD_DAYS,
        "days_remaining": max(LOW_VALUE_HOLD_DAYS - days_held, 0),
    }


# Plain-language version of the same target/cap/time-limit levels, written so
# someone who has never traded before knows exactly what to do — not just
# what the numbers are. Computed once here and reused by BOTH the dashboard
# HTML and the chat brief JSON, so the wording never drifts between the two
# (2026-07-16, user-requested — the first version used trading jargon like
# "Target," "Catastrophe cap," and percentages, which isn't usable by someone
# who isn't already a trader; this tool is meant for non-experts too).
def _plain_exit_instructions(entry_price: float, side: str, entry_time_iso: str) -> dict:
    levels = _target_stop_prices(entry_price, side)
    progress = _time_exit_progress(entry_time_iso)
    target = levels["target_price"]
    cap = levels["catastrophe_cap_price"]
    days_remaining = progress["days_remaining"]

    # A LONG position profits when the price goes UP; a SHORT position
    # profits when the price goes DOWN — the sentences have to say the
    # opposite thing depending on which one this is, or they'd be backwards.
    if side == "short":
        profit_line = f"Buy it back and close this out for a profit if the price drops to ${target:,.2f}."
        loss_line = f"Buy it back and close this out to limit the loss if the price rises to ${cap:,.2f} (rare — most positions never get this far)."
    else:
        profit_line = f"Sell for a profit if the price rises to ${target:,.2f}."
        loss_line = f"Sell to limit the loss if the price drops to ${cap:,.2f} (rare — most positions never fall this far)."

    if days_remaining <= 0:
        time_line = "If neither of those has happened yet, the system closes this position today no matter the price."
    else:
        day_word = "day" if days_remaining == 1 else "days"
        time_line = f"If neither of those happens, the system automatically closes this position in {days_remaining} more trading {day_word}, no matter the price."

    return {
        "target_price": target,
        "catastrophe_cap_price": cap,
        "days_held": progress["days_held"],
        "hold_limit_days": progress["hold_limit_days"],
        "days_remaining": days_remaining,
        "profit_line": profit_line,
        "loss_line": loss_line,
        "time_line": time_line,
    }


def _exit_levels_html(t: dict) -> str:
    """Open-position card block telling a non-trader exactly what to do — see
    the comment above _plain_exit_instructions for why the wording is built
    there once instead of duplicated per-caller."""
    instr = _plain_exit_instructions(t["entry_price"], t["side"], t["entry_time"])
    return (
        f'<div class="trade-detail" style="margin-top:4px; line-height:1.6;">'
        f'<div>✅ {instr["profit_line"]}</div>'
        f'<div>⚠️ {instr["loss_line"]}</div>'
        f'<div>⏰ {instr["time_line"]}</div>'
        f'</div>'
    )


# Added 2026-07-16 (Tier 0 of the trading-model audit — see CODEMAP.md).
# High Value's dashboard (app.py: trade_dashboard) has had a "Phase +
# readiness" confidence banner since before this session — it tells the user
# in plain English whether there's enough closed-trade data to trust
# anything the model says, using the same 10/30/50-trade tiers the
# calibration machinery itself uses. Low Value's dashboard never had an
# equivalent — a user could stare at a BUY signal with no way to tell
# whether it's backed by 0 trades or 200. This mirrors High Value's exact
# phase/verdict cascade logic, adapted to Low Value's own 20/50/100-trade
# tiers (thesis_type_calibration_report / low_value_calibration_readiness)
# and its own metrics (win_rate + total P&L on fixed-$25 trades, since Low
# Value doesn't track R-multiples the way High Value does).
def _phase_and_verdict(n_total: int, win_rate, total_pnl) -> dict:
    if n_total < 20:
        phase, phase_color, phase_label = "insufficient", "#f59e0b", "Watching"
        phase_desc = (
            f"The model is logging trades. You need at least 20 closed trades before even a "
            f"preliminary per-thesis-type read exists (see 'Win Rate by Thesis Type' below). "
            f"You have {n_total} so far."
        )
    elif n_total < 50:
        phase, phase_color, phase_label = "preliminary", "#f59e0b", "Preliminary Data"
        phase_desc = (
            f"Enough for an early per-thesis-type read, but not enough to change how the model "
            f"weighs its 8 signals yet. At 50 trades, signal-weight recalibration unlocks. "
            f"You have {n_total}."
        )
    elif n_total < 100:
        phase, phase_color, phase_label = "dynamic_weights", "#38bdf8", "Calibrating"
        phase_desc = (
            f"The model can now start learning which of its 8 signals actually work (see "
            f"'Per-Signal Accuracy' below). At 100 trades, position sizing based on real "
            f"performance unlocks — sizing stays fixed at $25/trade until then. You have {n_total}."
        )
    else:
        phase, phase_color, phase_label = "empirical_sizing", "#a78bfa", "Empirical Sizing Ready"
        phase_desc = "Enough data collected. Position sizing can now reflect actual signal quality instead of a flat $25."

    phase_target = LOW_VALUE_PHASE_TARGETS[phase]
    progress_pct = min(100, int(n_total / phase_target * 100)) if phase_target else 100

    if n_total < 20:
        verdict_color, verdict_icon = "#f59e0b", "🔴"
        verdict_title = "Not ready for real money"
        verdict_msg = f"You need {20 - n_total} more closed trades before even a preliminary read exists."
    elif win_rate is not None and win_rate < 0.50:
        verdict_color, verdict_icon = "#ef4444", "🔴"
        verdict_title = "Not ready — win rate too low"
        verdict_msg = (
            f"Win rate of {win_rate*100:.0f}% means the model is wrong more than it's right so far "
            f"on the trades that have closed. Keep collecting data."
        )
    elif win_rate is not None and win_rate >= 0.55 and total_pnl is not None and total_pnl > 0 and n_total >= 50:
        verdict_color, verdict_icon = "#22c55e", "🟢"
        verdict_title = "Consider graduating to real money"
        verdict_msg = (
            f"Win rate {win_rate*100:.0f}% with positive total P&L across {n_total} trades. "
            f"These are good early signals. Start with very small size — real markets are harder than paper."
        )
    elif win_rate is not None and win_rate >= 0.52:
        verdict_color, verdict_icon = "#38bdf8", "🔵"
        verdict_title = "Getting there"
        verdict_msg = f"Win rate {win_rate*100:.0f}% is a slight edge but not enough to be confident yet. Keep collecting."
    else:
        verdict_color, verdict_icon = "#f59e0b", "🟡"
        verdict_title = "Too early to tell"
        verdict_msg = "Not enough closed trades yet, or the numbers are mixed. Keep the runner going."

    return {
        "phase": phase, "phase_color": phase_color, "phase_label": phase_label,
        "phase_desc": phase_desc, "phase_target": phase_target, "progress_pct": progress_pct,
        "verdict_color": verdict_color, "verdict_icon": verdict_icon,
        "verdict_title": verdict_title, "verdict_msg": verdict_msg,
    }


# Plain-English translations (2026-07-13, user-requested — the raw
# thesis_type/exit_reason codes and the -100..+100 composite score reads as
# jargon to anyone who isn't already familiar with this codebase; a real
# user reading this dashboard mistook the score for a win probability, e.g.
# "31.85 out of 100" the way the sports models report a calibrated win %).
# IMPORTANT: this composite score is NOT a win probability. It's a weighted
# blend of 8 raw signals (price vs 20-day low, RSI, volume spike, insider
# buying, short interest, sector strength, cash burn, news sentiment) — see
# models/trading/low_value/thesis_tracker.py. Unlike the sports pipelines
# (which report a real, backtested "65% chance to win" because they've been
# calibrated against thousands of resolved games), this engine has zero
# resolved trades to calibrate against yet — score-to-win-rate calibration
# (the same 20/50/100-trade tiers signal_calibration.py already tracks) only
# becomes possible once enough of these trades actually close. Until then,
# a higher |score| means "more of the 8 signals agree, more strongly" — not
# "X% chance of being right."
THESIS_TYPE_LABELS: dict[str, str] = {
    # "Oversold" is a common point of confusion (2026-07-13, user-reported):
    # it does NOT mean the price is too high. It means selling has been so
    # heavy/fast that the price likely fell further than the fundamentals
    # justify — sellers overshot. The contrarian bet is that it snaps back
    # toward fair value, i.e. BUY the overshoot, not "it's overpriced, avoid."
    "TECHNICAL_OVERSOLD":  "sold off hard and fast — the bet is sellers overshot and it bounces back, not that the price is too high",
    "INSIDER_BUYING":      "company insiders have been buying their own stock recently",
    "EARNINGS_MISS":       "missed earnings — the bet is the market's reaction overshot",
    "ANALYST_DOWNGRADE":   "an analyst downgrade — the bet is the market's reaction overshot",
    "REGULATORY_RISK":     "a regulatory/legal scare — the bet is the market's reaction overshot",
    "OPERATIONAL_CRISIS":  "layoffs/restructuring news — the bet is the market's reaction overshot",
    "POSITIVE_CATALYST":   "a positive news catalyst",
    "UNKNOWN":             "unclassified signal",
}

EXIT_REASON_LABELS: dict[str, str] = {
    "TARGET":          "sold for a profit — the price moved 50% in our favor",
    "STOP":             "sold to limit the loss — the price moved 50% against us (this is rare; most losing trades exit via the 5-day time limit below, at a much smaller loss)",
    "TIME":             "sold automatically after 5 trading days — the price never reached the profit point or the loss-limit point",
    "THESIS_RESOLVED":  "sold — the bad news that caused the price drop turned out to be temporary, like the model expected",
}

# Per-signal plain-English translator (2026-07-13, user-requested — "the
# signals or news or whatever the model thinks"). Mirrors the exact detail
# dict shape each _score_* function in thesis_tracker.py returns; a signal
# not present here (shouldn't happen — this list matches SIGNAL_WEIGHTS)
# falls back to a generic "name: score" line rather than crashing.
def _signal_explanation(name: str, entry: dict) -> str:
    score = entry.get("score")
    detail = entry.get("detail") or {}
    try:
        if name == "price_vs_20d_low":
            return f"Trading {detail['ratio']:.0%} above its 20-day low (${detail['20d_low']:.2f} → ${detail['close']:.2f}) — the closer to 0%, the nearer a recent bottom"
        if name == "rsi_14":
            rsi = detail["rsi_14"]
            zone = "oversold (heavy recent selling)" if rsi < 30 else "overbought" if rsi > 70 else "neutral"
            return f"RSI(14) is {rsi:.0f} — {zone}"
        if name == "volume_spike":
            return f"Today's volume is {detail['spike_ratio']:.1f}x the 20-day average — a spike often means capitulation selling (or, less often, a real move starting)"
        if name == "insider_buying_30d":
            bought, sold = detail.get("insider_buying_30d"), detail.get("insider_selling_30d")
            if sold is None:  # legacy row, logged before insider-selling tracking existed
                return "Company insiders bought shares in the last 30 days" if bought else "No recorded insider buying in the last 30 days"
            if bought and not sold:
                return "Insiders bought shares and did NOT sell — a strong sign this is fear-driven selling, not a real problem with the company"
            if bought and sold:
                return "Mixed insider activity — some insiders bought, but others sold, in the same 30 days"
            if sold:
                return "Insiders SOLD shares during this window with no offsetting purchases — a red flag against the 'sellers overshot' bet"
            return "No recorded insider buying or selling in the last 30 days"
        if name == "short_interest_pct":
            asof = detail.get("short_interest_as_of") or "unknown date"
            return f"Short interest is {detail['short_interest_pct']:.1f}% of float (as of {asof}) — higher means more potential for a short squeeze if it turns"
        if name == "sector_relative_strength":
            return f"Last 5 days: stock {detail['symbol_5d_return_pct']:+.1f}% vs. its sector ETF {detail['sector_5d_return_pct']:+.1f}% ({detail['relative_pp']:+.1f}pp relative)"
        if name == "cash_burn_months":
            base = f"~{detail['cash_burn_months']:.1f} months of cash runway left at the current burn rate"
            if detail.get("recent_dilutive_filing"):
                base += (" — WARNING: an SEC filing in the last 90 days shows the company sold new shares, "
                         "which can extend runway by diluting existing shareholders rather than through real cash flow")
            return base
        if name == "news_sentiment":
            return f"{detail.get('headline_count', 0)} recent headlines, sentiment {detail.get('sentiment', 0):+.2f} (-1 very negative to +1 very positive)"
    except (KeyError, TypeError, ValueError):
        pass
    return f"{name.replace('_', ' ')}: {score}"


def _signals_breakdown_html(signals_json: str | None) -> str:
    if not signals_json:
        return '<div class="muted-note" style="margin-top:6px;">Detailed signal breakdown wasn\'t recorded for this trade (opened before this feature) — new trades will show it.</div>'
    try:
        signals = json.loads(signals_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not signals:
        return ""
    lines = "".join(f"<li>{_signal_explanation(name, entry)}</li>" for name, entry in signals.items())
    return f'<ul style="margin:6px 0 0 18px; padding:0; font-size:.78rem; color:var(--muted); line-height:1.5;">{lines}</ul>'


def _thesis_label(code: str) -> str:
    return THESIS_TYPE_LABELS.get(code or "UNKNOWN", (code or "unclassified").replace("_", " ").lower())


def _exit_label(code: str) -> str:
    return EXIT_REASON_LABELS.get(code or "", code or "closed")


# Added 2026-07-18, direct user request: the previous card led with "Signal
# strength 62/100 (not a win probability) · sold off hard and fast — the bet
# is..." all crammed into one line, then a separate list of up to 8 raw
# technical readings (RSI, volume spike, etc.) right below it. Individually
# each piece was already plain-English (per THESIS_TYPE_LABELS,
# _signal_explanation), but there was no single, concrete "why" sentence a
# non-trader could read once and understand — and the user was explicit that
# technical language loses them entirely. These two functions build that
# single lead sentence; the full signal-by-signal breakdown still exists
# (_signals_breakdown_html) but moves into a collapsed <details> block,
# available for anyone who wants it without being the first thing shown.
def _plain_why(symbol: str, thesis_type: str, signals_json: str | None) -> str:
    """One concrete sentence: why the model flagged this stock."""
    thesis_desc = _thesis_label(thesis_type)
    sentence = thesis_desc[0].upper() + thesis_desc[1:] if thesis_desc else "No specific reason recorded"
    concrete = ""
    if signals_json:
        try:
            signals = json.loads(signals_json)
            pvl = (signals.get("price_vs_20d_low") or {}).get("detail") or {}
            if pvl.get("ratio") is not None and pvl.get("close") is not None:
                concrete = (
                    f" It's trading at ${pvl['close']:.2f}, only {pvl['ratio']*100:.0f}% "
                    f"above its lowest price in the last 20 days."
                )
        except (TypeError, ValueError):
            pass
    return f"{sentence}.{concrete}"


def _plain_confidence(score) -> str:
    """Translates the raw -100..100 composite into a plain descriptor instead
    of leading with a bare number — the raw score stays available in the
    collapsed technical details for anyone who wants it."""
    if score is None:
        return "Confidence: unknown"
    a = abs(score)
    if a >= 70:
        return "Confidence: very strong — multiple signals strongly agree"
    if a >= 55:
        return "Confidence: strong"
    return "Confidence: moderate — just past the bar the model requires to act at all"


def get_low_value_brief(days: int = 7) -> dict:
    """
    Read-only recap for the "give me a brief" / "this week" / "yesterday"
    query box on the Low Value page — same DB-only philosophy as the
    dashboard, never triggers a live scan. days controls the closed-trade
    lookback window (1 for "yesterday", 7 for "this week"/default).
    """
    with get_db() as conn:
        closed_rows = conn.execute(
            """
            SELECT symbol, exit_reason, pnl_dollars, lv_thesis_type
            FROM intraday_trades
            WHERE engine = 'low_value' AND is_hypothetical = 1 AND exit_time IS NOT NULL
              AND exit_time >= datetime('now', ? || ' days')
            ORDER BY exit_time DESC
            """,
            (f"-{days}",),
        ).fetchall()
        open_rows = conn.execute(
            """
            SELECT symbol, side, entry_price, entry_time, entry_score, lv_thesis_type, lv_signals_json
            FROM intraday_trades
            WHERE engine='low_value' AND is_hypothetical=1 AND exit_time IS NULL
            ORDER BY entry_time DESC
            """
        ).fetchall()

    closed = [dict(r) for r in closed_rows]
    open_t = [dict(r) for r in open_rows]
    open_count = len(open_t)
    n_closed = len(closed)
    win_rate = total_pnl = None
    if n_closed:
        wins = sum(1 for t in closed if (t.get("pnl_dollars") or 0) > 0)
        win_rate = round(wins / n_closed, 3)
        total_pnl = round(sum(t.get("pnl_dollars") or 0 for t in closed), 2)

    recent_snapshots = get_universe_snapshots(days=1)
    universe_size = recent_snapshots[0]["symbol_count"] if recent_snapshots else 0

    # open_positions stays a plain count (unchanged shape — existing frontend
    # renders it directly as a number); open_positions_detail is new and
    # additive, carrying the per-symbol target/catastrophe-cap/time-limit
    # levels that were previously invisible outside the dashboard HTML page.
    open_positions_detail = []
    for t in open_t:
        instr = _plain_exit_instructions(t["entry_price"], t["side"], t["entry_time"])
        open_positions_detail.append({
            "symbol": t["symbol"],
            "side": t["side"],
            "entry_price": round(t["entry_price"], 2),
            "why": _plain_why(t["symbol"], t.get("lv_thesis_type"), t.get("lv_signals_json")),
            "confidence": _plain_confidence(t.get("entry_score")),
            **instr,
        })

    # confidence_note uses ALL-TIME closed trades (low_value_calibration_readiness),
    # not the days-windowed n_closed above — calibration readiness is about total
    # historical evidence, not just this week's trades. Same "how confident should
    # I really be" gap the 2026-07-16 audit flagged: nothing told the user how much
    # (or how little) data backs the scores they're looking at.
    readiness = low_value_calibration_readiness()
    n_total = readiness["total_closed_trades"]
    if n_total < 20:
        confidence_note = (
            f"Confidence: only {n_total} closed trade(s) so far — need 20 for even a preliminary "
            f"read. Treat every score above as an untested hypothesis, not a probability."
        )
    elif n_total < 50:
        confidence_note = f"Confidence: {n_total} closed trades — enough for an early read, not enough to trust yet."
    elif n_total < 100:
        confidence_note = f"Confidence: {n_total} closed trades — real patterns are starting to show, still building toward reliable."
    else:
        confidence_note = f"Confidence: {n_total} closed trades — enough for position sizing to start reflecting real performance."

    return {
        "mode": "brief",
        "days": days,
        "universe_size": universe_size,
        "confidence_note": confidence_note,
        "open_positions_detail": open_positions_detail,
        "open_positions": open_count,
        "closed_trades": n_closed,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "recent_trades": [
            {
                "symbol": t["symbol"],
                "thesis_type": t.get("lv_thesis_type") or "UNKNOWN",
                "exit_reason": t.get("exit_reason") or "—",
                "pnl_dollars": t.get("pnl_dollars") or 0.0,
            }
            for t in closed[:10]
        ],
    }


def render_low_value_dashboard() -> str:
    with get_db() as conn:
        closed_rows = conn.execute(
            """
            SELECT symbol, side, entry_time, exit_time, entry_price, exit_price, pnl_dollars, pnl_pct,
                   exit_reason, entry_score, lv_thesis_type, lv_signals_json
            FROM intraday_trades
            WHERE engine = 'low_value' AND is_hypothetical = 1 AND exit_time IS NOT NULL
            ORDER BY exit_time DESC LIMIT 50
            """
        ).fetchall()
        open_rows = conn.execute(
            """
            SELECT id, symbol, side, entry_time, entry_price, entry_score, lv_thesis_type,
                   lv_news_flags, lv_signals_json, is_hypothetical, alpaca_order_id, qty
            FROM intraday_trades
            WHERE engine = 'low_value' AND exit_time IS NULL
            ORDER BY entry_time DESC
            """
        ).fetchall()

    closed = [dict(r) for r in closed_rows]
    open_t = [dict(r) for r in open_rows]
    n_closed = len(closed)

    win_rate = avg_pnl = total_pnl = None
    if n_closed:
        wins = sum(1 for t in closed if (t.get("pnl_dollars") or 0) > 0)
        win_rate = round(wins / n_closed, 3)
        total_pnl = round(sum(t.get("pnl_dollars") or 0 for t in closed), 2)
        avg_pnl = round(total_pnl / n_closed, 2)

    thesis_report = thesis_type_calibration_report(min_trades=3)
    readiness = low_value_calibration_readiness()
    pv = _phase_and_verdict(readiness["total_closed_trades"], win_rate, total_pnl)
    signal_report = low_value_per_signal_accuracy_report(min_trades=10)
    runner_status = get_runner_status()
    # Cross-engine exposure (Tier 2, 2026-07-16/17 trading-model audit) — see
    # models/trading/shared/exposure.py's docstring for why this is
    # visibility-only, not a combined position cap.
    try:
        from models.trading.shared.exposure import get_cross_engine_exposure
        cross_exposure = get_cross_engine_exposure()
    except Exception:
        cross_exposure = None
    # Read-only: the most recently LOGGED universe snapshot, never a live
    # rescan (a rescan is a slow, multi-API-call operation that belongs to
    # the runner's scheduled job or a manual /trade/low-value/scan-now call,
    # not to loading a dashboard page).
    recent_snapshots = get_universe_snapshots(days=1)
    universe_size = recent_snapshots[0]["symbol_count"] if recent_snapshots else 0
    stagnant_filtered = 0
    scan_timing_note = ""
    funnel_note = ""
    data_source_warning = ""
    if recent_snapshots and recent_snapshots[0].get("filter_stats_json"):
        try:
            fstats = json.loads(recent_snapshots[0]["filter_stats_json"])
            stagnant_filtered = fstats.get("stagnant_filtered_count", 0)
            if "elapsed_sec" in fstats:
                scan_timing_note = (
                    f" · last scan took {fstats['elapsed_sec']:.0f}s, evaluated {fstats.get('candidates_evaluated', 0)} candidates"
                    + (" (TIME BUDGET EXCEEDED — partial result)" if fstats.get("time_budget_exceeded") else "")
                )
            if "volume_filtered_count" in fstats:
                funnel_note = (
                    f"Funnel: {fstats.get('candidates_evaluated', 0)} evaluated → "
                    f"{fstats.get('no_bar_data_count', 0)} no-bar-data, "
                    f"{stagnant_filtered} stagnant, {fstats.get('volume_filtered_count', 0)} low-volume, "
                    f"{fstats.get('market_cap_unavailable_count', 0)} cap-unavailable, "
                    f"{fstats.get('market_cap_too_small_count', 0)} cap-too-small, "
                    f"{fstats.get('bankruptcy_filtered_count', 0)} bankruptcy → {universe_size} passed"
                )
                evaluated = fstats.get("candidates_evaluated", 0)
                no_bar_data = fstats.get("no_bar_data_count", 0)
                if evaluated > 0 and no_bar_data / evaluated > 0.2:
                    data_source_warning += (
                        f'<div class="card" style="border-color:#f59e0b;">'
                        f'<div class="card-title" style="color:#f59e0b;">No Bar Data Warning</div>'
                        f'<div style="font-size:.85rem;">{no_bar_data}/{evaluated} candidates ({no_bar_data/evaluated:.0%}) '
                        f'had no daily-bar history from Alpaca — its free-tier feed is IEX only (not SIP), which has thin '
                        f'coverage for illiquid/small-cap names. This silently shrank every scan before this counter existed '
                        f'(2026-07-12) — it is a data-source coverage gap, not a real filter result.</div>'
                        f'</div>'
                    )
                cap_unavailable = fstats.get("market_cap_unavailable_count", 0)
                if evaluated > 0 and cap_unavailable / evaluated > 0.5:
                    data_source_warning += (
                        f'<div class="card" style="border-color:#f59e0b;">'
                        f'<div class="card-title" style="color:#f59e0b;">Data Source Warning</div>'
                        f'<div style="font-size:.85rem;">Market cap was unavailable for {cap_unavailable}/{evaluated} '
                        f'candidates ({cap_unavailable/evaluated:.0%}) — check FINNHUB_API_KEY is set in Railway and that '
                        f'Yahoo Finance isn\'t blocking Railway\'s IP. This is a data-source problem, not a real filter result.</div>'
                        f'</div>'
                    )
        except Exception:
            stagnant_filtered = 0
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    runner_dot = "#22c55e" if runner_status.get("active") else "#ef4444"
    runner_lbl = "Running" if runner_status.get("active") else "Stopped"
    sprint_lbl = (
        f" &nbsp;·&nbsp; <span style=\"color:#f59e0b;\">SPRINT MODE (logging bar {runner_status['sprint_min_score']:.0f}, real threshold {runner_status.get('entry_threshold', 40):.0f})</span>"
        if runner_status.get("data_collection_sprint_mode") else ""
    )

    scan_banner_html = ""
    if runner_status.get("scan_in_progress") or runner_status.get("universe_build_in_progress"):
        started = runner_status.get("last_scan_started_at") or runner_status.get("last_universe_build_started_at")
        scan_banner_html = (
            f'<div class="card" style="border-color:#f59e0b;">'
            f'<div class="card-title" style="color:#f59e0b;">Scan In Progress</div>'
            f'<div style="font-size:.85rem;">Started {started}. This page does not auto-update — refresh in a few minutes.</div>'
            f'</div>'
        )
    elif runner_status.get("last_scan_error"):
        scan_banner_html = (
            f'<div class="card" style="border-color:#ef4444;">'
            f'<div class="card-title" style="color:#ef4444;">Last Scan Failed</div>'
            f'<div style="font-size:.85rem;">{runner_status["last_scan_error"]}</div>'
            f'</div>'
        )
    elif runner_status.get("last_universe_build_error"):
        scan_banner_html = (
            f'<div class="card" style="border-color:#ef4444;">'
            f'<div class="card-title" style="color:#ef4444;">Last Universe Build Failed</div>'
            f'<div style="font-size:.85rem;">{runner_status["last_universe_build_error"]}</div>'
            f'</div>'
        )

    def _lv_action_html(t: dict) -> str:
        tid = t.get("id")
        if not t.get("is_hypothetical", 1):
            return (
                f'<span class="real-badge">🔴 REAL ORDER PLACED</span> '
                f'<button class="trade-btn sell-btn" onclick="predictaAction(\'/trade/low-value/close/{tid}\', \'SELL — close this position now\')">Sell Now</button>'
            )
        side_lbl = "BUY" if t["side"] == "long" else "SHORT"
        return (
            f'<button class="trade-btn buy-btn" onclick="predictaAction(\'/trade/low-value/execute/{tid}\', \'{side_lbl} {_display_name(t["symbol"])} — place a real paper order\')">{side_lbl} — place real order</button>'
        )

    open_rows_html = "".join(
        f"""<div class="trade-row">
              <div class="trade-icon">{"🟢" if t['side']=='long' else "🔴"}</div>
              <div class="trade-info">
                <span class="trade-sym">{"BUY" if t['side']=='long' else "SHORT"} {_display_name(t['symbol'])}</span>
                <div class="trade-why">{_plain_why(t['symbol'], t.get('lv_thesis_type'), t.get('lv_signals_json'))}</div>
                <span class="trade-detail">Bought at ${t['entry_price']:,.2f}/share (real stock price) · {_plain_confidence(t.get('entry_score'))}</span>
                {_exit_levels_html(t)}
                <span class="trade-time">opened {t['entry_time'][:16]} UTC — this is a fixed $25 paper position, not shares bought at full account size</span>
                <div style="margin-top:8px;">{_lv_action_html(t)}</div>
                <details style="margin-top:8px;">
                  <summary style="cursor:pointer; font-size:.78rem; color:var(--muted);">See the technical details (raw score {t.get('entry_score') or '—'}/100)</summary>
                  {_signals_breakdown_html(t.get('lv_signals_json'))}
                </details>
              </div>
            </div>"""
        for t in open_t
    ) or '<div class="muted-note">No open Low Value positions right now — the model didn\'t find a strong enough signal on the last scan. Predicta only ever suggests here — nothing is bought automatically.</div>'

    closed_rows_html = "".join(
        f"""<div class="exit-block">
              <div class="exit-item" style="border-bottom:none; padding:0;">
                <span>{"BUY" if t.get('side')=='long' else "SHORT" if t.get('side') else ''} {_display_name(t['symbol'])} · {_thesis_label(t.get('lv_thesis_type'))} · {_exit_label(t.get('exit_reason'))}</span>
                <span style="color:{_pnl_color(t.get('pnl_dollars'))}">P&amp;L: {_pnl_sign(t.get('pnl_dollars'))}</span>
              </div>
              <div style="font-size:.76rem; color:var(--muted); margin-top:2px;">
                Real stock price: ${t['entry_price']:,.2f}/share → ${t['exit_price']:,.2f}/share ({t.get('pnl_pct') or 0:+.1f}%) — the P&amp;L above is on a fixed $25 paper position, not the share price itself
              </div>
              {_signals_breakdown_html(t.get('lv_signals_json'))}
            </div>"""
        for t in closed[:15]
    ) or '<div class="muted-note">No closed Low Value trades yet — nothing has hit its target, stop, or time limit.</div>'

    thesis_rows_html = "".join(
        f"""<div class="exit-item">
              <span>{row['thesis_type']}</span>
              <span class="exit-count">n={row['n']}{f" · {row['win_rate']:.0%} win" if row.get('win_rate') is not None else ''}</span>
            </div>"""
        for row in thesis_report.get("by_thesis_type", [])
    ) or '<div class="muted-note">No thesis-type data yet.</div>'

    signal_rows_html = ""
    for s in signal_report.get("by_signal", []):
        wr = s.get("win_rate")
        if wr is None:
            signal_rows_html += f"<div class='exit-item'><span>{s['signal']}</span><span class='exit-count'>{s.get('note', 'insufficient data')}</span></div>"
            continue
        color = "#22c55e" if wr >= 0.54 else "#f59e0b" if wr >= 0.48 else "#ef4444"
        avg_r = s.get("avg_pnl_r")
        avg_r_str = f"{avg_r:+.2f}R" if avg_r is not None else "—"
        signal_rows_html += f"""
        <div class="exit-item">
          <span>{s['signal']}</span>
          <span class="exit-count" style="color:{color}">{wr*100:.0f}% WR · {avg_r_str} · n={s['n']}</span>
        </div>"""
    if not signal_rows_html:
        signal_rows_html = "<p class='muted-note'>No per-signal data yet — need 10+ active trades per signal.</p>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Predicta — Low Value Monitor</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg: #0b1120; --surface: #131d30; --border: #1e2d47;
    --blue: #38bdf8; --green: #22c55e; --amber: #f59e0b;
    --red: #ef4444; --purple: #a78bfa; --text: #e2e8f0; --muted: #64748b;
  }}
  body {{ background: var(--bg); color: var(--text); font-family: system-ui, -apple-system, sans-serif; min-height: 100vh; padding-bottom: 60px; }}
  header {{ background: var(--surface); border-bottom: 1px solid var(--border); padding: 16px 20px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
  .logo {{ font-size: 1.2rem; font-weight: 700; color: var(--purple); }}
  .header-meta {{ margin-left: auto; font-size: .78rem; color: var(--muted); }}
  .runner-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: {runner_dot}; margin-right: 4px; vertical-align: middle; }}
  main {{ max-width: 680px; margin: 0 auto; padding: 20px 16px; display: flex; flex-direction: column; gap: 16px; }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }}
  .card-title {{ font-size: .7rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); margin-bottom: 14px; }}
  .stat-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  .stat-box {{ background: var(--bg); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }}
  .stat-value {{ font-size: 2rem; font-weight: 800; line-height: 1; margin-bottom: 4px; }}
  .stat-label {{ font-size: .7rem; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }}
  .trade-row {{ display: flex; align-items: center; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--border); }}
  .trade-row:last-child {{ border-bottom: none; }}
  .trade-icon {{ font-size: 1.1rem; flex-shrink: 0; width: 22px; text-align: center; }}
  .trade-info {{ flex: 1; min-width: 0; display: flex; flex-direction: column; }}
  .trade-sym {{ font-weight: 700; font-size: .95rem; }}
  .trade-detail {{ font-size: .8rem; color: var(--muted); }}
  .trade-why {{ font-size: .88rem; color: var(--text); margin: 4px 0; line-height: 1.5; }}
  .trade-time {{ font-size: .72rem; color: var(--muted); margin-top: 2px; }}
  .trade-btn {{ background: var(--blue); color: #04121f; border: none; border-radius: 8px; padding: 8px 14px; font-size: .82rem; font-weight: 700; cursor: pointer; }}
  .trade-btn:hover {{ filter: brightness(1.1); }}
  .buy-btn {{ background: var(--green); }}
  .sell-btn {{ background: var(--red); color: #fff; }}
  .real-badge {{ display: inline-block; font-size: .7rem; font-weight: 700; color: var(--red); margin-right: 8px; }}
  .exit-item {{ display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid var(--border); font-size: .83rem; }}
  .exit-item:last-child {{ border-bottom: none; }}
  .exit-block {{ padding: 7px 0; border-bottom: 1px solid var(--border); }}
  .exit-block:last-child {{ border-bottom: none; }}
  .exit-count {{ color: var(--muted); }}
  .muted-note {{ font-size: .82rem; color: var(--muted); font-style: italic; }}
  footer {{ text-align: center; font-size: .75rem; color: var(--muted); padding: 20px; }}
  a {{ color: var(--blue); }}
  .nav-row {{ display: flex; gap: 10px; align-items: center; font-size: .8rem; color: var(--muted); flex-wrap: wrap; }}
  .nav-row a {{ color: var(--muted); text-decoration: none; }}
  .nav-row a:hover {{ color: var(--blue); text-decoration: underline; }}
  .phase-banner {{ border-radius: 12px; padding: 20px; border: 2px solid {pv['phase_color']}; background: color-mix(in srgb, {pv['phase_color']} 8%, var(--surface)); }}
  .phase-name {{ font-size: 1.4rem; font-weight: 800; color: {pv['phase_color']}; margin-bottom: 4px; }}
  .phase-desc {{ font-size: .9rem; line-height: 1.55; }}
  .progress-wrap {{ background: var(--border); border-radius: 99px; height: 8px; margin-top: 16px; overflow: hidden; }}
  .progress-bar {{ height: 100%; border-radius: 99px; background: {pv['phase_color']}; width: {pv['progress_pct']}%; transition: width .4s; }}
  .progress-label {{ font-size: .72rem; color: var(--muted); margin-top: 6px; text-align: right; }}
  .verdict-card {{ border: 2px solid {pv['verdict_color']}; border-radius: 12px; padding: 20px; background: color-mix(in srgb, {pv['verdict_color']} 6%, var(--surface)); }}
  .verdict-icon {{ font-size: 2rem; margin-bottom: 8px; }}
  .verdict-title {{ font-size: 1.15rem; font-weight: 700; color: {pv['verdict_color']}; margin-bottom: 10px; }}
  .verdict-body {{ font-size: .88rem; line-height: 1.6; }}
</style>
</head>
<body>
<header>
  <div class="logo">Predicta · Low Value Monitor</div>
  <div class="header-meta">
    <span class="runner-dot"></span>{runner_lbl} &nbsp;·&nbsp; {now_str}{sprint_lbl}
  </div>
</header>
<main>

  <div class="nav-row">
    <a href="/">← Home</a><span>·</span>
    <a href="/trading">Stock Market</a><span>·</span>
    <a href="/trading/low-value">Low Value (query box)</a><span>·</span>
    <a href="/trade/dashboard">High Value dashboard</a>
  </div>

  {scan_banner_html}
  {data_source_warning}

  <div class="phase-banner">
    <div class="phase-name">{pv['phase_label']}</div>
    <div class="phase-desc">{pv['phase_desc']}</div>
    <div class="progress-wrap"><div class="progress-bar"></div></div>
    <div class="progress-label">{readiness['total_closed_trades']} / {pv['phase_target']} trades · {pv['progress_pct']}%</div>
  </div>

  <div class="verdict-card">
    <div class="verdict-icon">{pv['verdict_icon']}</div>
    <div class="verdict-title">{pv['verdict_title']}</div>
    <div class="verdict-body">{pv['verdict_msg']}</div>
  </div>

  <div class="card">
    <div class="card-title">Today's Universe</div>
    <div class="stat-grid">
      <div class="stat-box"><div class="stat-value">{universe_size}</div><div class="stat-label">Symbols scanned</div></div>
      <div class="stat-box"><div class="stat-value">{len(open_t)}</div><div class="stat-label">Open positions (max 3)</div></div>
    </div>
    <div style="font-size:.78rem; color:var(--muted); margin-top:10px;">{stagnant_filtered} excluded as stagnant (volatility floor){scan_timing_note}</div>
    {f'<div style="font-size:.76rem; color:var(--muted); margin-top:6px;">{funnel_note}</div>' if funnel_note else ''}
    <div style="font-size:.78rem; color:var(--muted); margin-top:10px;">Predicta only ever analyzes and suggests here — it never buys or sells on its own. Use the buttons under a suggestion below to place a real order yourself.</div>
    <button class="trade-btn" style="margin-top:10px;" onclick="predictaScanNow()">Scan Now</button>
  </div>

  <div class="card">
    <div class="card-title">Closed P&amp;L ({n_closed} trades)</div>
    <div class="stat-grid">
      <div class="stat-box"><div class="stat-value" style="color:{_pnl_color(total_pnl)}">{_pnl_sign(total_pnl)}</div><div class="stat-label">Total P&amp;L</div></div>
      <div class="stat-box"><div class="stat-value">{f"{win_rate:.0%}" if win_rate is not None else "—"}</div><div class="stat-label">Win rate</div></div>
    </div>
    <div style="font-size:.78rem; color:var(--muted); margin-top:10px;">
      {f"Nothing has finished yet — every open position is still running. Total P&amp;L and win rate only count trades that have actually closed (hit their target, stop, or time limit); this will fill in as positions close." if n_closed == 0 else f"Across the {n_closed} paper trades that have closed so far: total hypothetical profit/loss (fixed $25 per trade, not real money) and the share that were profitable."}
    </div>
  </div>

  <div class="card">
    <div class="card-title">Open Positions</div>
    {open_rows_html}
  </div>

  <div class="card">
    <div class="card-title">Cross-Engine Exposure</div>
    {f'''<div class="exit-item"><span>High Value</span><span class="exit-count">{cross_exposure["high_value_open"]} open / cap {cross_exposure["high_value_cap"]}</span></div>
    <div class="exit-item"><span>Low Value</span><span class="exit-count">{cross_exposure["low_value_open"]} open / cap {cross_exposure["low_value_cap"]}</span></div>
    <div class="exit-item"><span>Pairs</span><span class="exit-count">{cross_exposure["pairs_open"]} open / no cap set</span></div>
    <div style="font-size:.78rem; color:var(--muted); margin-top:10px;">{cross_exposure["total_open"]} total open positions across all engines. {cross_exposure["note"]}</div>''' if cross_exposure else "<p class='muted-note'>Unavailable.</p>"}
  </div>

  <div class="card">
    <div class="card-title">Recent Closed Trades</div>
    {closed_rows_html}
  </div>

  <div class="card">
    <div class="card-title">Win Rate by Thesis Type ({readiness['calibration_quality']})</div>
    {thesis_rows_html}
  </div>

  <div class="card">
    <div class="card-title">Per-Signal Accuracy ({signal_report['total_closed']} closed trades)</div>
    <div style="font-size:.78rem; color:var(--muted); margin-bottom:10px;">
      Which of the 8 signals actually correlate with a winning trade, not just how much weight each one was assigned by guess. A signal below 50% win rate here is a real candidate for being overweighted in the composite.
    </div>
    {signal_rows_html}
  </div>


</main>
<footer>Auto-refreshes every 5 minutes &nbsp;·&nbsp; <a href="/trade/dashboard">High Value dashboard</a></footer>
<script>
async function predictaAction(url, label) {{
  if (!confirm('Confirm: ' + label + '?\\n\\nThis places (or closes) a real Alpaca paper order.')) return;
  const passcode = prompt('Trade passcode (leave blank if none is set):') || '';
  try {{
    const res = await fetch(url, {{ method: 'POST', headers: {{ 'X-Trade-Passcode': passcode }} }});
    const data = await res.json();
    if (!res.ok) {{
      const d = data.detail;
      const msg = (typeof d === 'string') ? d : (d ? JSON.stringify(d) : JSON.stringify(data));
      alert('Failed: ' + msg);
      return;
    }}
    alert('Done.\\n' + JSON.stringify(data, null, 2));
    location.reload();
  }} catch (e) {{ alert('Request failed: ' + e); }}
}}
async function predictaScanNow() {{
  try {{
    const res = await fetch('/trade/low-value/scan-now', {{ method: 'POST' }});
    const data = await res.json();
    alert('Scan started in the background — this can take a few minutes. Reloading now; refresh again shortly to see results.');
    location.reload();
  }} catch (e) {{ alert('Scan failed: ' + e); }}
}}
</script>
</body>
</html>"""
    return html
