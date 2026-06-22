"""
Dixon-Coles Poisson model for soccer match outcomes.
Estimates attack/defense strengths from recent xG data and applies
the DC low-score correlation correction.
"""
from __future__ import annotations
import math
from typing import Optional
from scipy.stats import poisson  # type: ignore
import numpy as np


TAU = 0.1  # Dixon-Coles rho parameter (low-score correction strength)
HOME_ADVANTAGE = 1.15  # multiplicative factor on home attack
MAX_GOALS = 7  # score matrix dimension


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
