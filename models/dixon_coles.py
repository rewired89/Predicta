"""
Dixon-Coles Poisson model for soccer match outcomes.
Estimates attack/defense strengths from recent xG data and applies
the DC low-score correlation correction.

Two entry points:
  predict() — legacy single-strength input; used by engine.py for backward compat.
  predict_xg() — npxG + venue-split aware; used by analyze_soccer.py.
"""
from __future__ import annotations
import math
from typing import Optional
from scipy.stats import poisson  # type: ignore
import numpy as np


TAU = 0.1  # Dixon-Coles rho parameter (low-score correction strength)
HOME_ADVANTAGE = 1.15  # multiplicative factor on home attack (legacy default)
MAX_GOALS = 7  # score matrix dimension

# Recent vs season-long blend — last-5 carries 60% weight by default. Mark Dixon's
# original 1997 paper used exponential decay with ~2yr half-life; for in-season
# prediction a fixed last-5 weight tracks form changes well without over-fitting.
DEFAULT_RECENT_WEIGHT = 0.6

# Shrinkage strength when matches_played is small — pulls attack/defense toward
# league average. λ = matches / (matches + SHRINKAGE_K). At 10 games, ~50% raw.
SHRINKAGE_K = 10.0


def _dc_adjustment(goals_home: int, goals_away: int, mu_h: float, mu_a: float, tau: float) -> float:
    """Dixon-Coles correction factor for low-score scorelines."""
    if goals_home == 0 and goals_away == 0:
        return 1 - mu_h * mu_a * tau
    if goals_home == 1 and goals_away == 0:
        return 1 + mu_a * tau
    if goals_home == 0 and goals_away == 1:
        return 1 + mu_h * tau
    if goals_home == 1 and goals_away == 1:
        return 1 - tau
    return 1.0


def predict(
    attack_home: float,
    defense_home: float,
    attack_away: float,
    defense_away: float,
    league_avg_goals: float = 1.35,
    neutral: bool = False,
) -> dict:
    """
    Return {'prob_home', 'prob_draw', 'prob_away', 'mu_home', 'mu_away'}.

    attack/defense strength values are relative to league average (1.0 = average).
    """
    ha = 1.0 if neutral else HOME_ADVANTAGE
    mu_h = league_avg_goals * attack_home * defense_away * ha
    mu_a = league_avg_goals * attack_away * defense_home

    score_matrix = np.zeros((MAX_GOALS + 1, MAX_GOALS + 1))
    for g_h in range(MAX_GOALS + 1):
        for g_a in range(MAX_GOALS + 1):
            p = poisson.pmf(g_h, mu_h) * poisson.pmf(g_a, mu_a)
            adj = _dc_adjustment(g_h, g_a, mu_h, mu_a, TAU)
            score_matrix[g_h, g_a] = p * adj

    # Normalise (DC adjustment can shift total slightly)
    score_matrix /= score_matrix.sum()

    prob_home = float(np.tril(score_matrix, -1).sum())  # home goals > away
    prob_draw = float(np.trace(score_matrix))
    prob_away = float(np.triu(score_matrix, 1).sum())

    return {
        "prob_home": prob_home,
        "prob_draw": prob_draw,
        "prob_away": prob_away,
        "mu_home": mu_h,
        "mu_away": mu_a,
    }


def strengths_from_signals(signals: dict) -> dict:
    """
    Derive attack/defense multipliers from stored signal values.
    Falls back to 1.0 (league average) for missing signals.
    """
    xg_for = signals.get("xg_for_avg5", 1.35)
    xg_against = signals.get("xg_against_avg5", 1.35)
    league_avg = 1.35  # typical top-division average
    # attack > 1.0 = above-average scoring; defense > 1.0 = weak defense (concedes more)
    # mu_home = league_avg * attack_home * defense_away, so weak away defense inflates home goals
    attack = xg_for / league_avg if league_avg else 1.0
    defense = xg_against / league_avg if league_avg else 1.0
    return {"attack": max(attack, 0.1), "defense": max(defense, 0.1)}


def explain(result: dict, team_home: str, team_away: str) -> str:
    return (
        f"Dixon-Coles model: expected goals {team_home}={result['mu_home']:.2f}, "
        f"{team_away}={result['mu_away']:.2f}. "
        f"Outcome probabilities — {team_home}: {result['prob_home']*100:.1f}%, "
        f"Draw: {result['prob_draw']*100:.1f}%, "
        f"{team_away}: {result['prob_away']*100:.1f}%."
    )


