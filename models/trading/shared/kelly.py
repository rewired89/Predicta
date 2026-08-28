"""
Trading position sizing.
- trading_kelly: classical Kelly when historical win/loss stats exist.
- atr_position_size: volatility-targeting sizing (preferred; risks 1% of capital).
- kelly_from_signals: entry point used by pipelines — wraps ATR sizing.
- risk_parity_position_size: Bridgewater-style inverse-vol sizing, gated off
  by default (Kimi review, round 5) — not yet wired into any live pipeline.
Always paper mode only.
"""
from __future__ import annotations

# Gate for risk_parity_position_size (Kimi review, round 5 — Bridgewater's
# core insight: size inversely to volatility so each position contributes
# roughly equal risk, not equal dollars). Off by default: kelly_from_signals
# still uses fixed-1% ATR sizing until real trade data shows risk-parity
# actually improves outcomes vs. the simpler approach.
RISK_PARITY_MODE: bool = False


def trading_kelly(
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
    bankroll: float = 10000.0,
    fraction: float = 0.25,
) -> dict:
    """
    win_rate:     probability of a winning trade (0-1)
    avg_win_pct:  average win size as % of position
    avg_loss_pct: average loss size as % of position (positive number)
    bankroll:     total capital available
    fraction:     Kelly fraction (0.25 = quarter Kelly)
    """
    if avg_loss_pct <= 0 or win_rate <= 0 or win_rate >= 1:
        return {"error": "Invalid inputs"}

    b = avg_win_pct / avg_loss_pct   # win/loss ratio
    p = win_rate
    q = 1 - win_rate

    # Full Kelly: f* = (bp - q) / b
    full_kelly = (b * p - q) / b
    kelly = full_kelly * fraction

    if kelly <= 0:
        return {
            "full_kelly": round(full_kelly * 100, 2),
            "kelly_fraction": round(kelly * 100, 2),
            "position_size": 0,
            "note": "Negative Kelly — no edge. Do not trade.",
            "paper_mode": True,
        }

    position_size = round(bankroll * kelly, 2)
    edge = round((b * p - q) * 100, 2)

    return {
        "full_kelly":      round(full_kelly * 100, 2),
        "kelly_fraction":  round(kelly * 100, 2),
        "position_size":   position_size,
        "bankroll":        bankroll,
        "edge_pct":        edge,
        "win_rate":        round(win_rate * 100, 1),
        "avg_win_pct":     avg_win_pct,
        "avg_loss_pct":    avg_loss_pct,
        "win_loss_ratio":  round(b, 2),
        "note": (
            f"Quarter-Kelly suggests ${position_size:,.2f} position "
            f"({round(kelly*100,1)}% of bankroll). Edge: {edge:+.1f}%."
        ),
        "paper_mode": True,
    }


def atr_position_size(
    price: float,
    atr: float,
    risk_per_trade: float = 0.01,
    account_value: float = 10000.0,
    stop_mult: float = 1.5,
) -> dict:
    """
    Volatility-targeting position size: risk a fixed % of capital per trade,
    sized by ATR so wider stops = fewer shares.
    stop_mult × ATR = stop distance (default 1.5×).
    """
    if atr <= 0 or price <= 0:
        return {"error": "Invalid ATR or price", "paper_mode": True}
    stop_distance = stop_mult * atr
    risk_amount   = account_value * risk_per_trade
    shares        = risk_amount / stop_distance
    pos_dollars   = shares * price
    return {
        "shares":              round(shares, 4),
        "position_size":       round(pos_dollars, 2),
        "stop_distance":       round(stop_distance, 4),
        "risk_amount":         round(risk_amount, 2),
        "risk_pct_of_account": risk_per_trade,
        "paper_mode":          True,
    }


