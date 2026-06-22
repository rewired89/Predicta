"""
De-vig utilities: convert raw bookmaker odds to fair probabilities.
"""
from __future__ import annotations


def american_to_decimal(american: float) -> float:
    if american > 0:
        return american / 100 + 1
    return 100 / abs(american) + 1


def decimal_to_implied(decimal: float) -> float:
    if decimal <= 0:
        raise ValueError("Decimal odds must be positive")
    return 1.0 / decimal


def devig_market(prices: dict) -> dict:
    """
    Remove the vig from a market.

    prices: dict with keys 'a', 'b', and optionally 'draw'.
            Values are decimal odds (e.g. 2.10).
    Returns: dict of fair (vig-free) probabilities with same keys.
    """
    implied = {k: decimal_to_implied(v) for k, v in prices.items() if v is not None}
    total = sum(implied.values())
    return {k: v / total for k, v in implied.items()}


def clv(
    your_implied_prob: float, closing_prices: dict, outcome_key: str = "a"
) -> float:
    """
    Closing Line Value: your model prob vs vig-free closing line.
    Positive CLV means you had an edge over the closing market.
    """
    fair = devig_market(closing_prices)
    return your_implied_prob - fair.get(outcome_key, 0.0)
