"""
scripts/enrich_nrfi_fi_rates.py

Post-processing enrichment for data/nrfi_dataset.csv.
Computes rolling per-starter first-inning run rate from within the dataset
itself — no API calls, runs in ~5 seconds.

Features added:
  home_starter_fi_rate  — fraction of prior starts where this pitcher allowed
                          ≥1 run in the 1st inning (away_1st_runs > 0 when home)
  away_starter_fi_rate  — same for the away starter (home_1st_runs > 0 when away)

Defaults to league average (0.477) when fewer than MIN_STARTS prior appearances
exist in the dataset.

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

CSV_PATH     = _REPO / "data" / "nrfi_dataset.csv"
LEAGUE_AVG   = 0.477   # historical: ~47.7% of starts see ≥1 first-inning run allowed
MIN_STARTS   = 5       # require this many prior starts before trusting the rolling rate


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

    # Sort chronologically so rolling look-back is always on past data only
    df = df.sort_values(["game_date", "game_pk"]).reset_index(drop=True)

    # pitcher_name → list of (date, allowed_run: bool)
    # allowed_run = did this pitcher give up ≥1 run in the 1st inning of that start?
    history: dict[str, list] = defaultdict(list)

    h_rates: list[float] = []
    a_rates: list[float] = []

    for _, row in df.iterrows():
        date = row["game_date"]
        h    = str(row.get("home_starter", "") or "").strip()
        a    = str(row.get("away_starter", "") or "").strip()
        h1   = int(row["home_1st_runs"])
        a1   = int(row["away_1st_runs"])

        def _rate(name: str) -> float:
            if not name:
                return LEAGUE_AVG
            prior = [(d, r) for d, r in history[name] if d < date]
            if len(prior) < MIN_STARTS:
                return LEAGUE_AVG
            return sum(r for _, r in prior) / len(prior)

        h_rates.append(_rate(h))
        a_rates.append(_rate(a))

        # Update history AFTER reading — no look-ahead bias
        # Home starter "allowed" if the away team scored (away bats vs home starter in 1st)
        if h:
            history[h].append((date, int(a1 > 0)))
        # Away starter "allowed" if the home team scored (home bats vs away starter in 1st)
        if a:
            history[a].append((date, int(h1 > 0)))

    df["home_starter_fi_rate"] = h_rates
    df["away_starter_fi_rate"] = a_rates

    # Summary statistics
    fi = df["home_starter_fi_rate"]
    print(f"\nhome_starter_fi_rate  mean={fi.mean():.3f}  std={fi.std():.3f}  "
          f"min={fi.min():.3f}  max={fi.max():.3f}")
    fi = df["away_starter_fi_rate"]
    print(f"away_starter_fi_rate  mean={fi.mean():.3f}  std={fi.std():.3f}  "
          f"min={fi.min():.3f}  max={fi.max():.3f}")

    # How many rows have real rolling data vs. league-average defaults?
    n_real = (df["home_starter_fi_rate"] != LEAGUE_AVG).sum()
    print(f"\nRows with real rolling fi_rate (home): {n_real}/{len(df)} ({n_real/len(df)*100:.1f}%)")

    # Write enriched CSV (keep original date format)
    df["game_date"] = df["game_date"].dt.strftime("%Y-%m-%d")
    df.to_csv(csv_path, index=False, encoding="utf-8")
    print(f"\nSaved enriched dataset → {csv_path}")
    print("Next step: python models/nrfi_model.py --train")


if __name__ == "__main__":
    compute_fi_rates()