def risk_parity_position_size(
    price: float,
    atr: float,
    realized_vol_pct: float,
    account_value: float = 10000.0,
    base_risk_pct: float = 0.01,
    reference_vol_pct: float = 20.0,
    stop_mult: float = 1.5,
    max_position_pct: float = 0.25,
) -> dict:
    """
    Risk-parity position sizing (Kimi review, round 5 — Bridgewater's core
    insight): size inversely to a ticker's realized volatility so each
    position contributes roughly equal risk instead of equal dollars. If a
    ticker's realized vol is 2x the reference level, its risk budget (and
    therefore size) is halved; half the reference vol doubles it.

    An alternative to atr_position_size's fixed 1% risk — not wired into
    kelly_from_signals or any live pipeline. Gated behind RISK_PARITY_MODE
    (default False) until real trade data validates whether risk-parity
    sizing actually improves outcomes vs. the simpler fixed-risk approach.
    """
    if price <= 0 or atr <= 0 or realized_vol_pct <= 0:
        return {"error": "Invalid price, ATR, or volatility", "paper_mode": True}

    vol_scalar    = round(reference_vol_pct / realized_vol_pct, 3)
    risk_pct      = base_risk_pct * vol_scalar
    stop_distance = stop_mult * atr
    risk_amount   = account_value * risk_pct
    shares        = risk_amount / stop_distance
    pos_dollars   = min(shares * price, account_value * max_position_pct)
    shares        = round(pos_dollars / price, 4) if price else 0

    return {
        "shares":            shares,
        "position_size":     round(pos_dollars, 2),
        "risk_pct_used":     round(risk_pct, 4),
        "vol_scalar":        vol_scalar,
        "realized_vol_pct":  realized_vol_pct,
        "reference_vol_pct": reference_vol_pct,
        "note": (
            f"Risk-parity: {realized_vol_pct:.1f}% vol vs {reference_vol_pct:.1f}% "
            f"reference -> {vol_scalar:.2f}x risk scalar -> {risk_pct*100:.2f}% "
            f"risk (${risk_amount:.0f})"
        ),
        "paper_mode": True,
    }


def kelly_from_signals(score: float, atr_pct: float, bankroll: float = 10000.0) -> dict:
    """
    ATR-based position sizing with empirical win rate overlay.

    Position size: always ATR-based (1% risk, 1.5× ATR stop) — does not scale
    with win rate until calibration reaches "stable" quality (50+ trades).
    Score gate: no position when |score| < 20.

    Empirical win rate and the veto are both gated behind
    calibration_globally_active() — 100+ total closed trades (Kimi review,
    round 2). Below that floor the base ensemble's edge is unvalidated, so
    acting on 30-50 trade calibration data risks tuning noise, not real edge.
    Once active, the veto itself uses a confidence-interval test
    (veto_decision) rather than a raw point-estimate cutoff — a naive
    "win rate < 45%" check at n=15 is noise responding to noise (Kimi review,
    round 1).
    """
    if atr_pct <= 0:
        return {
            "full_kelly": 0, "kelly_fraction": 0, "position_size": 0,
            "bankroll": bankroll, "edge_pct": 0,
            "note": "Invalid ATR — cannot size position.",
            "paper_mode": True,
        }

    abs_score = abs(score)
    if abs_score < 20:
        return {
            "full_kelly": 0, "kelly_fraction": 0, "position_size": 0,
            "bankroll": bankroll, "edge_pct": 0,
            "win_rate": None, "win_rate_source": "none",
            "note": "Score below ±20 threshold — no edge detected, no trade.",
            "paper_mode": True,
        }

    # Empirical win rate + CI-gated veto — both no-op until 100 total closed trades
    empirical_wr     = None
    win_rate_source  = "unavailable"
    veto_info: dict  = {"veto": False}
    try:
        from models.trading.shared.signal_calibration import (
            calibrated_win_rate, veto_decision, calibration_globally_active,
        )
        if calibration_globally_active():
            empirical_wr = calibrated_win_rate(score)
            if empirical_wr is not None:
                win_rate_source = "empirical"
            veto_info = veto_decision(score, min_trades=30, ci_floor=0.48)
        else:
            win_rate_source = "gated_pending_100_trades"
    except Exception:
        pass

    if veto_info.get("veto"):
        return {
            "full_kelly": 0, "kelly_fraction": 0, "position_size": 0,
            "bankroll": bankroll, "edge_pct": 0,
            "win_rate": empirical_wr, "win_rate_source": win_rate_source,
            "note": f"Vetoed: {veto_info.get('reason')}",
            "paper_mode": True,
        }

    risk_pct       = 0.01
    stop_dist_pct  = 1.5 * atr_pct / 100
    risk_amount    = bankroll * risk_pct
    pos_dollars    = round(min(risk_amount / stop_dist_pct, bankroll * 0.25), 2)
    kelly_fraction = round(pos_dollars / bankroll * 100, 2)

    wr_note = (
        f" Empirical win rate: {empirical_wr:.1%}." if empirical_wr is not None
        else " Win rate: unconfirmed (need 50+ trades)."
    )

    return {
        "full_kelly":     kelly_fraction,
        "kelly_fraction": kelly_fraction,
        "position_size":  pos_dollars,
        "bankroll":       bankroll,
        "edge_pct":       round(abs_score / 2, 1),
        "win_rate":       empirical_wr,
        "win_rate_source":win_rate_source,
        "avg_win_pct":    round(atr_pct * 1.5, 2),
        "avg_loss_pct":   round(atr_pct * 1.5, 2),
        "win_loss_ratio": 1.0,
        "note": (
            f"ATR sizing: 1% risk (${risk_amount:.0f}) ÷ 1.5× ATR stop "
            f"= ${pos_dollars:,.0f} position. Signal: {score:+.0f}.{wr_note}"
        ),
        "paper_mode": True,
    }


