"""
Baseball market calculations using a Poisson run model.

Expected runs formula:
  mu = LEAGUE_AVG_RUNS × (wRC+/100) × (opp_FIP / LEAGUE_AVG_FIP) × park_factor × home_boost

Lower opp FIP → smaller mu (better pitcher suppresses runs).
"""
from __future__ import annotations
import math
from typing import Optional

import numpy as np
from scipy.stats import poisson

LEAGUE_AVG_RUNS    = 4.5    # 2024 MLB average runs per team per game
LEAGUE_AVG_FIP     = 4.00   # 2024 MLB average FIP
LEAGUE_AVG_WRC_PLUS = 100.0
MAX_RUNS           = 20     # matrix dimension cap


def expected_runs(
    wrc_plus: float,
    opp_starter_fip: float,
    park_factor: float = 1.0,
    is_home: bool = False,
) -> float:
    """
    Returns expected runs scored for one team in a given game.
    wrc_plus   — batting team's season wRC+ (100 = league avg)
    opp_starter_fip — opposing starter's FIP (lower = better pitcher = fewer runs)
    """
    off_factor  = (wrc_plus or LEAGUE_AVG_WRC_PLUS) / 100.0
    fip_clamped = min(max(opp_starter_fip or LEAGUE_AVG_FIP, 1.5), 7.5)
    pitch_factor = fip_clamped / LEAGUE_AVG_FIP   # <1.0 for elite pitchers
    home_boost  = 1.03 if is_home else 1.0
    mu = LEAGUE_AVG_RUNS * off_factor * pitch_factor * park_factor * home_boost
    return max(1.5, min(mu, 10.0))


def build_run_matrix(mu_home: float, mu_away: float) -> np.ndarray:
    """Build joint probability matrix P[home_runs, away_runs] using independent Poisson."""
    n = MAX_RUNS + 1
    h = poisson.pmf(np.arange(n), mu_home)
    a = poisson.pmf(np.arange(n), mu_away)
    return np.outer(h, a)


def moneyline_market(matrix: np.ndarray) -> dict:
    """
    P(home wins), P(away wins) with no draw — tied games go to extras.
    Extra innings: home wins ~52% of the time.
    """
    p_home_reg  = float(np.sum(np.tril(matrix, -1)))
    p_away_reg  = float(np.sum(np.triu(matrix, 1)))
    p_extras    = float(np.trace(matrix))
    p_home = p_home_reg + p_extras * 0.52
    p_away = p_away_reg + p_extras * 0.48
    total  = p_home + p_away
    return {
        "p_home_win": round(p_home / total, 4),
        "p_away_win": round(p_away / total, 4),
    }


def run_line_market(matrix: np.ndarray, line: float = 1.5) -> list[dict]:
    """Run line market (typically ±1.5, occasionally ±2.5)."""
    n = matrix.shape[0]
    p_home_covers = p_away_covers = p_push = 0.0
    for i in range(n):
        for j in range(n):
            diff = i - j
            if diff > line:
                p_home_covers += matrix[i, j]
            elif diff < -line:
                p_away_covers += matrix[i, j]
            elif abs(abs(diff) - line) < 1e-6:
                p_push += matrix[i, j]
    return [{
        "line":           line,
        "label":          f"Home -{line} / Away +{line}",
        "p_home_covers":  round(float(p_home_covers), 4),
        "p_away_covers":  round(float(p_away_covers), 4),
        "p_push":         round(float(p_push), 4),
    }]


def total_market(matrix: np.ndarray, lines: list[float] | None = None) -> list[dict]:
    """Over/Under totals for multiple lines."""
    if lines is None:
        lines = [6.5, 7.5, 8.5, 9.5, 10.5]
    n = matrix.shape[0]
    results = []
    for line in lines:
        p_over = p_under = p_push = 0.0
        for i in range(n):
            for j in range(n):
                t = i + j
                if t > line:
                    p_over += matrix[i, j]
                elif t < line:
                    p_under += matrix[i, j]
                else:
                    p_push += matrix[i, j]
        results.append({
            "line":    line,
            "label":   f"O/U {line}",
            "p_over":  round(float(p_over), 4),
            "p_under": round(float(p_under), 4),
            "p_push":  round(float(p_push), 4),
        })
    return results


def first_five_market(mu_home: float, mu_away: float, fraction: float = 0.55) -> dict:
    """
    First 5 innings market — starters pitch ~5 IP, roughly 55% of total runs occur by then.
    Uses a reduced Poisson model (no bullpen blowups possible in this window).
    """
    mu_h5 = mu_home * fraction
    mu_a5 = mu_away * fraction
    n = 13  # cap matrix at 12 runs per team for first 5
    h = poisson.pmf(np.arange(n), mu_h5)
    a = poisson.pmf(np.arange(n), mu_a5)
    mat5 = np.outer(h, a)
    ml = moneyline_market(mat5)
    return {
        "mu_home_5":  round(mu_h5, 2),
        "mu_away_5":  round(mu_a5, 2),
        "p_home_win": ml["p_home_win"],
        "p_away_win": ml["p_away_win"],
        "note":       "Starter-driven — bullpen excluded",
    }


def nrfi_market(mu_home: float, mu_away: float) -> dict:
    """
    No Run First Inning market.
    Each team's 1st-inning run rate ≈ mu/9.
    """
    mu_h1 = mu_home / 9.0
    mu_a1 = mu_away / 9.0
    p_h_scores = float(1 - poisson.pmf(0, mu_h1))
    p_a_scores = float(1 - poisson.pmf(0, mu_a1))
    p_nrfi = (1 - p_h_scores) * (1 - p_a_scores)
    return {
        "p_nrfi": round(p_nrfi, 4),
        "p_yrfi": round(1 - p_nrfi, 4),
        "note":   "NRFI = No Run First Inning (Poisson 1st-inning approximation)",
    }


def team_total_market(mu: float, lines: list[float] | None = None) -> list[dict]:
    """Over/Under for a single team's run total."""
    if lines is None:
        lines = [3.5, 4.5, 5.5]
    results = []
    for line in lines:
        p_over  = float(1 - poisson.cdf(math.floor(line), mu))
        p_under = float(poisson.cdf(math.floor(line) - (0 if line != math.floor(line) else 1), mu))
        # For half-lines (3.5, 4.5 …) there's no push
        p_push  = max(0.0, 1.0 - p_over - p_under)
        results.append({
            "line":    line,
            "label":   f"O/U {line}",
            "p_over":  round(float(p_over), 4),
            "p_under": round(float(p_under), 4),
            "p_push":  round(float(p_push), 4),
        })
    return results


def compute_baseball_markets(
    mu_home: float,
    mu_away: float,
    run_line: float = 1.5,
    total_lines: list[float] | None = None,
) -> dict:
    """Orchestrate all baseball markets from expected run values."""
    matrix = build_run_matrix(mu_home, mu_away)
    return {
        "mu_home":         round(mu_home, 2),
        "mu_away":         round(mu_away, 2),
        "moneyline":       moneyline_market(matrix),
        "run_line":        run_line_market(matrix, run_line),
        "totals":          total_market(matrix, total_lines),
        "first_five":      first_five_market(mu_home, mu_away),
        "nrfi":            nrfi_market(mu_home, mu_away),
        "team_total_home": team_total_market(mu_home),
        "team_total_away": team_total_market(mu_away),
    }
