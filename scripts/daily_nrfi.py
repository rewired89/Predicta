"""
scripts/daily_nrfi.py

Automated daily NRFI pipeline — runs in GitHub Actions twice a day.

Morning mode (default):
  1. Fetch today's MLB schedule from MLB Stats API
  2. Run NRFI analysis for each game via run_baseball_analysis()
  3. Save raw predictions → data/nrfi_predictions/YYYY-MM-DD.json
  4. Write markdown report  → data/nrfi_reports/YYYY-MM-DD.md

Night / resolve mode (--resolve):
  1. Read yesterday's prediction JSON
  2. Fetch final linescores from MLB Stats API
  3. Compute 1st-inning NRFI outcome for each game
  4. Update the prediction JSON with results
  5. Append a Results section to the markdown report

Usage:
  python scripts/daily_nrfi.py              # predict today's games
  python scripts/daily_nrfi.py --resolve    # resolve yesterday's games
  python scripts/daily_nrfi.py --date 2026-07-04           # specific date
  python scripts/daily_nrfi.py --resolve --date 2026-07-04 # resolve specific date
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

# Load .env for local runs
try:
    from dotenv import load_dotenv
    load_dotenv(_REPO / ".env")
except ImportError:
    pass

MLB_API   = "https://statsapi.mlb.com/api/v1"
PRED_DIR  = _REPO / "data" / "nrfi_predictions"
RPT_DIR   = _REPO / "data" / "nrfi_reports"

# MLB Stats API team abbreviation → name map built at runtime from schedule
# These match ESPN well enough for fetch_baseball_context fuzzy matching
MLB_ABBR_MAP: dict[str, str] = {}


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None, retries: int = 3) -> dict:
    import urllib.request, urllib.parse
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Predicta/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    return {}


# ── Schedule fetch ────────────────────────────────────────────────────────────

def fetch_schedule(game_date: str) -> list[dict]:
    """
    Return list of dicts: {game_pk, home_abbr, away_abbr, home_name, away_name, game_time}
    Primary: MLB Stats API.  Fallback: ESPN cached scoreboard.
    game_date: 'YYYY-MM-DD'
    """
    try:
        return _fetch_schedule_mlb(game_date)
    except Exception as e:
        print(f"  MLB Stats API unavailable ({e}), trying ESPN cache...")
        return _fetch_schedule_espn_cache(game_date)


def _fetch_schedule_mlb(game_date: str) -> list[dict]:
    data = _get(f"{MLB_API}/schedule", {
        "sportId": 1,
        "date":    game_date,
        "hydrate": "team,probablePitcher",
    })

    games = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            status = g.get("status", {}).get("abstractGameState", "")
            if status == "Final" and "--resolve" not in sys.argv:
                continue  # skip already-finished games in morning mode

            home = g.get("teams", {}).get("home", {})
            away = g.get("teams", {}).get("away", {})
            home_team = home.get("team", {})
            away_team = away.get("team", {})

            home_abbr = home_team.get("abbreviation", "")
            away_abbr = away_team.get("abbreviation", "")
            home_name = home_team.get("name", home_abbr)
            away_name = away_team.get("name", away_abbr)

            MLB_ABBR_MAP[home_abbr] = home_name
            MLB_ABBR_MAP[away_abbr] = away_name

            home_pitcher = home.get("probablePitcher", {}).get("fullName", "TBD")
            away_pitcher = away.get("probablePitcher", {}).get("fullName", "TBD")

            games.append({
                "game_pk":      g.get("gamePk"),
                "game_time":    g.get("gameDate", ""),
                "home_abbr":    home_abbr,
                "away_abbr":    away_abbr,
                "home_name":    home_name,
                "away_name":    away_name,
                "home_pitcher": home_pitcher,
                "away_pitcher": away_pitcher,
                "status":       status,
            })

    return games


def _fetch_schedule_espn_cache(game_date: str) -> list[dict]:
    """Fallback: read ESPN scoreboard from data/live/mlb_scoreboard.json."""
    cache = _REPO / "data" / "live" / "mlb_scoreboard.json"
    if not cache.exists():
        return []

    with open(cache) as f:
        raw = json.load(f)
    scoreboard = raw.get("data", raw)  # handle nested or flat

    games = []
    for event in scoreboard.get("events", []):
        for comp in event.get("competitions", []):
            competitors = comp.get("competitors", [])
            home_c = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away_c = next((c for c in competitors if c.get("homeAway") == "away"), {})

            home_team = home_c.get("team", {})
            away_team = away_c.get("team", {})

            home_abbr = home_team.get("abbreviation", "")
            away_abbr = away_team.get("abbreviation", "")

            # Extract probable pitchers from probables list
            def get_pitcher(comp_entry):
                for p in comp_entry.get("probables", []):
                    name = p.get("athlete", {}).get("displayName", "") or p.get("displayName", "")
                    if name:
                        return name
                return "TBD"

            games.append({
                "game_pk":      comp.get("id"),
                "game_time":    comp.get("date", ""),
                "home_abbr":    home_abbr,
                "away_abbr":    away_abbr,
                "home_name":    home_team.get("displayName", home_abbr),
                "away_name":    away_team.get("displayName", away_abbr),
                "home_pitcher": get_pitcher(home_c),
                "away_pitcher": get_pitcher(away_c),
                "status":       comp.get("status", {}).get("type", {}).get("name", ""),
            })

    return games


# ── Linescore fetch (for resolve) ─────────────────────────────────────────────

def fetch_linescore(game_pk: int) -> Optional[dict]:
    """
    Returns {home_1st: int, away_1st: int} for a completed game, or None.
    """
    try:
        data = _get(f"{MLB_API}/game/{game_pk}/linescore")
        innings = data.get("innings", [])
        if not innings:
            return None
        first = innings[0]
        home_runs = first.get("home", {}).get("runs")
        away_runs = first.get("away", {}).get("runs")
        if home_runs is None or away_runs is None:
            return None
        return {"home_1st": int(home_runs), "away_1st": int(away_runs)}
    except Exception:
        return None


# ── CLV / odds capture ──────────────────────────────────────────────────────

def _bet_side(p_nrfi: Optional[float]) -> str:
    """Side the model leans: NRFI when p_nrfi >= 50, else YRFI."""
    return "NRFI" if (p_nrfi is not None and p_nrfi >= 50) else "YRFI"


def capture_odds(predictions: list[dict], phase: str) -> int:
    """
    Fetch current NRFI/YRFI odds from The Odds API and attach to each prediction
    record for Closing Line Value (CLV) measurement.

    phase="entry"   → fill entry_* (the line when we made the pick). Also seeds
                       closing_* so a single daily run still yields a CLV of 0.
    phase="closing" → update closing_* (the latest line before first pitch).

    No-op (returns 0) when ODDS_API_KEY is unset or no games match. Mutates the
    records in place; caller is responsible for saving the JSON.
    """
    try:
        from fetchers.nrfi_odds import fetch_nrfi_odds
    except Exception:
        return 0

    games = [
        {
            "home_abbr": p.get("home_abbr", ""),
            "away_abbr": p.get("away_abbr", ""),
            "home_name": p.get("home_team", p.get("home_name", "")),
            "away_name": p.get("away_team", p.get("away_name", "")),
        }
        for p in predictions
        if not p.get("error")
    ]
    odds = fetch_nrfi_odds(games)
    if not odds:
        return 0

    updated = 0
    for p in predictions:
        key = f"{p.get('away_abbr')}@{p.get('home_abbr')}"
        o = odds.get(key)
        if not o:
            continue
        if phase == "entry":
            # Only set entry once; seed closing so same-day CLV is defined.
            if p.get("entry_nrfi_dec") is None:
                p["entry_nrfi_dec"] = o["nrfi_dec"]
                p["entry_yrfi_dec"] = o["yrfi_dec"]
                p["entry_book"]     = o["book"]
                p["entry_odds_at"]  = o["captured_at"]
            p["closing_nrfi_dec"] = o["nrfi_dec"]
            p["closing_yrfi_dec"] = o["yrfi_dec"]
            p["closing_book"]     = o["book"]
            p["closing_odds_at"]  = o["captured_at"]
        else:  # closing
            p["closing_nrfi_dec"] = o["nrfi_dec"]
            p["closing_yrfi_dec"] = o["yrfi_dec"]
            p["closing_book"]     = o["book"]
            p["closing_odds_at"]  = o["captured_at"]
        updated += 1

    return updated


def _compute_clv(pred: dict) -> None:
    """Compute clv_pp + beat_close on a prediction record (in place) if both
    entry and closing NRFI lines are present. Mirrors into nrfi_bets best-effort."""
    if (pred.get("entry_nrfi_dec") and pred.get("entry_yrfi_dec")
            and pred.get("closing_nrfi_dec") and pred.get("closing_yrfi_dec")):
        try:
            from models.devig import nrfi_clv
            side = _bet_side(pred.get("p_nrfi"))
            res = nrfi_clv(
                side,
                pred["entry_nrfi_dec"], pred["entry_yrfi_dec"],
                pred["closing_nrfi_dec"], pred["closing_yrfi_dec"],
            )
            pred["clv_pp"]     = res["clv_pp"]
            pred["beat_close"] = res["beat_close"]
        except Exception:
            return
    _mirror_clv_to_db(pred)


def _mirror_clv_to_db(pred: dict) -> None:
    """Best-effort: copy odds/CLV onto the matching nrfi_bets row (live app path).
    Silent no-op if the row doesn't exist (e.g. ephemeral CI database)."""
    try:
        from db.database import get_db
        with get_db() as db:
            db.execute(
                """UPDATE nrfi_bets SET
                     entry_nrfi_dec=?, entry_yrfi_dec=?, entry_book=?, entry_odds_at=?,
                     closing_nrfi_dec=?, closing_yrfi_dec=?, closing_book=?, closing_odds_at=?,
                     clv_pp=?, beat_close=?
                   WHERE game_date=? AND home_team=? AND away_team=?""",
                (
                    pred.get("entry_nrfi_dec"), pred.get("entry_yrfi_dec"),
                    pred.get("entry_book"), pred.get("entry_odds_at"),
                    pred.get("closing_nrfi_dec"), pred.get("closing_yrfi_dec"),
                    pred.get("closing_book"), pred.get("closing_odds_at"),
                    pred.get("clv_pp"), pred.get("beat_close"),
                    pred.get("game_date"), pred.get("home_team"), pred.get("away_team"),
                ),
            )
    except Exception:
        pass


