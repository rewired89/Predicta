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


def vig_removed_prob_three_way(
    decimal_a: float, decimal_draw: float, decimal_b: float,
) -> tuple[float, float, float]:
    """
    Strip vig from a 3-way market (soccer 1X2). Returns (fair_a, fair_draw, fair_b)
    summing to 1.0. Applies proportional (multiplicative) devig — the standard
    sportsbook method, equivalent to normalising raw implieds.

    For power/Shin devig methods there are more academic alternatives, but
    proportional is what Pinnacle/Betfair converge to at low margins and is
    the sensible default when we don't know the book's specific weighting.
    """
    impl_a = 1.0 / decimal_a
    impl_d = 1.0 / decimal_draw
    impl_b = 1.0 / decimal_b
    total  = impl_a + impl_d + impl_b
    return (
        round(impl_a / total, 4),
        round(impl_d / total, 4),
        round(impl_b / total, 4),
    )


def market_edge_summary(
    model_prob_a: float,
    model_prob_b: float,
    decimal_a: float,
    decimal_b: float,
    decimal_draw: float | None = None,
    model_prob_draw: float | None = None,
) -> dict:
    """
    Compare model probabilities to market odds. Handles both 2-way markets
    (baseball / TT / DNB) and 3-way markets (soccer 1X2).

    3-way mode (decimal_draw provided):
      vig = 1/decimal_a + 1/decimal_draw + 1/decimal_b - 1     (always ≥ 0)
      market_implied_a/draw/b = proportional-devig fair probs
      edge_a = model_prob_a - fair_a           (model vs vig-free market)
      edge_b = model_prob_b - fair_b
      edge_draw = model_prob_draw - fair_draw  (when model draw supplied)

    2-way mode (no draw odds): unchanged legacy behaviour for baseball / TT.

    Round 5 fix: previously we did 2-way devig on 3-way soccer inputs, which
    computed vig from just home+away (missing the ~30% draw implied) and
    produced negative vig + garbage breakevens. Every soccer BET recommendation
    with sportsbook odds was mis-scored.

    Returns dict with has_real_odds, market_type ("2way"/"3way"), market_implied_*,
    breakeven_*, edge_*, vig, verdict_*.
    """
    def _verdict(edge: float) -> str:
        if edge >= EDGE_VALUE_THRESHOLD:  return "VALUE"
        if edge >= EDGE_SLIGHT_THRESHOLD: return "SLIGHT EDGE"
        if edge >= -EDGE_VALUE_THRESHOLD: return "FAIR"
        return "AVOID"

    if decimal_draw is not None:
        # 3-way (soccer)
        impl_a_raw = 1.0 / decimal_a
        impl_d_raw = 1.0 / decimal_draw
        impl_b_raw = 1.0 / decimal_b
        vig        = round(impl_a_raw + impl_d_raw + impl_b_raw - 1.0, 4)
        impl_a_nv, impl_d_nv, impl_b_nv = vig_removed_prob_three_way(
            decimal_a, decimal_draw, decimal_b
        )

        # Edge is measured against the vig-FREE fair prob, not the raw price.
        # (In 2-way mode below we compare against 1/decimal because there's
        # only one non-outcome to redistribute against.)
        edge_a = model_prob_a - impl_a_nv
        edge_b = model_prob_b - impl_b_nv
        edge_d = (model_prob_draw - impl_d_nv) if model_prob_draw is not None else None

        result = {
            "has_real_odds":       True,
            "market_type":         "3way",
            "market_implied_a":    impl_a_nv,
            "market_implied_draw": impl_d_nv,
            "market_implied_b":    impl_b_nv,
            "breakeven_a":         round(impl_a_raw, 4),
            "breakeven_draw":      round(impl_d_raw, 4),
            "breakeven_b":         round(impl_b_raw, 4),
            "edge_a":              round(edge_a, 4),
            "edge_b":              round(edge_b, 4),
            "vig":                 vig,
            "verdict_a":           _verdict(edge_a),
            "verdict_b":           _verdict(edge_b),
        }
        if edge_d is not None:
            result["edge_draw"]    = round(edge_d, 4)
            result["verdict_draw"] = _verdict(edge_d)
        return result

    # ── 2-way (baseball / TT / DNB) — unchanged legacy path ─────────────────
    impl_a_raw = 1.0 / decimal_a
    impl_b_raw = 1.0 / decimal_b
    vig        = round(impl_a_raw + impl_b_raw - 1.0, 4)
    impl_a_nv, impl_b_nv = vig_removed_prob(decimal_a, decimal_b)

    edge_a = model_prob_a - impl_a_raw
    edge_b = model_prob_b - impl_b_raw

    return {
        "has_real_odds":       True,
        "market_type":         "2way",
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
