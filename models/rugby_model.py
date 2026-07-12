"""
Negative-Binomial score model for NRL (Rugby League) match outcomes.

Rugby scoring (tries=4, conversions=2, penalties=2, drop goals=1 in league)
has materially higher score variance than soccer goals — a plain Poisson
would understate that variance (Poisson forces variance == mean). This model
uses a Negative Binomial with a fixed overdispersion parameter instead, and
skips the Dixon-Coles low-score correlation correction entirely: that
correction exists to fix soccer's specific 0-0/1-0/1-1 clustering problem,
which has no rugby analogue (rugby essentially never finishes 0-0 or 1-0).

All constants below (LEAGUE_AVG_POINTS, HOME_ADVANTAGE, NB_DISPERSION_K) are
provisional starting estimates, not fitted to real NRL history — this repo's
build/test environment cannot reach ESPN (see fetchers/rugby.py docstring),
so there was no historical score data available to calibrate against in this
session. Treat these as an "Open Calibration Issue" (per CLAUDE.md convention)
until backtested against a season of real results.
"""
from __future__ import annotations
from typing import Optional
from scipy.stats import nbinom
import numpy as np

LEAGUE_AVG_POINTS = 22.0   # provisional NRL team points/game — recalibrate from live standings
HOME_ADVANTAGE     = 1.12  # provisional — NRL home win rate is historically ~55-58%
NB_DISPERSION_K     = 8.0  # provisional overdispersion; variance = mu + mu^2/k
MAX_POINTS          = 60
SHRINKAGE_K         = 8.0  # NRL regular season is ~24 rounds — fewer games than soccer's 38
DEFAULT_RECENT_WEIGHT = 0.6
MODEL_PROB_CAP      = 0.78  # provisional safety cap — mirrors the baseball 72% cap culture
                             # in this repo (Bug 3 fix); rugby blowouts run bigger than
                             # baseball's so the cap is a little looser, still bounded.


def _blend(season_val: Optional[float], recent_val: Optional[float],
           recent_weight: float = DEFAULT_RECENT_WEIGHT) -> Optional[float]:
    if season_val is None and recent_val is None:
        return None
    if recent_val is None:
        return season_val
    if season_val is None:
        return recent_val
    return recent_weight * recent_val + (1 - recent_weight) * season_val


def _shrink(value: float, league_mean: float, matches: float) -> float:
    if matches <= 0:
        return league_mean
    w = matches / (matches + SHRINKAGE_K)
    return w * value + (1 - w) * league_mean


def strengths_from_points(
    *,
    season_pf: Optional[float] = None,
    season_pa: Optional[float] = None,
    recent_pf: Optional[float] = None,
    recent_pa: Optional[float] = None,
    venue_pf: Optional[float] = None,   # home_ppg_for if is_home else away_ppg_for
    venue_pa: Optional[float] = None,
    league_avg_points: float = LEAGUE_AVG_POINTS,
    matches_played: int = 0,
    venue_matches: int = 0,
    effective_n: Optional[float] = None,
    is_home: bool = True,
    recent_weight: float = DEFAULT_RECENT_WEIGHT,
    venue_weight: float = 0.40,
) -> dict:
    """
    Attack/defense multipliers from points-for/against, mirroring
    models/dixon_coles.strengths_from_xg's layering:
      1. blend season vs recent (last-5) points
      2. blend venue split vs overall, ramped by venue sample size
      3. shrink to league mean by sample size
    Returns {"attack", "defense", "components"}; attack/defense are >1.0 for
    above-average scoring/leaking respectively (1.0 = league average).
    """
    base = league_avg_points if league_avg_points > 0 else LEAGUE_AVG_POINTS

    pf_blend = _blend(season_pf, recent_pf, recent_weight)
    pa_blend = _blend(season_pa, recent_pa, recent_weight)

    def _venue_blend(overall, venue, venue_n, total_n):
        if venue is None:
            return overall
        if overall is None:
            return venue
        eff_w = venue_weight * min(1.0, venue_n / 6.0)
        return eff_w * venue + (1 - eff_w) * overall

    pf_final = _venue_blend(pf_blend, venue_pf, venue_matches, matches_played)
    pa_final = _venue_blend(pa_blend, venue_pa, venue_matches, matches_played)

    if pf_final is None:
        pf_final = base
    if pa_final is None:
        pa_final = base

    shrink_n = effective_n if (effective_n is not None and effective_n > 0) else float(matches_played)
    pf_final = _shrink(pf_final, base, shrink_n)
    pa_final = _shrink(pa_final, base, shrink_n)

    attack  = max(0.3, min(2.5, pf_final / base))
    defense = max(0.3, min(2.5, pa_final / base))

    return {
        "attack": attack,
        "defense": defense,
        "components": {
            "pf_blend": pf_blend, "pa_blend": pa_blend,
            "pf_final": round(pf_final, 2), "pa_final": round(pa_final, 2),
            "league_avg": base, "is_home": is_home,
        },
    }


