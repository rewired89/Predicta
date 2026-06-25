#!/usr/bin/env python3
"""
Validate tennis logistic scale parameter against ATP hold-rate benchmarks.

The model's p_serve = P(A wins service POINT). This feeds into a Markov chain
that computes P(hold service game). We compare model-implied hold rates to
published ATP surface averages to sanity-check the scale parameter.

CALIBRATION NOTE — relative vs absolute:
  SQI is centred at 100 = tour average. For equal players (SQI_a = RQI_b),
  p_serve = 0.5, not the ATP average of ~0.65. The model captures RELATIVE
  quality differences, not absolute service win rates.

  Do NOT compare absolute hold% to ATP stats — they will always look low.
  Instead validate the GAP between elite and qualifier hold rates:
    ATP reference (hard court):
      elite vs qualifier gap: ~20-25pp
      Top-10 hold%: ~86-90%,  Qualifier hold%: ~65-72%
  A realistic model should reproduce the ~20-25pp gap even if the absolute
  values are shifted downward by the relative-centering effect.

To populate with real data:
  1. Go to https://www.tennisabstract.com/charting/ or
     https://www.ultimatetennisstatistics.com/
  2. Find 5-10 ATP matches with full serve/return stats
  3. Compute SQI/RQI using the formulas at the bottom of this file
  4. Fill TEST_MATCHES, then call find_best_scale(TEST_MATCHES)

Usage:
  python tests/validate_tennis_scale.py
"""
from __future__ import annotations
import math
from dataclasses import dataclass


SURFACE_AMP: dict[str, float] = {"grass": 1.15, "hard": 1.00, "clay": 0.88}


def _logistic(x: float, scale: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x / scale))
    except OverflowError:
        return 1.0 if x > 0 else 0.0


def hold_prob(p: float) -> float:
    """Exact P(hold service game) from P(win service point), Markov game formula."""
    q = 1.0 - p
    p2, q2 = p * p, q * q
    # P(4-0) + P(4-1) + P(4-2) + P(reach deuce then win)
    return p**4 + 4*p**4*q + 10*p**4*q**2 + 20*p**3*q**3 * p2 / (p2 + q2)


def p_serve_model(sqi_a: float, rqi_b: float, surface: str, scale: float) -> float:
    amp = SURFACE_AMP.get(surface, 1.0)
    return _logistic((sqi_a - rqi_b) * amp, scale)


# ---------------------------------------------------------------------------
# Section 1 — Sanity-check across scales
# ---------------------------------------------------------------------------

def run_scale_summary(scales: list[float]) -> None:
    """Print hold-rate gap between elite server and qualifier for each scale."""
    print("\n=== Hold-rate gap: elite (SQI=130, RQI=95) vs qualifier (SQI=88, RQI=120), hard ===")
    print(f"  {'Scale':>5} | {'Elite hold%':>12} | {'Qual hold%':>11} | {'Gap (pp)':>10}")
    print("  " + "-" * 47)
    for s in scales:
        p_e = p_serve_model(130, 95,  "hard", s)
        p_q = p_serve_model(88,  120, "hard", s)
        h_e = hold_prob(p_e) * 100
        h_q = hold_prob(p_q) * 100
        gap = h_e - h_q
        print(f"  {s:>5.0f} | {h_e:>11.1f}% | {h_q:>10.1f}% | {gap:>9.1f}pp")
    print("\n  ATP reference: elite ~88-90%, qualifier ~65-72%, gap ~20-25pp")
    print("  Absolute values will appear lower (relative model); focus on gap column.")


def run_scenario_table(scale: float) -> None:
    print(f"\n--- Scenario table for scale={scale} ---")
    print(f"  {'Scenario':<40} {'p_serve':>8} {'hold%':>7}")
    print("  " + "-" * 58)
    cases = [
        # (label, SQI_server, RQI_receiver, surface)
        ("Equal players, hard",                100, 100, "hard"),
        ("Server +20 SQI, hard",               120, 100, "hard"),
        ("Server +40 SQI, hard",               140, 100, "hard"),
        ("Elite server (SQI=130) vs avg, hard", 130,  95, "hard"),
        ("Elite server (SQI=130) vs avg, grass",130,  95, "grass"),
        ("Elite server (SQI=130) vs avg, clay", 130,  95, "clay"),
        ("Qualifier vs elite returner, hard",    88, 120, "hard"),
        ("Avg server vs elite returner, hard",  100, 120, "hard"),
    ]
    for label, sqi_a, rqi_b, surface in cases:
        p = p_serve_model(sqi_a, rqi_b, surface, scale)
        h = hold_prob(p) * 100
        print(f"  {label:<40} {p:>8.3f} {h:>6.1f}%")