# ── npxG-aware strengths (preferred path; used by analyze_soccer.py) ─────────

def _blend(season_val: Optional[float], recent_val: Optional[float],
           recent_weight: float = DEFAULT_RECENT_WEIGHT) -> Optional[float]:
    """Weighted average of season and recent values; falls back to whichever exists."""
    if season_val is None and recent_val is None:
        return None
    if recent_val is None:
        return season_val
    if season_val is None:
        return recent_val
    return recent_weight * recent_val + (1 - recent_weight) * season_val


def _shrink(value: float, league_mean: float, matches: float) -> float:
    """
    Bayesian-style shrinkage toward league mean. Early in season the raw rate
    is noisy; we pull it toward the prior with weight inversely proportional
    to matches played. At 38 matches, ~79% raw; at 5 matches, ~33% raw.

    Accepts float matches (Kish effective_n) so callers using time-decayed
    inputs can pass the right denominator for the variance estimate.
    """
    if matches <= 0:
        return league_mean
    w = matches / (matches + SHRINKAGE_K)
    return w * value + (1 - w) * league_mean


def strengths_from_xg(
    *,
    season_xg_for: Optional[float]  = None,
    season_xg_against: Optional[float] = None,
    recent_xg_for: Optional[float]   = None,
    recent_xg_against: Optional[float] = None,
    venue_xg_for: Optional[float]    = None,    # home_* if is_home else away_*
    venue_xg_against: Optional[float] = None,
    league_avg_goals: float          = 1.40,
    matches_played: int              = 19,
    venue_matches: int               = 9,
    effective_n: Optional[float]     = None,    # Kish n_eff from time-decay
    goal_overperform: float          = 1.0,     # G/xG ratio
    is_home: bool                    = True,
    recent_weight: float             = DEFAULT_RECENT_WEIGHT,
    venue_weight: float              = 0.45,
) -> dict:
    """
    Attack/defense multipliers built from npxG-style inputs.

    Layering (each step is optional — falls back when input is None):
      1. Blend season vs recent (time-decayed) xG (60/40 default).
      2. Blend venue-specific (home or away) vs overall, scaled by sample size
         using a 6-match ramp (was 8 — Kimi #g).
      3. Apply Bayesian shrinkage to league mean using sample sizes.
      4. Damp continuously by goal_overperform (Kimi #8):
            damp = 1 + clamp((1 - overperform) × 0.15, -0.05, +0.05)
         At overperform=1.33 → 0.95; at 0.67 → 1.05; at 1.0 → 1.0. Replaces
         the previous binary step at 0.85 / 1.15 — no discontinuities.

    Returns {"attack", "defense", "components"} where attack/defense are >1.0 for
    above-average performance.
    """
    base = league_avg_goals if league_avg_goals > 0 else 1.40

    # Step 1: time blend
    xg_for_blend = _blend(season_xg_for, recent_xg_for, recent_weight)
    xg_ag_blend  = _blend(season_xg_against, recent_xg_against, recent_weight)

    # Step 2: venue blend — 6-match ramp instead of 8 (Kimi #g)
    def _venue_blend(overall: Optional[float], venue: Optional[float],
                     venue_n: int, total_n: int) -> Optional[float]:
        if venue is None:
            return overall
        if overall is None:
            return venue
        eff_w = venue_weight * min(1.0, venue_n / 6.0)
        return eff_w * venue + (1 - eff_w) * overall

    xg_for_final = _venue_blend(xg_for_blend, venue_xg_for, venue_matches, matches_played)
    xg_ag_final  = _venue_blend(xg_ag_blend,  venue_xg_against, venue_matches, matches_played)

    # Step 3: shrinkage to league mean — uses Kish effective_n when callers
    # supply time-decayed inputs (Kimi #2). Falls back to integer match count.
    if xg_for_final is None:
        xg_for_final = base
    if xg_ag_final is None:
        xg_ag_final = base
    shrink_n = effective_n if (effective_n is not None and effective_n > 0) else float(matches_played)
    xg_for_final = _shrink(xg_for_final, base, shrink_n)
    xg_ag_final  = _shrink(xg_ag_final,  base, shrink_n)

    # Step 4: continuous overperformance damping (Kimi #8)
    raw_damp = (1.0 - goal_overperform) * 0.15
    damp = 1.0 + max(-0.05, min(0.05, raw_damp))

    attack  = max(0.3, min(2.5, (xg_for_final * damp) / base))
    defense = max(0.3, min(2.5, xg_ag_final / base))

    return {
        "attack":  attack,
        "defense": defense,
        "components": {
            "xg_for_blend":  xg_for_blend,
            "xg_ag_blend":   xg_ag_blend,
            "xg_for_final":  round(xg_for_final, 3),
            "xg_ag_final":   round(xg_ag_final, 3),
            "league_avg":    base,
            "damping":       round(damp, 4),
            "is_home":       is_home,
        },
    }


