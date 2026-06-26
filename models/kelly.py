"""
Kelly Criterion staking calculator — paper mode only, computes stake size.
Never places real bets; outputs recommended stake for tracking purposes.

Also provides market-comparison utilities:
  american_to_decimal  — convert American odds to decimal
  vig_removed_prob     — strip the bookmaker's vig to get true implied probs
  market_edge_summary  — full edge report: model vs market, verdict, vig
"""
from __future__ import annotations


KELLY_FRACTION = 0.25  # quarter-Kelly

# Minimum edge (vs raw market implied, before vig) to label a bet "VALUE"
EDGE_VALUE_THRESHOLD  = 0.030   # +3pp
EDGE_SLIGHT_THRESHOLD = 0.005   # +0.5pp


def american_to_decimal(american: float) -> float:
    """Convert American odds to decimal. -130 → 1.769, +110 → 2.100"""
    if american >= 100:
        return round(american / 100 + 1, 4)
    else:
        return round(100 / abs(american) + 1, 4)


def vig_removed_prob(decimal_a: float, decimal_b: float) -> tuple[float, float]:
    """Strip the bookmaker vig; return true implied probabilities summing to 1."""
    impl_a = 1.0 / decimal_a
    impl_b = 1.0 / decimal_b
    total  = impl_a + impl_b
    return round(impl_a / total, 4), round(impl_b / total, 4)


def market_edge_summary(
    model_prob_a: float,
    model_prob_b: float,
    decimal_a: float,
    decimal_b: float,
) -> dict:
    """
    Compare model probabilities to market odds.

    edge_a = model_prob_a - (1/decimal_a)   ← raw: beats the price you pay
    Positive edge means the model thinks this team wins more often than
    the market price requires to be profitable.

    Returns:
      has_real_odds        — True (caller should set False when using defaults)
      market_implied_a/b   — vig-free implied probabilities
      breakeven_a/b        — raw implied (must exceed to profit)
      edge_a/b             — model minus breakeven (pp)
      vig                  — total overround (e.g. 0.045 = 4.5%)
      verdict_a/b          — "VALUE" / "SLIGHT EDGE" / "FAIR" / "AVOID"
    """
    impl_a_raw = 1.0 / decimal_a
    impl_b_raw = 1.0 / decimal_b
    vig        = round(impl_a_raw + impl_b_raw - 1.0, 4)
    impl_a_nv, impl_b_nv = vig_removed_prob(decimal_a, decimal_b)

    edge_a = model_prob_a - impl_a_raw
    edge_b = model_prob_b - impl_b_raw

    def _verdict(edge: float) -> str:
        if edge >= EDGE_VALUE_THRESHOLD:  return "VALUE"
        if edge >= EDGE_SLIGHT_THRESHOLD: return "SLIGHT EDGE"
        if edge >= -EDGE_VALUE_THRESHOLD: return "FAIR"
        return "AVOID"

    return {
        "has_real_odds":       True,
        "market_implied_a":    impl_a_nv,
        "market_implied_b":    impl_b_nv,
        "breakeven_a":         round(impl_a_raw, 4),
        "breakeven_b":         round(impl_b_raw, 4),
        "edge_a":              round(edge_a, 4),
        "edge_b":              round(edge_b, 4),
        "vig":                 vig,
        "verdict_a":           _verdict(edge_a),
        "verdict_b":           _verdict(edge_b),
    }


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
