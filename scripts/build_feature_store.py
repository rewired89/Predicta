"""
scripts/build_feature_store.py

Build the committed pitcher feature store (data/nrfi_feature_store.json) that
lets the NRFI model get real FanGraphs/Savant features in CI, where those
sources are IP-blocked.

WHY: FanGraphs' leaders endpoint blocks datacenter IPs, so live enrichment
silently fails on GitHub Actions / Railway and every starter defaults to ESPN
— flattening the model to ~51% on every game. Run THIS locally (residential
IP, where FanGraphs works), commit the JSON, and the pipeline reads it at
runtime with no live fetch. Refresh every few days.

Usage:
  python scripts/build_feature_store.py            # FanGraphs + Savant (fuller)
  python scripts/build_feature_store.py --fg-only  # FanGraphs only (fast core)
  python scripts/build_feature_store.py --season 2025

Then commit data/nrfi_feature_store.json.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

OUT_PATH = _REPO / "data" / "nrfi_feature_store.json"


def _norm(name: str) -> str:
    return "".join(c for c in (name or "").lower() if c.isalnum())


def build(season: int | None = None, fg_only: bool = False) -> None:
    from fetchers.savant import (
        _load_fg_pitchers, fetch_pitcher_fg, fetch_pitcher_statcast,
        fetch_pitcher_arsenal, _current_season,
    )

    yr = season or _current_season()
    print(f"Building feature store for season {yr} "
          f"({'FanGraphs only' if fg_only else 'FanGraphs + Savant'})...")

    df = _load_fg_pitchers(yr)
    if df is None or df.empty:
        print("ERROR: FanGraphs pitching_stats returned nothing. Are you on a "
              "residential IP? (FanGraphs blocks datacenter IPs.)")
        sys.exit(1)

    names = df["Name"].tolist()
    print(f"FanGraphs returned {len(names)} qualified pitchers.")

    pitchers: dict[str, dict] = {}
    sources = ["fangraphs"] if fg_only else ["fangraphs", "savant"]

    for i, name in enumerate(names, 1):
        fg = fetch_pitcher_fg(name, yr)      # cached df → fast
        if not fg:
            continue
        feat = {
            "display_name": name,
            "siera":        fg.get("siera"),
            "xfip":         fg.get("xfip"),
            "fip":          fg.get("fip"),
            "era":          fg.get("era"),
            "k_pct":        fg.get("k_pct"),
            "bb_pct":       fg.get("bb_pct"),
            "k_bb_ratio":   fg.get("k_bb_ratio"),
            "swstr_pct":    fg.get("swstr_pct"),
            "f_strike_pct": fg.get("f_strike_pct"),
            "zone_pct":     fg.get("zone_pct"),
            "o_swing_pct":  fg.get("o_swing_pct"),
            "contact_pct":  fg.get("contact_pct"),
            "csw_pct":      fg.get("csw_pct"),
            "gb_pct":       fg.get("gb_pct"),
            "hr_fb_pct":    fg.get("hr_fb_pct"),
            "whip":         fg.get("whip"),
        }

        if not fg_only:
            try:
                sv = fetch_pitcher_statcast(name, yr)
                if sv:
                    feat["barrel_pct_against"]   = sv.get("barrel_pct_against")
                    feat["hard_hit_pct_against"] = sv.get("hard_hit_pct_against")
                    feat["exit_velo_against"]    = sv.get("exit_velo_against")
                    feat["xwoba_against"]        = sv.get("xwoba_against")
                ar = fetch_pitcher_arsenal(name, yr)
                if ar:
                    feat["avg_fb_velo"]  = ar.get("avg_fb_velo")
                    feat["whiff_pct"]    = ar.get("whiff_pct")
                    feat["fastball_pct"] = ar.get("fastball_pct")
                    feat["breaking_pct"] = ar.get("breaking_pct")
            except Exception as exc:
                print(f"  savant lookup failed for {name}: {exc}")
            time.sleep(0.3)  # be gentle with Savant

        pitchers[_norm(name)] = feat
        if i % 25 == 0:
            print(f"  {i}/{len(names)} pitchers...")

    store = {
        "season":   yr,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "sources":  sources,
        "n_pitchers": len(pitchers),
        "pitchers": pitchers,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(store, f, indent=2)

    n_siera  = sum(1 for p in pitchers.values() if p.get("siera") is not None)
    n_barrel = sum(1 for p in pitchers.values() if p.get("barrel_pct_against") is not None)
    print(f"\nSaved {len(pitchers)} pitchers → {OUT_PATH}")
    print(f"  with SIERA: {n_siera}  |  with barrel%: {n_barrel}")
    print("Commit data/nrfi_feature_store.json to activate it in CI.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--fg-only", action="store_true",
                    help="FanGraphs only (skip per-pitcher Savant; much faster)")
    args = ap.parse_args()
    build(season=args.season, fg_only=args.fg_only)
