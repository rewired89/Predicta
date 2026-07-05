"""
Baseball market calculations using a split Poisson run model.

Full-game expected runs = F5 (innings 1-5, starter) + L4 (innings 6-9, bullpen):
  mu_f5 = LEAGUE_AVG × starter_frac × (wRC+/100) × (starter_FIP/LEAGUE_FIP) × park × home
  mu_l4 = LEAGUE_AVG × bullpen_frac × (wRC+/100) × (bullpen_FIP/LEAGUE_FIP) × park × home
  mu_total = mu_f5 + mu_l4

starter_frac = min(max(avg_ip, 3.0), 7.0) / 9.0  when avg_ip is known
             = 5/9 (default) otherwise

Platoon adjustment: RHB-heavy lineups gain ~5% wRC+ vs LHP starters.
"""
from __future__ import annotations
import math
from typing import Optional

import numpy as np
from scipy.stats import poisson

LEAGUE_AVG_RUNS     = 4.5    # 2024 MLB average runs per team per game
LEAGUE_AVG_FIP      = 4.00   # 2024 MLB average FIP (starters)
LEAGUE_BULLPEN_FIP  = 4.40   # MLB bullpens average slightly worse than starters
LEAGUE_AVG_WRC_PLUS = 100.0
MAX_RUNS            = 20     # matrix dimension cap

STARTER_FRAC = 5 / 9   # starters pitch ~5 of 9 innings
BULLPEN_FRAC = 4 / 9   # bullpen covers remaining ~4 innings

# Platoon: league lineup is ~65% RHB → slight boost when facing a LHP starter
PLATOON_VS_LHP = 1.05
PLATOON_VS_RHP = 1.00


def platoon_wrc_adjust(wrc_plus: float, pitcher_throws: str) -> float:
    """Scale a team's wRC+ for L/R handedness matchup against the opposing starter."""
    if pitcher_throws == "L":
        return wrc_plus * PLATOON_VS_LHP
    return wrc_plus * PLATOON_VS_RHP


LEAGUE_AVG_CSW_PCT    = 0.285   # 2024 MLB called strike + whiff rate
LEAGUE_AVG_FB_VELO    = 93.5    # 2024 MLB average 4-seam fastball velocity (mph)
LEAGUE_AVG_CHASE_PCT  = 0.295   # 2024 MLB O-Swing% (out-of-zone swing rate)
LEAGUE_AVG_BARREL_PCT = 0.075   # 2024 MLB barrel% against (batted-ball quality)


def pitcher_process_adjustment(
    csw_pct: Optional[float] = None,
    avg_fb_velo: Optional[float] = None,
    o_swing_pct: Optional[float] = None,
    barrel_pct_against: Optional[float] = None,
) -> float:
    """
    Convert pitch-level process metrics to a run-prevention multiplier for mu_f5.
    < 1.0 means the pitcher suppresses runs beyond what FIP/SIERA captures;
    > 1.0 means the opposite.

    Research basis:
      CSW%: ~0.73 R² with full-season K%; each 1pp above avg → ~1.7% fewer runs
      Velocity: each 1 mph above avg → ~1.0% fewer runs (PITCHf/x studies)
      O-Swing%: each 1pp above avg → ~1.2% fewer runs (chase = weak contact)
      Barrel%: ~0.85 R² with future ERA (Statcast; FIP misses "almost HRs");
        each 1pp above avg → ~2.5% more runs allowed

    Why barrel% matters even when SIERA is available:
      SIERA uses GB/FB rate and HR/FB rate, but barrel% is measured on contact
      quality rather than outcomes. A pitcher giving up 12% barrels will allow
      more hard-hit outs that don't show in SIERA yet, predicting future HR/ERA.

    Each signal capped ±8%; combined cap ±15%.
    Returns 1.0 when all inputs are None (no adjustment).

    Unit note (fixed 2026-07-05): barrel_pct_against always arrives here as a
    raw percent from the Savant feature store (e.g. 10.4 meaning 10.4%), never
    as a decimal fraction — the store is built via scripts/enrich_nrfi_savant,
    which is also what the trained NRFI XGBoost model was fit on, so that
    scale is correct for the NRFI model's own feature vector. But this
    function's LEAGUE_AVG_BARREL_PCT constant (0.075) is a decimal fraction,
    so a raw percent value fed straight in always blew past the ±0.08 cap —
    every pitcher, elite or poor, silently clamped to the exact same +8%
    run-inflation penalty, destroying the signal entirely. Convert here so
    csw_pct/o_swing_pct (already decimal-scale from the feature store's
    FanGraphs CSV import) don't need to change.
    """
    adj = 0.0
    if csw_pct is not None:
        adj += max(-0.08, min(0.08, (csw_pct - LEAGUE_AVG_CSW_PCT) * 1.70))
    if avg_fb_velo is not None:
        adj += max(-0.08, min(0.08, (avg_fb_velo - LEAGUE_AVG_FB_VELO) * 0.010))
    if o_swing_pct is not None:
        adj += max(-0.08, min(0.08, (o_swing_pct - LEAGUE_AVG_CHASE_PCT) * 1.20))
    if barrel_pct_against is not None:
        barrel_frac = barrel_pct_against / 100.0 if barrel_pct_against > 1.5 else barrel_pct_against
        # High barrel% → pitcher is worse than FIP suggests → positive adj → MORE runs
        adj -= max(-0.08, min(0.08, (barrel_frac - LEAGUE_AVG_BARREL_PCT) * 2.50))
    return max(0.85, min(1.15, 1.0 - adj))