def predict_xg(
    *,
    home_attack: float,
    home_defense: float,
    away_attack: float,
    away_defense: float,
    league_avg_goals: float = 1.40,
    home_advantage: float   = HOME_ADVANTAGE,
    neutral: bool           = False,
    keeper_adj_home: float  = 0.0,   # PSxG-GA per 90; positive = good GK → suppress opp μ
    keeper_adj_away: float  = 0.0,
    set_piece_share_home: float = 0.22,
    set_piece_share_away: float = 0.22,
    set_piece_aerial_mult_home: float = 1.0,  # away aerial advantage → home concedes more set pieces
    set_piece_aerial_mult_away: float = 1.0,
    max_goals: int          = MAX_GOALS,
    tau: float              = TAU,
) -> dict:
    """
    Goal-expectation model splitting μ into open-play and set-piece terms,
    then Poisson-summing back into a score matrix.

      μ_home = μ_open_h + μ_set_h
      μ_open_h = league_avg * home_attack * away_defense * (1 - set_piece_share_home) * ha
      μ_set_h  = league_avg * home_attack * away_defense * set_piece_share_home * aerial_mult * ha
      keeper adjustment: opposing GK PSxG-GA reduces μ by ~7% per (+0.5 per-90).

    Returns same keys as predict() plus mu_open / mu_set decomposition.
    """
    ha = 1.0 if neutral else home_advantage

    mu_h_raw = league_avg_goals * home_attack * away_defense * ha
    mu_a_raw = league_avg_goals * away_attack * home_defense

    # Split into open-play vs set-piece
    mu_h_open = mu_h_raw * (1 - set_piece_share_home)
    mu_h_set  = mu_h_raw * set_piece_share_home * set_piece_aerial_mult_home
    mu_a_open = mu_a_raw * (1 - set_piece_share_away)
    mu_a_set  = mu_a_raw * set_piece_share_away * set_piece_aerial_mult_away

    # Tiered keeper adjustment (Kimi #f): GK quality matters more for open-play
    # shots than for set-piece goals, which are often headed in from close range
    # past a planted keeper.
    def _gk_mult_open(adj: float) -> float:
        return max(0.88, min(1.15, 1.0 - 0.12 * adj))

    def _gk_mult_set(adj: float) -> float:
        return max(0.94, min(1.08, 1.0 - 0.06 * adj))

    # Home's μ is suppressed by AWAY's keeper, and vice versa.
    mu_h_open_adj = mu_h_open * _gk_mult_open(keeper_adj_away)
    mu_h_set_adj  = mu_h_set  * _gk_mult_set(keeper_adj_away)
    mu_a_open_adj = mu_a_open * _gk_mult_open(keeper_adj_home)
    mu_a_set_adj  = mu_a_set  * _gk_mult_set(keeper_adj_home)

    mu_h_total = mu_h_open_adj + mu_h_set_adj
    mu_a_total = mu_a_open_adj + mu_a_set_adj
    # Preserve adjusted open/set for the return dict
    mu_h_open, mu_h_set = mu_h_open_adj, mu_h_set_adj
    mu_a_open, mu_a_set = mu_a_open_adj, mu_a_set_adj

    matrix = np.zeros((max_goals + 1, max_goals + 1))
    for g_h in range(max_goals + 1):
        for g_a in range(max_goals + 1):
            p = poisson.pmf(g_h, mu_h_total) * poisson.pmf(g_a, mu_a_total)
            adj = _dc_adjustment(g_h, g_a, mu_h_total, mu_a_total, tau)
            matrix[g_h, g_a] = p * adj
    matrix /= matrix.sum()

    prob_home = float(np.tril(matrix, -1).sum())
    prob_draw = float(np.trace(matrix))
    prob_away = float(np.triu(matrix, 1).sum())

    return {
        "prob_home":  prob_home,
        "prob_draw":  prob_draw,
        "prob_away":  prob_away,
        "mu_home":    mu_h_total,
        "mu_away":    mu_a_total,
        "mu_home_open": mu_h_open,
        "mu_home_set":  mu_h_set,
        "mu_away_open": mu_a_open,
        "mu_away_set":  mu_a_set,
        "score_matrix": matrix,
    }
