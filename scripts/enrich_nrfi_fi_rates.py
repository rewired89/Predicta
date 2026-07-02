"""
scripts/enrich_nrfi_fi_rates.py

Post-processing enrichment for data/nrfi_dataset.csv.
Computes a **short-window, time-decayed** rolling first-inning run rate per
starter — no API calls, runs in ~5 seconds.

Why short + decayed (Kimi feedback, 2026-07):
  Baseball is the worst sport for stale historical aggregates. A starter's
  arsenal, velocity and command drift within a single season, so a 2-season
  average badly misprices their *current* first-inning risk. We therefore:
    • keep only the last WINDOW starts (recency), and
    • weight them by exponential time decay (HALF_LIFE_DAYS) so a start from
      3 weeks ago counts ~2× one from 6 weeks ago, and
    • shrink toward the league average when the sample is thin (PRIOR_STARTS),
      which stabilizes the noisy short window without reintroducing staleness.

Features added:
  home_starter_fi_rate  — pitcher's decayed fi_rate in their recent HOME starts
  away_starter_fi_rate  — pitcher's decayed fi_rate in their recent AWAY starts

Fallback cascade (per venue):
  1. Venue-specific decayed rate  (if ≥ MIN_STARTS_VENUE prior venue starts)
  2. Overall decayed rate         (if ≥ MIN_STARTS prior starts, any venue)
  3. League average               (computed from the data, not hardcoded)
All rates are shrunk toward the league average by PRIOR_STARTS pseudo-starts.

Run AFTER build_nrfi_dataset.py:
  python scripts/enrich_nrfi_fi_rates.py

Then retrain:
  python models/nrfi_model.py --train
"""
from __future__ import annotations
import math
import sys
from collections import defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

try:
    import pandas as pd
except ImportError:
    print("ERROR: pandas not installed. Run: pip install pandas")
    sys.exit(1)

CSV_PATH         = _REPO / "data" / "nrfi_dataset.csv"
WINDOW           = 6    # only the most recent 6 starts feed the rate
HALF_LIFE_DAYS   = 21   # a start's weight halves every 3 weeks
MIN_STARTS       = 3    # minimum overall prior starts before trusting a rate
MIN_STARTS_VENUE = 3    # minimum venue-specific prior starts for the venue rate
PRIOR_STARTS     = 2.0  # shrinkage strength: pseudo-starts at the league mean


def _decayed_rate(prior: list[tuple], ref_date, league_avg: float) -> float:
    """
    Weighted first-inning run rate from the most recent WINDOW starts, with
    exponential time decay and Bayesian shrinkage toward league_avg.

    prior:   list of (date, allowed_run) strictly before ref_date.
    Returns league_avg when prior is empty.
    """
    if not prior:
        return league_avg

    # Most recent WINDOW starts by date.
    recent = sorted(prior, key=lambda t: t[0])[-WINDOW:]

    decay = math.log(2) / HALF_LIFE_DAYS
    wsum = 0.0
    rsum = 0.0
    for d, r in recent:
        age = max(0, (ref_date - d).days)
        w   = math.exp(-decay * age)
        wsum += w
        rsum += w * r

    # Shrink toward the league mean with PRIOR_STARTS pseudo-observations.
    return (rsum + PRIOR_STARTS * league_avg) / (wsum + PRIOR_STARTS)


def compute_fi_rates(csv_path: Path = CSV_PATH) -> None:
    if not csv_path.exists():
        print(f"Dataset not found: {csv_path}")
        print("Run: python scripts/build_nrfi_dataset.py")
        sys.exit(1)

    df = pd.read_csv(csv_path, encoding="latin-1")
    print(f"Loaded {len(df)} rows from {csv_path}")

    df["game_date"]     = pd.to_datetime(df["game_date"])
    df["home_1st_runs"] = pd.to_numeric(df["home_1st_runs"], errors="coerce").fillna(0).astype(int)
    df["away_1st_runs"] = pd.to_numeric(df["away_1st_runs"], errors="coerce").fillna(0).astype(int)

    # League average fi_rate = P(a starter allows ≥1 run in the 1st inning).
    # NOT the YRFI rate (~0.477); per-pitcher fi_rate ≈ half of that.
    league_avg_home = (df["away_1st_runs"] > 0).mean()  # home starter faces away bats
    league_avg_away = (df["home_1st_runs"] > 0).mean()  # away starter faces home bats
    LEAGUE_AVG = (league_avg_home + league_avg_away) / 2
    print(f"League average fi_rate: {LEAGUE_AVG:.3f}  "
          f"(home starters: {league_avg_home:.3f}, away starters: {league_avg_away:.3f})")
    print(f"Window={WINDOW} starts | half-life={HALF_LIFE_DAYS}d | "
          f"shrinkage prior={PRIOR_STARTS} starts")

    df = df.sort_values(["game_date", "game_pk"]).reset_index(drop=True)

    # Venue-split + overall histories: name → list of (date, allowed_run)
    history_home: dict[str, list] = defaultdict(list)
    history_away: dict[str, list] = defaultdict(list)
    history_all:  dict[str, list] = defaultdict(list)

    h_rates: list[float] = []
    a_rates: list[float] = []

    for _, row in df.iterrows():
        date = row["game_date"]
        h    = str(row.get("home_starter", "") or "").strip()
        a    = str(row.get("away_starter", "") or "").strip()
        h1   = int(row["home_1st_runs"])
        a1   = int(row["away_1st_runs"])

        def _rate(name: str, venue_hist: dict, overall_hist: dict) -> float:
            if not name:
                return LEAGUE_AVG
            venue_prior   = [(d, r) for d, r in venue_hist[name]   if d < date]
            overall_prior = [(d, r) for d, r in overall_hist[name] if d < date]
            if len(venue_prior) >= MIN_STARTS_VENUE:
                return _decayed_rate(venue_prior, date, LEAGUE_AVG)
            if len(overall_prior) >= MIN_STARTS:
                return _decayed_rate(overall_prior, date, LEAGUE_AVG)
            return LEAGUE_AVG

        # home starter pitches at home → their recent HOME-start rate
        h_rates.append(_rate(h, history_home, history_all))
        # away starter pitches on the road → their recent AWAY-start rate
        a_rates.append(_rate(a, history_away, history_all))

        # Update AFTER reading — no look-ahead bias.
        # Home starter allowed a run if the away team scored (away bats vs home).
        if h:
            history_home[h].append((date, int(a1 > 0)))
            history_all[h].append((date, int(a1 > 0)))
        # Away starter allowed a run if the home team scored (home bats vs away).
        if a:
            history_away[a].append((date, int(h1 > 0)))
            history_all[a].append((date, int(h1 > 0)))

    df["home_starter_fi_rate"] = h_rates
    df["away_starter_fi_rate"] = a_rates

    for col in ["home_starter_fi_rate", "away_starter_fi_rate"]:
        fi = df[col]
        print(f"{col:28s}  mean={fi.mean():.3f}  std={fi.std():.3f}  "
              f"min={fi.min():.3f}  max={fi.max():.3f}")

    n_real = (df["home_starter_fi_rate"] != LEAGUE_AVG).sum()
    print(f"\nRows with a real rolling fi_rate (home): {n_real}/{len(df)} "
          f"({n_real/len(df)*100:.1f}%)")

    df["game_date"] = df["game_date"].dt.strftime("%Y-%m-%d")
    df.to_csv(csv_path, index=False, encoding="utf-8")
    print(f"\nSaved enriched dataset → {csv_path}")
    print("Next step: python models/nrfi_model.py --train")


if __name__ == "__main__":
    compute_fi_rates()
