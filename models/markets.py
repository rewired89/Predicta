"""
Derive sportsbook-style market probabilities from the Dixon-Coles score matrix.

All functions take the raw score_matrix (numpy array [home_goals, away_goals])
produced by dixon_coles._build_matrix(), plus mu_home / mu_away.

Markets covered (matching what's visible in typical sportsbook apps):
  - match_result_2up      : win by 2+ goals (each side)
  - correct_score         : top N most likely exact scorelines
  - spread                : cover a given handicap line
  - winner_push_if_tied   : 2-way (draw = push/void)
  - next_shot_on_target   : which team gets next SOT (xG-ratio based)
  - method_of_goal        : foot / header / penalty for a given goal number
"""
from __future__ import annotations
import math
from typing import Optional
import numpy as np


# ── Build raw matrix (extracted from dixon_coles.predict so we can reuse it) ──

def build_score_matrix(
    attack_home: float,
    defense_home: float,
    attack_away: float,
    defense_away: float,
    league_avg_goals: float = 1.35,
    neutral: bool = False,
    max_goals: int = 7,
    tau: float = 0.1,
) -> tuple:
    """Return (matrix, mu_home, mu_away). Matrix[i,j] = P(home scores i, away scores j)."""
    from scipy.stats import poisson

    ha = 1.0 if neutral else 1.15
    mu_h = league_avg_goals * attack_home * defense_away * ha
    mu_a = league_avg_goals * attack_away * defense_home

    matrix = np.zeros((max_goals + 1, max_goals + 1))
    for g_h in range(max_goals + 1):
        for g_a in range(max_goals + 1):
            p = poisson.pmf(g_h, mu_h) * poisson.pmf(g_a, mu_a)
            # DC low-score adjustment
            if g_h == 0 and g_a == 0:
                adj = 1 - mu_h * mu_a * tau
            elif g_h == 1 and g_a == 0:
                adj = 1 + mu_a * tau
            elif g_h == 0 and g_a == 1:
                adj = 1 + mu_h * tau
            elif g_h == 1 and g_a == 1:
                adj = 1 - tau
            else:
                adj = 1.0
            matrix[g_h, g_a] = p * adj

    matrix /= matrix.sum()
    return matrix, mu_h, mu_a


# ── Individual market calculators ─────────────────────────────────────────────

def match_result_2up(matrix: np.ndarray) -> dict:
    """
    'Match Result – 2 Up': team wins by 2+ goals.
    Common market: bet on a team, wins if they're 2+ goals ahead.
    """
    n = matrix.shape[0]
    p_home_2up = float(sum(
        matrix[h, a] for h in range(n) for a in range(n) if h - a >= 2
    ))
    p_away_2up = float(sum(
        matrix[h, a] for h in range(n) for a in range(n) if a - h >= 2
    ))
    p_close = 1.0 - p_home_2up - p_away_2up  # win by 1 or draw
    return {
        "home_win_2up": p_home_2up,
        "away_win_2up": p_away_2up,
        "not_2up": p_close,
    }


def correct_score(matrix: np.ndarray, top_n: int = 8) -> list[dict]:
    """
    Most likely exact scorelines ranked by probability.
    Returns list of {score_home, score_away, prob, label}.
    """
    n = matrix.shape[0]
    scores = []
    for h in range(n):
        for a in range(n):
            scores.append({
                "score_home": h,
                "score_away": a,
                "label": f"{h}–{a}",
                "prob": float(matrix[h, a]),
            })
    scores.sort(key=lambda x: -x["prob"])
    return scores[:top_n]


