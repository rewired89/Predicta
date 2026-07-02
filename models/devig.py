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


def nrfi_clv(
    bet_side: str,
    entry_nrfi_dec: float,
    entry_yrfi_dec: float,
    close_nrfi_dec: float,
    close_yrfi_dec: float,
) -> dict:
    """
    Closing Line Value for a 2-way NRFI/YRFI bet, measured the sharp way:
    the vig-free implied probability of the side we bet at the CLOSING line
    minus that same side's vig-free implied probability at our ENTRY line.

    Positive CLV = the market moved toward our side after we bet it = we beat
    the close. This is the metric syndicates trust as proof of edge because it
    accumulates every game (no need to wait for the result).

    bet_side: "NRFI" or "YRFI".
    Returns {clv_pp, entry_fair, close_fair, beat_close} where *_fair are the
    vig-free probabilities (0-1) of the bet side, and clv_pp is in percentage
    points (close_fair - entry_fair) * 100.
    """
    side = "nrfi" if bet_side.upper() == "NRFI" else "yrfi"
    entry_fair = devig_market({"nrfi": entry_nrfi_dec, "yrfi": entry_yrfi_dec})
    close_fair = devig_market({"nrfi": close_nrfi_dec, "yrfi": close_yrfi_dec})
    e = entry_fair.get(side, 0.0)
    c = close_fair.get(side, 0.0)
    clv_pp = round((c - e) * 100, 2)
    return {
        "clv_pp":     clv_pp,
        "entry_fair": round(e, 4),
        "close_fair": round(c, 4),
        "beat_close": 1 if clv_pp > 0 else 0,
    }
