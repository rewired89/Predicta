"""
scripts/enrich_nrfi_fi_rates.py

Post-processing enrichment for data/nrfi_dataset.csv.
Computes rolling per-starter first-inning run rate from within the dataset
itself — no API calls, runs in ~5 seconds.

Features added:
  home_starter_fi_rate  — pitcher's fi_rate specifically in HOME starts
                          (venue-split: a pitcher who suppresses runs at home
                          but struggles on the road gets separate rates)
  away_starter_fi_rate  — pitcher's fi_rate specifically in AWAY (road) starts

Fallback cascade when insufficient venue-specific data:
  1. Venue-specific rate  (if ≥ MIN_STARTS_VENUE prior venue-split starts)
  2. Overall rate         (if ≥ MIN_STARTS prior starts of any venue)
  3. League average       (computed from data — NOT hardcoded 0.477 YRFI rate)

Run AFTER build_nrfi_dataset.py:
  python scripts/enrich_nrfi_fi_rates.py

Then retrain:
  python models/nrfi_model.py --train
"""
from __future__ import annotations
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

CSV_PATH        = _REPO / "data" / "nrfi_dataset.csv"
MIN_STARTS      = 5   # minimum overall starts before trusting any rolling rate
MIN_STARTS_VENUE = 3  # minimum venue-specific starts for venue-split rate


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

    # League average fi_rate = P(pitcher allows ≥1 run in 1st inning).
    # NOT the YRFI rate (0.477). Per-pitcher fi_rate ≈ half of YRFI rate.
    league_avg_home = (df["away_1st_runs"] > 0).mean()
    league_avg_away = (df["home_1st_runs"] > 0).mean()
    LEAGUE_AVG = (league_avg_home + league_avg_away) / 2
    print(f"League average fi_rate: {LEAGUE_AVG:.3f}  "
          f"(home starters: {league_avg_home:.3f}, away starters: {league_avg_away:.3f})")

    df = df.sort_values(["game_date", "game_pk"]).reset_index(drop=True)

    # Separate histories: venue-split (home starts vs road starts) + overall
    # history_home[name] = list of (date, allowed_run) when pitcher was HOME starter
    # history_away[name] = list of (date, allowed_run) when pitcher was AWAY starter
    # history_all[name]  = combined regardless of venue
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
                return sum(r for _, r in venue_prior) / len(venue_prior)
            if len(overall_prior) >= MIN_STARTS:
                return sum(r for _, r in overall_prior) / len(overall_prior)
            return LEAGUE_AVG

        # home starter → use their HOME-start rate (they're pitching at home)
        h_rates.append(_rate(h, history_home, history_all))
        # away starter → use their AWAY-start rate (they're pitching on the road)
        a_rates.append(_rate(a, history_away, history_all))

        # Update AFTER reading — no look-ahead bias
        # Home starter allowed run if away team scored (away bats vs home starter)
        if h:
            history_home[h].append((date, int(a1 > 0)))
            history_all[h].append((date, int(a1 > 0)))
        # Away starter allowed run if home team scored (home bats vs away starter)
        if a:
            history_away[a].append((date, int(h1 > 0)))
            history_all[a].append((date, int(h1 > 0)))

    df["home_starter_fi_rate"] = h_rates
    df["away_starter_fi_rate"] = a_rates

    # Summary
    for col in ["home_starter_fi_rate", "away_starter_fi_rate"]:
        fi = df[col]
        print(f"{col:28s}  mean={fi.mean():.3f}  std={fi.std():.3f}  "
              f"min={fi.min():.3f}  max={fi.max():.3f}")

    n_real = (df["home_starter_fi_rate"] != LEAGUE_AVG).sum()
    print(f"\nRows with real rolling fi_rate (home): {n_real}/{len(df)} ({n_real/len(df)*100:.1f}%)")

    df["game_date"] = df["game_date"].dt.strftime("%Y-%m-%d")
    df.to_csv(csv_path, index=False, encoding="utf-8")
    print(f"\nSaved enriched dataset → {csv_path}")
    print("Next step: python models/nrfi_model.py --train")


if __name__ == "__main__":
    compute_fi_rates()
