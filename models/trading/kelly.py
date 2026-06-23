"""
Kelly Criterion adapted for trading.
Uses win rate + avg win/loss ratio instead of fixed odds.
Always quarter-Kelly for safety. Paper mode only.
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


def kelly_from_signals(score: float, atr_pct: float, bankroll: float = 10000.0) -> dict:
    """
    Estimate Kelly sizing directly from composite score and ATR volatility.
    Used when historical win/loss stats aren't available.
    score:    composite signal score -100 to +100
    atr_pct:  ATR as % of price (daily volatility proxy)
    """
    # Convert score to win probability estimate
    # score=0 → 50%, score=100 → ~75%, score=-100 → ~25%
    win_rate = 0.5 + (score / 100) * 0.25
    win_rate = max(0.3, min(0.8, win_rate))

    # ATR as proxy for average move size
    avg_win_pct  = atr_pct * 1.5   # typical target = 1.5× ATR
    avg_loss_pct = atr_pct * 1.0   # stop = 1× ATR

    return trading_kelly(win_rate, avg_win_pct, avg_loss_pct, bankroll)