def expected_runs_split(
    wrc_plus: float,
    opp_starter_fip: float,
    opp_bullpen_fip: float,
    park_factor: float = 1.0,
    is_home: bool = False,
    opp_starter_avg_ip: Optional[float] = None,
    weather_factor: float = 1.0,
    off_rest_mult: float = 1.0,
    opp_starter_csw_pct: Optional[float] = None,
    opp_starter_fb_velo: Optional[float] = None,
    opp_starter_o_swing: Optional[float] = None,
    opp_starter_barrel_pct: Optional[float] = None,
) -> tuple[float, float]:
    """
    Returns (mu_f5, mu_l4): expected runs for innings 1-5 and 6-9.

    F5 uses the opposing starter's FIP (already replaced by SIERA via enrich_starter
    when FanGraphs data is available). L4 uses derived bullpen FIP.

    pitch-process signals (csw_pct, fb_velo, o_swing) feed pitcher_process_adjustment()
    which multiplies mu_f5 only — capturing what SIERA doesn't yet reflect
    (e.g. a pitcher with great CSW% but few innings this season).

    weather_factor: temp+wind combined (1.0 = neutral/dome), capped ±15%.
    off_rest_mult:  rest-day offense adj (0.99–1.01).
    """
    off  = (wrc_plus or LEAGUE_AVG_WRC_PLUS) / 100.0
    home = 1.03 if is_home else 1.0
    base = LEAGUE_AVG_RUNS * off * park_factor * home * weather_factor * off_rest_mult

    if opp_starter_avg_ip and opp_starter_avg_ip > 0:
        sf = min(max(opp_starter_avg_ip, 3.0), 7.0) / 9.0
        bf = 1.0 - sf
    else:
        sf = STARTER_FRAC
        bf = BULLPEN_FRAC

    starter_fip = min(max(opp_starter_fip  or LEAGUE_AVG_FIP,    1.5), 7.5)
    bullpen_fip = min(max(opp_bullpen_fip  or LEAGUE_BULLPEN_FIP, 1.5), 7.5)

    process_adj = pitcher_process_adjustment(
        csw_pct             = opp_starter_csw_pct,
        avg_fb_velo         = opp_starter_fb_velo,
        o_swing_pct         = opp_starter_o_swing,
        barrel_pct_against  = opp_starter_barrel_pct,
    )

    mu_f5 = base * sf * (starter_fip / LEAGUE_AVG_FIP) * process_adj
    mu_l4 = base * bf * (bullpen_fip / LEAGUE_AVG_FIP)

    return max(0.5, min(mu_f5, 8.0)), max(0.4, min(mu_l4, 7.0))


def expected_runs(
    wrc_plus: float,
    opp_starter_fip: float,
    park_factor: float = 1.0,
    is_home: bool = False,
    opp_bullpen_fip: Optional[float] = None,
    opp_starter_avg_ip: Optional[float] = None,
) -> float:
    """Total expected runs combining F5 (starter) and L4 (bullpen) windows."""
    bp = opp_bullpen_fip if opp_bullpen_fip is not None else LEAGUE_BULLPEN_FIP
    mu_f5, mu_l4 = expected_runs_split(wrc_plus, opp_starter_fip, bp, park_factor, is_home, opp_starter_avg_ip)
    return max(1.5, min(mu_f5 + mu_l4, 10.0))


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


def first_five_market(mu_home_f5: float, mu_away_f5: float) -> dict:
    """
    First 5 innings market using the starter-only expected run window.
    mu values are already scaled to ~5 innings via expected_runs_split.
    """
    n = 13  # cap matrix at 12 runs per team for first 5
    h = poisson.pmf(np.arange(n), mu_home_f5)
    a = poisson.pmf(np.arange(n), mu_away_f5)
    mat5 = np.outer(h, a)
    ml = moneyline_market(mat5)
    return {
        "mu_home_f5": round(mu_home_f5, 2),
        "mu_away_f5": round(mu_away_f5, 2),
        "p_home_win": ml["p_home_win"],
        "p_away_win": ml["p_away_win"],
        "note":       "Starter-driven — no bullpen variance",
    }


def last_four_market(mu_home_l4: float, mu_away_l4: float) -> dict:
    """Innings 6-9 market using the bullpen expected run window."""
    n = 11
    h = poisson.pmf(np.arange(n), mu_home_l4)
    a = poisson.pmf(np.arange(n), mu_away_l4)
    mat_l4 = np.outer(h, a)
    ml = moneyline_market(mat_l4)
    return {
        "mu_home_l4": round(mu_home_l4, 2),
        "mu_away_l4": round(mu_away_l4, 2),
        "p_home_win": ml["p_home_win"],
        "p_away_win": ml["p_away_win"],
        "note":       "Bullpen-driven — innings 6–9",
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
    mu_home_f5: float,
    mu_away_f5: float,
    mu_home_l4: float,
    mu_away_l4: float,
    run_line: float = 1.5,
    total_lines: list[float] | None = None,
) -> dict:
    """Orchestrate all baseball markets from split starter/bullpen expected run values."""
    matrix = build_run_matrix(mu_home, mu_away)
    return {
        "mu_home":         round(mu_home, 2),
        "mu_away":         round(mu_away, 2),
        "moneyline":       moneyline_market(matrix),
        "run_line":        run_line_market(matrix, run_line),
        "totals":          total_market(matrix, total_lines),
        "first_five":      first_five_market(mu_home_f5, mu_away_f5),
        "last_four":       last_four_market(mu_home_l4, mu_away_l4),
        "nrfi":            nrfi_market(mu_home, mu_away),
        "team_total_home": team_total_market(mu_home),
        "team_total_away": team_total_market(mu_away),
    }
