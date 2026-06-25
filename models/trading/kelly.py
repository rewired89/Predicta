"""
Trading position sizing.
- trading_kelly: classical Kelly when historical win/loss stats exist.
- atr_position_size: volatility-targeting sizing (preferred; risks 1% of capital).
- kelly_from_signals: entry point used by pipelines — wraps ATR sizing.
Always paper mode only.
"""
from __future__ import annotations


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


def kelly_from_signals(score: float, atr_pct: float, bankroll: float = 10000.0) -> dict:
    """
    ATR-based position sizing — replaces the previous score→win_rate heuristic.
    Risks 1% of bankroll per trade with stop at 1.5× ATR.
    Score gate: positions only when |score| ≥ 20 (Buy/Sell threshold).

    The old approach (win_rate = 0.5 + score/100 * 0.25) had no statistical
    basis before 50+ closed trades exist. ATR sizing is regime-agnostic and
    doesn't pretend to know win rate from a composite score.
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
            "win_rate": None, "avg_win_pct": round(atr_pct * 1.5, 2),
            "avg_loss_pct": round(atr_pct * 1.5, 2), "win_loss_ratio": 1.0,
            "note": "Score below ±20 threshold — no edge detected, no trade.",
            "paper_mode": True,
        }

    risk_pct       = 0.01                      # risk 1% per trade
    stop_dist_pct  = 1.5 * atr_pct / 100      # stop = 1.5× ATR as fraction of price
    risk_amount    = bankroll * risk_pct
    pos_dollars    = round(min(risk_amount / stop_dist_pct, bankroll * 0.25), 2)
    kelly_fraction = round(pos_dollars / bankroll * 100, 2)

    return {
        "full_kelly":     kelly_fraction,
        "kelly_fraction": kelly_fraction,
        "position_size":  pos_dollars,
        "bankroll":       bankroll,
        "edge_pct":       round(abs_score / 2, 1),
        "win_rate":       None,
        "avg_win_pct":    round(atr_pct * 1.5, 2),
        "avg_loss_pct":   round(atr_pct * 1.5, 2),
        "win_loss_ratio": 1.0,
        "note": (
            f"ATR sizing: 1% risk (${risk_amount:.0f}) ÷ 1.5× ATR stop "
            f"= ${pos_dollars:,.0f} position. Signal score: {score:+.0f}."
        ),
        "paper_mode": True,
    }