# ── Prediction runner ─────────────────────────────────────────────────────────

def run_predictions(games: list[dict], game_date: str) -> list[dict]:
    """Run NRFI analysis for each game. Returns list of prediction records."""
    from analyze_baseball import run_baseball_analysis

    predictions = []
    total = len(games)

    for i, g in enumerate(games, 1):
        home = g["home_abbr"]
        away = g["away_abbr"]
        print(f"  [{i}/{total}] {away} @ {home} ... ", end="", flush=True)

        try:
            result = run_baseball_analysis(
                f"{home} vs {away} {game_date}",
                bankroll=1000.0,
            )
            nrfi_market = result.get("markets", {}).get("nrfi", {})
            opts   = nrfi_market.get("options", [])
            p_nrfi = opts[0].get("prob") if opts else None
            p_yrfi = opts[1].get("prob") if len(opts) > 1 else None

            bet_recs  = result.get("bet_recommendations", [])
            nrfi_rec  = next((r for r in bet_recs if r.get("market") == "NRFI"), {})
            verdict   = nrfi_rec.get("verdict", "SKIP")
            kelly     = nrfi_rec.get("kelly") or {}
            model_src = nrfi_market.get("model", "unknown")

            rec = {
                "game_pk":      g["game_pk"],
                "game_date":    game_date,
                "game_time":    g["game_time"],
                "home_team":    result.get("team_home", g["home_name"]),
                "away_team":    result.get("team_away", g["away_name"]),
                "home_abbr":    home,
                "away_abbr":    away,
                "home_starter": result.get("starters", {}).get("home", {}).get("name", g["home_pitcher"]),
                "away_starter": result.get("starters", {}).get("away", {}).get("name", g["away_pitcher"]),
                "p_nrfi":       p_nrfi,
                "p_yrfi":       p_yrfi,
                "verdict":      verdict,
                "confidence":   nrfi_rec.get("confidence"),
                "model":        model_src,
                "kelly_half":   kelly.get("half_kelly_pct"),
                "stake_100":    kelly.get("recommended_stake"),  # at $1000 bankroll
                "edge_pct":     kelly.get("edge_pct"),
                # ── CLV tracking (filled by capture_odds + resolve) ──────────
                "entry_nrfi_dec":   None,
                "entry_yrfi_dec":   None,
                "entry_book":       None,
                "entry_odds_at":    None,
                "closing_nrfi_dec": None,
                "closing_yrfi_dec": None,
                "closing_book":     None,
                "closing_odds_at":  None,
                "clv_pp":           None,   # vig-free (close - entry) prob on bet side, pp
                "beat_close":       None,   # 1 = positive CLV
                # filled in by resolve
                "home_1st_runs": None,
                "away_1st_runs": None,
                "outcome":       None,   # "NRFI" or "YRFI"
                "won":           None,
                "pnl_units":     None,
            }
            verdict_str = f"{verdict} ({p_nrfi:.1f}%)" if p_nrfi else verdict
            print(verdict_str)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"ERROR: {e}")
            rec = {
                "game_pk":    g["game_pk"],
                "game_date":  game_date,
                "home_abbr":  home,
                "away_abbr":  away,
                "home_name":  g["home_name"],
                "away_name":  g["away_name"],
                "error":      str(e),
            }

        predictions.append(rec)
        time.sleep(0.5)  # be gentle with ESPN API

    return predictions


