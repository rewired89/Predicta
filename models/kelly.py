"""
Kelly Criterion staking calculator — paper mode only, computes stake size.
Never places real bets; outputs recommended stake for tracking purposes.
"""
from __future__ import annotations


KELLY_FRACTION = 0.25  # quarter-Kelly


def kelly_stake(
    your_prob: float,
    decimal_odds: float,
    bankroll: float,
    fraction: float = KELLY_FRACTION,
) -> dict:
    """
    Compute recommended paper stake.

    Returns a dict with kelly_fraction, recommended_stake, edge, and a note
    that this is paper-mode only.
    """
    b = decimal_odds - 1.0
    q = 1.0 - your_prob
    full_kelly = (b * your_prob - q) / b if b > 0 else 0.0
    fractional_kelly = max(full_kelly * fraction, 0.0)
    stake = bankroll * fractional_kelly

    return {
        "edge": b * your_prob - q,
        "full_kelly_fraction": full_kelly,
        "applied_kelly_fraction": fractional_kelly,
        "recommended_stake": stake,
        "bankroll": bankroll,
        "paper_mode": True,
        "note": "Paper mode — no real wager placed. Stake is for tracking only.",
    }


def explain_kelly(result: dict) -> str:
    if result["edge"] <= 0:
        return (
            f"No edge detected (Kelly = {result['full_kelly_fraction']*100:.1f}%). "
            "No stake recommended."
        )
    return (
        f"Estimated edge: {result['edge']*100:.1f}%. "
        f"Quarter-Kelly stake: {result['recommended_stake']:.2f} "
        f"({result['applied_kelly_fraction']*100:.2f}% of {result['bankroll']:.2f} bankroll). "
        f"[PAPER MODE — no real bet placed]"
    )