def spread(matrix: np.ndarray, lines: list[float] = None) -> list[dict]:
    """
    Asian handicap / spread for the home team.
    line = -1.5 means home team gives 1.5 goals; home covers if they win by 2+.
    Returns P(home covers) for each line.
    """
    if lines is None:
        lines = [-2.5, -1.5, -0.5, 0.5, 1.5, 2.5]
    n = matrix.shape[0]
    results = []
    for line in lines:
        p_cover = 0.0
        p_push = 0.0
        for h in range(n):
            for a in range(n):
                margin = h - a  # home margin
                adj_margin = margin + line  # adjusted by handicap
                if abs(adj_margin) < 0.01:
                    p_push += float(matrix[h, a])
                elif adj_margin > 0:
                    p_cover += float(matrix[h, a])
        results.append({
            "line": line,
            "label": f"Home {'+' if line > 0 else ''}{line}",
            "p_home_covers": p_cover,
            "p_push": p_push,
            "p_away_covers": 1.0 - p_cover - p_push,
        })
    return results


def winner_push_if_tied(matrix: np.ndarray) -> dict:
    """
    2-way market: draw is a push (void/refund).
    Returns fair probabilities for home and away, conditional on decisive result.
    """
    n = matrix.shape[0]
    p_home = float(np.tril(matrix, -1).sum())
    p_away = float(np.triu(matrix, 1).sum())
    p_draw = float(np.trace(matrix))
    decisive = p_home + p_away
    return {
        "p_home_win": p_home,
        "p_away_win": p_away,
        "p_draw": p_draw,
        "p_home_no_draw": p_home / decisive if decisive else 0.5,
        "p_away_no_draw": p_away / decisive if decisive else 0.5,
    }


def next_shot_on_target(mu_home: float, mu_away: float) -> dict:
    """
    Approximates 'next shot on target' probability via xG ratio.
    SOT rate roughly tracks xG pace. This is a first-order estimate only.
    """
    total = mu_home + mu_away
    if total == 0:
        return {"p_home_next_sot": 0.5, "p_away_next_sot": 0.5}
    return {
        "p_home_next_sot": mu_home / total,
        "p_away_next_sot": mu_away / total,
        "note": "Estimated from xG pace ratio — not a direct SOT model",
    }


# League-average goal-method distribution (top European football)
_GOAL_METHODS = {
    "foot":    0.69,
    "header":  0.22,
    "penalty": 0.09,
}


def method_of_goal(
    goal_number: int = 2,
    home_aerial_index: float = 1.0,
    away_aerial_index: float = 1.0,
    attacker_is_home: Optional[bool] = None,
) -> dict:
    """
    Probability distribution for the method of a specific goal (foot/header/penalty).

    home_aerial_index / away_aerial_index: relative header threat (1.0 = league avg).
    attacker_is_home: if known, adjust header probability for that team's style.
    Returns method probabilities for the team expected to score that goal.
    """
    base = dict(_GOAL_METHODS)

    if attacker_is_home is not None:
        idx = home_aerial_index if attacker_is_home else away_aerial_index
        # Scale header probability by aerial index, renormalise
        base["header"] = base["header"] * idx
        total = sum(base.values())
        base = {k: v / total for k, v in base.items()}

    return {
        "goal_number": goal_number,
        "foot":    base["foot"],
        "header":  base["header"],
        "penalty": base["penalty"],
        "note": "League-average distribution; adjust with team aerial_index signal if available.",
    }


# ── Full market bundle ────────────────────────────────────────────────────────

def compute_all_markets(
    attack_home: float,
    defense_home: float,
    attack_away: float,
    defense_away: float,
    league_avg_goals: float = 1.35,
    neutral: bool = False,
    home_aerial_index: float = 1.0,
    away_aerial_index: float = 1.0,
) -> dict:
    """Compute every market and return as a single dict."""
    matrix, mu_h, mu_a = build_score_matrix(
        attack_home, defense_home, attack_away, defense_away,
        league_avg_goals, neutral
    )
    return {
        "mu_home": mu_h,
        "mu_away": mu_a,
        "match_result_2up": match_result_2up(matrix),
        "correct_score": correct_score(matrix),
        "spread": spread(matrix),
        "winner_push_if_tied": winner_push_if_tied(matrix),
        "next_shot_on_target": next_shot_on_target(mu_h, mu_a),
        "method_of_goal_2": method_of_goal(2, home_aerial_index, away_aerial_index),
    }