# ── Resolve ───────────────────────────────────────────────────────────────────

def resolve_predictions(game_date: str) -> list[dict]:
    """
    Load predictions for game_date, fetch final linescores, add outcomes.
    Returns updated prediction list.
    """
    pred_file = PRED_DIR / f"{game_date}.json"
    if not pred_file.exists():
        print(f"No prediction file for {game_date}")
        return []

    with open(pred_file) as f:
        predictions = json.load(f)

    total     = len(predictions)
    resolved  = 0

    for pred in predictions:
        if pred.get("outcome") is not None:
            continue  # already resolved
        game_pk = pred.get("game_pk")
        if not game_pk:
            continue

        ls = fetch_linescore(game_pk)
        if ls is None:
            print(f"  {pred.get('away_abbr','?')} @ {pred.get('home_abbr','?')}: linescore not available")
            continue

        h1 = ls["home_1st"]
        a1 = ls["away_1st"]
        outcome = "NRFI" if (h1 == 0 and a1 == 0) else "YRFI"

        p_nrfi  = pred.get("p_nrfi", 50)
        verdict = pred.get("verdict", "SKIP")
        # Determine bet side: BET/LEAN with p_nrfi >= 50 = bet NRFI; else bet YRFI
        if verdict in ("BET", "LEAN"):
            bet_side = "NRFI" if (p_nrfi is not None and p_nrfi >= 50) else "YRFI"
            won      = 1 if bet_side == outcome else 0
            pnl      = round(100 / 110, 4) if won else -1.0
        else:
            won  = None
            pnl  = None

        pred["home_1st_runs"] = h1
        pred["away_1st_runs"] = a1
        pred["outcome"]       = outcome
        pred["won"]           = won
        pred["pnl_units"]     = pnl

        # Closing Line Value: did the market move toward our side after we bet?
        _compute_clv(pred)
        clv_str = ""
        if pred.get("clv_pp") is not None:
            clv_str = f" | CLV {pred['clv_pp']:+.2f}pp"

        print(f"  {pred.get('away_abbr','?')} @ {pred.get('home_abbr','?')}: "
              f"1st inning {a1}-{h1} → {outcome} | "
              f"{'WIN' if won else 'LOSS' if won == 0 else 'NO BET'}{clv_str}")
        resolved += 1
        time.sleep(0.3)

    print(f"\n  Resolved {resolved}/{total} games.")
    return predictions