# ---------------------------------------------------------------------------
# Section 2 — Validation against real match data
# ---------------------------------------------------------------------------

@dataclass
class MatchGroundTruth:
    """Known ATP/WTA match with published hold-rate stats."""
    label: str
    surface: str
    sqi_server: float    # SQI of the player serving
    rqi_receiver: float  # RQI of the opposing player
    actual_hold: float   # fraction of service games won (0-1)
    source: str


# Populate with real data from Tennis Abstract or UTS.
# Current entries are illustrative estimates — replace with verified stats.
TEST_MATCHES: list[MatchGroundTruth] = [
    # MatchGroundTruth(
    #     label="Alcaraz serving at Wimbledon 2024 SF",
    #     surface="grass", sqi_server=128, rqi_receiver=110,
    #     actual_hold=0.89, source="Tennis Abstract"
    # ),
]


def test_scale(scale: float, matches: list[MatchGroundTruth]) -> dict:
    if not matches:
        return {"scale": scale, "mean_abs_error": float("nan"), "n": 0}
    errors = []
    for m in matches:
        p = p_serve_model(m.sqi_server, m.rqi_receiver, m.surface, scale)
        predicted_hold = hold_prob(p)
        errors.append(abs(predicted_hold - m.actual_hold))
    mae = sum(errors) / len(errors)
    return {"scale": scale, "mean_abs_error": mae, "max_error": max(errors), "n": len(matches)}


def find_best_scale(matches: list[MatchGroundTruth],
                    scales: list[float] | None = None) -> dict:
    if scales is None:
        scales = [20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 100.0]
    results = [test_scale(s, matches) for s in scales]
    valid = [r for r in results if not math.isnan(r["mean_abs_error"])]
    if not valid:
        print("No ground truth data in TEST_MATCHES. Populate and re-run.")
        return {}
    best = min(valid, key=lambda x: x["mean_abs_error"])
    print(f"\n{'Scale':>8} | {'Mean Error':>12} | {'Max Error':>10}")
    print("-" * 38)
    for r in results:
        marker = " <-- BEST" if r["scale"] == best["scale"] else ""
        if math.isnan(r["mean_abs_error"]):
            print(f"{r['scale']:>8.0f} | {'n/a':>12} | {'n/a':>10}")
        else:
            print(f"{r['scale']:>8.0f} | {r['mean_abs_error']:>12.4f} | "
                  f"{r['max_error']:>10.4f}{marker}")
    return best


# ---------------------------------------------------------------------------
# SQI / RQI computation reference (ATP men's tour averages)
# ---------------------------------------------------------------------------
#
# SQI = ((first_serve_in_pct / 0.62) +
#         (first_serve_pts_won_pct / 0.73) +
#         (second_serve_pts_won_pct / 0.54)) / 3 * 100
#
# RQI = ((bp_converted_pct / 0.40) * 0.6 +
#         (return_pts_won_pct / 0.32) * 0.4) * 100
#
# actual_hold = service_games_won / service_games_total   (from match stats)
#
# Tour averages used as denominators:
#   first_serve_in:      62%
#   first_serve_pts_won: 73%
#   second_serve_pts_won:54%
#   bp_converted:        40%
#   return_pts_won:      32%


if __name__ == "__main__":
    SCALES = [20, 30, 40, 50, 60, 80, 100]

    run_scale_summary(SCALES)

    print("\n\nDetailed scenario tables:")
    for s in [40, 60, 80]:
        run_scenario_table(s)

    if TEST_MATCHES:
        print("\n\n=== Scale validation against ground truth ===")
        best = find_best_scale(TEST_MATCHES)
        if best:
            current = test_scale(40.0, TEST_MATCHES)
            if best["scale"] != 40.0:
                improvement = (current["mean_abs_error"] - best["mean_abs_error"]) * 100
                print(f"\nRecommendation: change LOGISTIC_SCALE 40 -> {best['scale']:.0f} "
                      f"(~{improvement:.1f}pp error reduction)")
                print("  Update SQI_LOGISTIC_SCALE constant in analyze_tennis.py")
            else:
                print("\nscale=40 validated. No change needed.")
    else:
        print("\n\nTEST_MATCHES is empty. Populate with real ATP data to find best scale.")
        print("See https://www.tennisabstract.com/charting/ for match-level serve stats.")