def _nb_pmf_vector(mu: float, k: float, max_points: int) -> np.ndarray:
    """P(score = 0..max_points) for a Negative Binomial with mean mu, dispersion k."""
    mu = max(mu, 0.5)
    p = k / (k + mu)
    return nbinom.pmf(np.arange(max_points + 1), k, p)


def predict_score(
    *,
    home_attack: float,
    home_defense: float,
    away_attack: float,
    away_defense: float,
    league_avg_points: float = LEAGUE_AVG_POINTS,
    home_advantage: float = HOME_ADVANTAGE,
    neutral: bool = False,
    dispersion_k: float = NB_DISPERSION_K,
    max_points: int = MAX_POINTS,
) -> dict:
    """
    Return {prob_home, prob_draw, prob_away, mu_home, mu_away, score_matrix}.
    score_matrix[i, j] = P(home scores i, away scores j) — independent NB
    marginals (no low-score correlation correction; see module docstring).
    """
    ha = 1.0 if neutral else home_advantage
    mu_h = league_avg_points * home_attack * away_defense * ha
    mu_a = league_avg_points * away_attack * home_defense

    pmf_h = _nb_pmf_vector(mu_h, dispersion_k, max_points)
    pmf_a = _nb_pmf_vector(mu_a, dispersion_k, max_points)
    matrix = np.outer(pmf_h, pmf_a)
    matrix /= matrix.sum()

    prob_home = float(np.tril(matrix, -1).sum())
    prob_draw = float(np.trace(matrix))
    prob_away = float(np.triu(matrix, 1).sum())

    return {
        "prob_home": prob_home,
        "prob_draw": prob_draw,
        "prob_away": prob_away,
        "mu_home": mu_h,
        "mu_away": mu_a,
        "score_matrix": matrix,
    }


def margin_buckets(matrix: np.ndarray) -> dict:
    """
    Victory-margin breakdown: P(home wins by 1-12), P(home wins 13+),
    P(away wins by 1-12), P(away wins 13+), P(draw). 12 points ≈ two
    converted tries — a common NRL handicap-line neighborhood.
    """
    n = matrix.shape[0]
    home_1_12 = home_13p = away_1_12 = away_13p = 0.0
    for i in range(n):
        for j in range(n):
            diff = i - j
            p = matrix[i, j]
            if diff == 0:
                continue
            if diff > 0:
                if diff <= 12:
                    home_1_12 += p
                else:
                    home_13p += p
            else:
                if diff >= -12:
                    away_1_12 += p
                else:
                    away_13p += p
    return {
        "home_by_1_12": home_1_12, "home_by_13_plus": home_13p,
        "away_by_1_12": away_1_12, "away_by_13_plus": away_13p,
    }


def totals_over_under(matrix: np.ndarray, line: float) -> dict:
    """P(total points over/under a given line), e.g. line=42.5."""
    n = matrix.shape[0]
    over = 0.0
    for i in range(n):
        for j in range(n):
            if (i + j) > line:
                over += matrix[i, j]
    return {"line": line, "prob_over": over, "prob_under": 1.0 - over}


def explain(result: dict, team_home: str, team_away: str) -> str:
    return (
        f"NRL Negative-Binomial model: expected points {team_home}={result['mu_home']:.1f}, "
        f"{team_away}={result['mu_away']:.1f}. "
        f"Win probability — {team_home}: {result['prob_home']*100:.1f}%, "
        f"{team_away}: {result['prob_away']*100:.1f}% "
        f"(draw {result['prob_draw']*100:.1f}% — golden-point extra time makes NRL draws rare)."
    )