# ── Report writer ─────────────────────────────────────────────────────────────

def write_report(predictions: list[dict], game_date: str, is_resolve: bool = False) -> Path:
    """Write a markdown report to data/nrfi_reports/YYYY-MM-DD.md"""
    RPT_DIR.mkdir(parents=True, exist_ok=True)
    path = RPT_DIR / f"{game_date}.md"

    bets  = [p for p in predictions if p.get("verdict") in ("BET", "LEAN") and not p.get("error")]
    skips = [p for p in predictions if p.get("verdict") == "SKIP" and not p.get("error")]
    errs  = [p for p in predictions if p.get("error")]

    lines = [
        f"# NRFI Daily Report — {game_date}",
        f"*Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*",
        "",
        f"**Total games:** {len(predictions)}  |  **BET/LEAN:** {len(bets)}  |  **SKIP:** {len(skips)}",
        "",
    ]

    if bets:
        lines += ["## Plays", ""]
        lines += ["| # | Matchup | Starter (H) | Starter (A) | p_NRFI | Verdict | Half-Kelly | CLV | Model |",
                  "|---|---------|-------------|-------------|--------|---------|------------|-----|-------|"]
        for i, p in enumerate(bets, 1):
            home   = p.get("home_abbr", p.get("home_team","?"))
            away   = p.get("away_abbr", p.get("away_team","?"))
            hs     = p.get("home_starter","TBD")[:20]
            as_    = p.get("away_starter","TBD")[:20]
            pn     = f"{p['p_nrfi']:.1f}%" if p.get("p_nrfi") else "—"
            verd   = p.get("verdict","?")
            conf   = p.get("confidence","")
            verd_s = f"**{verd}**" + (f" ({conf})" if conf else "")
            hk     = f"{p['kelly_half']:.1f}%" if p.get("kelly_half") else "—"
            clv    = f"{p['clv_pp']:+.2f}pp" if p.get("clv_pp") is not None else "—"
            mdl    = p.get("model","?")

            outcome_cell = ""
            if p.get("outcome"):
                won_str = "✅" if p.get("won") else "❌"
                outcome_cell = f" → {p['outcome']} {won_str}"

            lines.append(
                f"| {i} | {away} @ {home}{outcome_cell} | {hs} | {as_} | {pn} | {verd_s} | {hk} | {clv} | {mdl} |"
            )
        lines.append("")

    if skips:
        lines += ["## Skipped Games", ""]
        lines += ["| Matchup | p_NRFI | p_YRFI | Model |",
                  "|---------|--------|--------|-------|"]
        for p in skips:
            home = p.get("home_abbr", "?")
            away = p.get("away_abbr", "?")
            pn   = f"{p['p_nrfi']:.1f}%" if p.get("p_nrfi") else "—"
            py   = f"{p['p_yrfi']:.1f}%" if p.get("p_yrfi") else "—"
            mdl  = p.get("model","?")
            lines.append(f"| {away} @ {home} | {pn} | {py} | {mdl} |")
        lines.append("")

    if is_resolve:
        bet_resolved = [p for p in bets if p.get("outcome") is not None]
        if bet_resolved:
            wins   = sum(1 for p in bet_resolved if p.get("won") == 1)
            losses = sum(1 for p in bet_resolved if p.get("won") == 0)
            pnl    = sum(p.get("pnl_units", 0) or 0 for p in bet_resolved)
            lines += [
                "## Results",
                "",
                f"**W-L:** {wins}–{losses}  |  **P&L:** {pnl:+.3f} units",
                "",
            ]
            # CLV summary — proof of edge that doesn't depend on the result.
            clv_bets = [p for p in bet_resolved if p.get("clv_pp") is not None]
            if clv_bets:
                beat = sum(1 for p in clv_bets if p.get("beat_close") == 1)
                avg  = sum(p["clv_pp"] for p in clv_bets) / len(clv_bets)
                lines += [
                    f"**CLV:** beat the close {beat}/{len(clv_bets)} "
                    f"({beat/len(clv_bets)*100:.0f}%)  |  **Avg CLV:** {avg:+.2f}pp",
                    "",
                    "*CLV = vig-free closing probability minus entry probability on the bet "
                    "side. Positive means the market moved toward our pick after we made it — "
                    "the sharpest available proof of edge.*",
                    "",
                ]

    if errs:
        lines += ["## Errors", ""]
        for p in errs:
            lines.append(f"- {p.get('away_abbr','?')} @ {p.get('home_abbr','?')}: {p.get('error','')}")
        lines.append("")

    path.write_text("\n".join(lines))
    return path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resolve", action="store_true",
                        help="Resolve yesterday's games (night run)")
    parser.add_argument("--capture-odds", action="store_true", dest="capture_odds",
                        help="Snapshot current NRFI lines as the closing reference (afternoon run)")
    parser.add_argument("--date", default=None,
                        help="Override date (YYYY-MM-DD). Default: today (morning) or yesterday (resolve)")
    args = parser.parse_args()

    today = date.today().isoformat()

    if args.capture_odds:
        target_date = args.date or today
        print(f"\n=== NRFI Closing-Line Capture — {target_date} ===")
        pred_file = PRED_DIR / f"{target_date}.json"
        if not pred_file.exists():
            print(f"No prediction file for {target_date}. Run predictions first.")
            return
        with open(pred_file) as f:
            predictions = json.load(f)
        n = capture_odds(predictions, phase="closing")
        with open(pred_file, "w") as f:
            json.dump(predictions, f, indent=2)
        if n:
            print(f"Updated closing lines for {n} games.")
        else:
            print("No odds captured (ODDS_API_KEY unset or no market match).")
        return

    if args.resolve:
        target_date = args.date or (date.today() - timedelta(days=1)).isoformat()
        print(f"\n=== NRFI Resolve — {target_date} ===")

        predictions = resolve_predictions(target_date)
        if not predictions:
            print("Nothing to resolve.")
            return

        # Save updated JSON
        pred_file = PRED_DIR / f"{target_date}.json"
        with open(pred_file, "w") as f:
            json.dump(predictions, f, indent=2)

        rpt = write_report(predictions, target_date, is_resolve=True)
        print(f"\nReport updated: {rpt}")

    else:
        target_date = args.date or today
        print(f"\n=== NRFI Daily Predictions — {target_date} ===\n")

        print("Fetching schedule...")
        games = fetch_schedule(target_date)
        print(f"Found {len(games)} games\n")

        if not games:
            print("No games scheduled. Nothing to predict.")
            return

        print("Running NRFI analysis...")
        predictions = run_predictions(games, target_date)

        # Capture the NRFI line available now as our entry price (for CLV).
        n_odds = capture_odds(predictions, phase="entry")
        if n_odds:
            print(f"Captured entry odds for {n_odds} games.")

        PRED_DIR.mkdir(parents=True, exist_ok=True)
        pred_file = PRED_DIR / f"{target_date}.json"
        with open(pred_file, "w") as f:
            json.dump(predictions, f, indent=2)
        print(f"\nPredictions saved: {pred_file}")

        rpt = write_report(predictions, target_date)
        print(f"Report written:    {rpt}")

        bets = [p for p in predictions if p.get("verdict") in ("BET", "LEAN")]
        print(f"\n{len(bets)} plays today out of {len(games)} games.")


if __name__ == "__main__":
    main()