LOW_VALUE_FIXED_POSITION_DOLLARS: float = 25.0
LOW_VALUE_MAX_CONCURRENT_POSITIONS: int = 3


def low_value_position_size(price: float) -> dict:
    """
    Fixed $25-per-trade sizing for the Low Value engine (Kimi review round 6
    follow-up) — deliberately NOT percentage-of-account or volatility-scaled.
    These are sub-$20, thinly-covered names; a fixed small dollar amount caps
    the damage of a single bad pick regardless of account size, and keeps
    kelly_from_signals / atr_position_size / risk_parity_position_size (the
    High Value engine's sizing logic) completely untouched.
    """
    if price <= 0:
        return {"error": "Invalid price", "paper_mode": True}
    shares = round(LOW_VALUE_FIXED_POSITION_DOLLARS / price, 4)
    return {
        "shares": shares,
        "position_size": LOW_VALUE_FIXED_POSITION_DOLLARS,
        "note": f"Low Value fixed sizing: ${LOW_VALUE_FIXED_POSITION_DOLLARS:.0f} / ${price:.2f} = {shares} shares.",
        "paper_mode": True,
    }


# Automaton engine (added 2026-08-28) — same fixed-dollar-sizing discipline
# as Low Value, deliberately kept as its OWN constant rather than reusing
# LOW_VALUE_FIXED_POSITION_DOLLARS so either engine's size can be tuned
# independently later without affecting the other. See
# fetchers/automaton_runner.py and models/trading/automaton/learning.py.
AUTOMATON_FIXED_POSITION_DOLLARS: float = 25.0
AUTOMATON_MAX_CONCURRENT_POSITIONS: int = 5


def automaton_position_size(price: float) -> dict:
    """
    Fixed $25-per-trade sizing for the Automaton engine — identical shape to
    low_value_position_size, separate constant. Automaton holds positions up
    to ~6 months (vs Low Value's 5 trading days), so a small fixed dollar
    amount matters even more here: capital sits committed far longer per
    trade, and a fixed size still caps the damage of any single bad pick
    regardless of how the thesis plays out over that longer horizon.
    """
    if price <= 0:
        return {"error": "Invalid price", "paper_mode": True}
    shares = round(AUTOMATON_FIXED_POSITION_DOLLARS / price, 4)
    return {
        "shares": shares,
        "position_size": AUTOMATON_FIXED_POSITION_DOLLARS,
        "note": f"Automaton fixed sizing: ${AUTOMATON_FIXED_POSITION_DOLLARS:.0f} / ${price:.2f} = {shares} shares.",
        "paper_mode": True,
    }
