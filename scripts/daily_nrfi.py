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
    Returns full linescore for a completed game:
      home_1st, away_1st          — first-inning runs (NRFI)
      home_runs, away_runs        — final total runs (moneyline + O/U)
      home_f5, away_f5            — runs through 5 innings (F5)
    Returns None if game data is unavailable or incomplete.
    """
    try:
        data = _get(f"{MLB_API}/game/{game_pk}/linescore")
        innings = data.get("innings", [])
        if not innings:
            return None
        first = innings[0]
        home_1st = first.get("home", {}).get("runs")
        away_1st = first.get("away", {}).get("runs")
        if home_1st is None or away_1st is None:
            return None

        # Total runs from the teams block (most reliable)
        teams = data.get("teams", {})
        home_total = teams.get("home", {}).get("runs")
        away_total = teams.get("away", {}).get("runs")

        # Runs through 5 innings (F5)
        home_f5 = away_f5 = 0
        for inn in innings[:5]:
            h = inn.get("home", {}).get("runs")
            a = inn.get("away", {}).get("runs")
            if h is not None:
                home_f5 += int(h)
            if a is not None:
                away_f5 += int(a)

        return {
            "home_1st": int(home_1st),
            "away_1st": int(away_1st),
            "home_runs": int(home_total) if home_total is not None else None,
            "away_runs": int(away_total) if away_total is not None else None,
            "home_f5": home_f5,
            "away_f5": away_f5,
        }
    except Exception:
        return None


# ── CLV / odds capture ──────────────────────────────────────────────────────

# Minimum hours the entry line must be captured BEFORE first pitch for its CLV
# to count. Kimi's caveat: if we capture "entry" after the morning sharps have
# already moved the line, CLV measures "moved WITH the sharps", not "led them".
# We time-stamp every entry and flag anything captured too close to first pitch.
MIN_ENTRY_LEAD_HOURS = 3.0


def _bet_side(p_nrfi: Optional[float]) -> str:
    """Side the model leans: NRFI when p_nrfi >= 50, else YRFI."""
    return "NRFI" if (p_nrfi is not None and p_nrfi >= 50) else "YRFI"


def _parse_iso(ts: str):
    """Parse an ISO-8601 timestamp (handles trailing 'Z' and missing seconds)."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _hours_before_first_pitch(game_time: str, captured_at: str) -> Optional[float]:
    """Hours between an odds capture and first pitch (positive = captured before)."""
    gt = _parse_iso(game_time)
    cap = _parse_iso(captured_at)
    if gt is None or cap is None:
        return None
    if gt.tzinfo is None:
        gt = gt.replace(tzinfo=timezone.utc)
    if cap.tzinfo is None:
        cap = cap.replace(tzinfo=timezone.utc)
    return round((gt - cap).total_seconds() / 3600.0, 2)


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
                # Audit trail: how early was this entry line? (CLV validity)
                lead = _hours_before_first_pitch(p.get("game_time", ""), o["captured_at"])
                p["entry_hours_to_fp"] = lead
                p["entry_stale"] = (lead is not None and lead < MIN_ENTRY_LEAD_HOURS)
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
            if result.get("error"):
                # run_baseball_analysis failed outright (e.g. query-parse step hit an
                # Anthropic API error) — without this check the code below silently
                # pulls .get() off empty dicts and produces a record that looks like
                # a normal SKIP (verdict/model set, every stat None) instead of a
                # flagged failure. Raise so it lands in the except block and shows
                # up in the report's Errors section instead of vanishing silently.
                raise RuntimeError(result["error"])
            nrfi_market = result.get("markets", {}).get("nrfi", {})
            opts   = nrfi_market.get("options", [])
            p_nrfi = opts[0].get("prob") if opts else None
            p_yrfi = opts[1].get("prob") if len(opts) > 1 else None

            bet_recs  = result.get("bet_recommendations", [])
            nrfi_rec  = next((r for r in bet_recs if r.get("market") == "NRFI"), {})
            verdict   = nrfi_rec.get("verdict", "SKIP")
            kelly     = nrfi_rec.get("kelly") or {}
            model_src = nrfi_market.get("model", "unknown")

            # The model also predicts moneyline + F5 + O/U for every game —
            # capture them so the daily report shows the full slate.
            f5_rec = next((r for r in bet_recs if r.get("market") == "F5"), {})
            ml_rec = next((r for r in bet_recs if r.get("market") == "Full Game"), {})
            ou_rec = next((r for r in bet_recs if r.get("market") == "Game Total"), {})

            # Feature-source diagnostics: record which advanced pitcher features
            # were real vs defaulted, so we can SEE from the committed JSON whether
            # FanGraphs/Savant enrichment actually fired in the CI environment.
            def _feat_flags(s: dict) -> dict:
                return {
                    "fip_source": s.get("fip_source"),
                    "siera":  s.get("siera") is not None,
                    "barrel": s.get("barrel_pct_against") is not None,
                    "csw":    s.get("csw_pct") is not None,
                    "velo":   s.get("avg_fb_velo") is not None,
                }
            _hs = result.get("starters", {}).get("home", {})
            _as = result.get("starters", {}).get("away", {})
            _feat_home = _feat_flags(_hs)
            _feat_away = _feat_flags(_as)
            _enriched  = any(v is True for d in (_feat_home, _feat_away)
                             for v in (d["siera"], d["barrel"], d["csw"], d["velo"]))

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
                # Full-model picks (moneyline + F5) — the model predicts these
                # for EVERY game, not just NRFI.
                "ml_pick":      ml_rec.get("bet"),        # "<team> moneyline"
                "ml_prob":      ml_rec.get("model_prob"),
                "ml_verdict":   ml_rec.get("verdict"),
                "f5_pick":      f5_rec.get("bet"),        # "<team> to lead after 5"
                "f5_prob":      f5_rec.get("model_prob"),
                "f5_verdict":   f5_rec.get("verdict"),
                # Over/Under (Game Total)
                "ou_pick":      ou_rec.get("bet"),        # "Over 8.5" / "Under 7.5"
                "ou_prob":      ou_rec.get("model_prob"),
                "ou_verdict":   ou_rec.get("verdict"),
                "ou_line":      ou_rec.get("line"),       # numeric line (8.5)
                # Expected runs (Poisson model output)
                "mu_home":      result.get("mu_home"),
                "mu_away":      result.get("mu_away"),
                "expected_total": round((result.get("mu_home", 0) or 0)
                                        + (result.get("mu_away", 0) or 0), 2),
                "data_confidence": result.get("data_confidence"),
                # Injury gate diagnostics (added 2026-07-05, per Kimi's review) —
                # tracks how often the starter-injury downgrade fires, so it can
                # be sanity-checked against real accuracy after ~50+ more games
                # instead of just trusting the gate blindly.
                "starter_injury_flag": result.get("starter_injury_flag", False),
                "home_starter_injury": result.get("starters", {}).get("home", {}).get("injury_status"),
                "away_starter_injury": result.get("starters", {}).get("away", {}).get("injury_status"),
                "features_enriched": _enriched,   # any advanced stat present?
                "feat_home":    _feat_home,
                "feat_away":    _feat_away,
                "kelly_half":   kelly.get("half_kelly_pct"),
                "stake_100":    kelly.get("recommended_stake"),  # at $1000 bankroll
                "edge_pct":     kelly.get("edge_pct"),
                # ── CLV tracking (filled by capture_odds + resolve) ──────────
                "entry_nrfi_dec":   None,
                "entry_yrfi_dec":   None,
                "entry_book":       None,
                "entry_odds_at":    None,
                "entry_hours_to_fp": None,   # hours before first pitch (CLV validity)
                "entry_stale":      None,    # True if captured < 3h before first pitch
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
                "lean_side":     None,   # model's lean (NRFI/YRFI) — every game
                "lean_correct":  None,   # 1 if lean matched outcome — every game
                "won":           None,   # bet result (BET/LEAN only)
                "pnl_units":     None,
                # All-market resolution (filled by resolve)
                "home_runs":     None,   # final score
                "away_runs":     None,
                "ml_correct":    None,   # 1 if moneyline pick won
                "f5_correct":    None,   # 1 if F5 pick was leading after 5
                "ou_correct":    None,   # 1 if O/U pick was right
                "total_runs":    None,   # actual total runs
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

        lean_side = _bet_side(p_nrfi)
        pred["lean_side"]    = lean_side
        pred["lean_correct"] = 1 if lean_side == outcome else 0

        if verdict in ("BET", "LEAN"):
            won = 1 if lean_side == outcome else 0
            pnl = round(100 / 110, 4) if won else -1.0
        else:
            won = None
            pnl = None

        pred["home_1st_runs"] = h1
        pred["away_1st_runs"] = a1
        pred["outcome"]       = outcome
        pred["won"]           = won
        pred["pnl_units"]     = pnl

        # ── Moneyline resolution ────────────────────────────────────────────
        home_runs = ls.get("home_runs")
        away_runs = ls.get("away_runs")
        if home_runs is not None and away_runs is not None:
            pred["home_runs"] = home_runs
            pred["away_runs"] = away_runs
            pred["total_runs"] = home_runs + away_runs

            ml_pick = pred.get("ml_pick", "")
            if ml_pick and home_runs != away_runs:
                home_team = pred.get("home_team", pred.get("home_abbr", ""))
                ml_picked_home = home_team.lower() in ml_pick.lower()
                home_won = home_runs > away_runs
                pred["ml_correct"] = 1 if (ml_picked_home == home_won) else 0

            # ── F5 resolution ───────────────────────────────────────────────
            home_f5 = ls.get("home_f5")
            away_f5 = ls.get("away_f5")
            f5_pick = pred.get("f5_pick", "")
            if f5_pick and home_f5 is not None and away_f5 is not None and home_f5 != away_f5:
                home_team = pred.get("home_team", pred.get("home_abbr", ""))
                f5_picked_home = home_team.lower() in f5_pick.lower()
                home_led_f5 = home_f5 > away_f5
                pred["f5_correct"] = 1 if (f5_picked_home == home_led_f5) else 0

            # ── Over/Under resolution ───────────────────────────────────────
            ou_line = pred.get("ou_line")
            ou_pick = pred.get("ou_pick", "")
            total = home_runs + away_runs
            if ou_line is not None and ou_pick and total != ou_line:
                actual_over = total > ou_line
                picked_over = "over" in ou_pick.lower()
                pred["ou_correct"] = 1 if (picked_over == actual_over) else 0

        _compute_clv(pred)
        clv_str = ""
        if pred.get("clv_pp") is not None:
            clv_str = f" | CLV {pred['clv_pp']:+.2f}pp"

        # Summary line with all markets
        parts = [f"1st: {a1}-{h1} → {outcome}"]
        if home_runs is not None:
            parts.append(f"Final: {away_runs}-{home_runs}")
            ml_r = pred.get("ml_correct")
            if ml_r is not None:
                parts.append(f"ML:{'✓' if ml_r else '✗'}")
            f5_r = pred.get("f5_correct")
            if f5_r is not None:
                parts.append(f"F5:{'✓' if f5_r else '✗'}")
            ou_r = pred.get("ou_correct")
            if ou_r is not None:
                parts.append(f"O/U:{'✓' if ou_r else '✗'}")

        print(f"  {pred.get('away_abbr','?')} @ {pred.get('home_abbr','?')}: "
              f"{' | '.join(parts)}{clv_str}")
        resolved += 1
        time.sleep(0.3)

    print(f"\n  Resolved {resolved}/{total} games.")
    return predictions


def resolve_pending_bets(max_age_days: int = 14) -> dict:
    """
    Grades nrfi_bets DB rows that have a game_pk but no outcome yet — this is
    what closes the loop for ad-hoc manual website queries (e.g. someone
    typing "Yankees vs Red Sox tonight" in the browser), which _log_prediction
    persists to the DB on every call but which the JSON-file pipeline never
    sees (that pipeline only covers the scheduled daily slate it fetched
    itself). Reuses fetch_linescore, the same MLB Stats API call the JSON
    pipeline's resolve_predictions() uses, so both paths grade identically.

    Skips rows whose game_date is too recent (game may still be in progress)
    or too old (max_age_days — avoids hammering the API for stale unresolved
    rows from data issues; those show up as still-pending in /nrfi-performance).
    """
    from db.database import get_db

    cutoff_old   = (date.today() - timedelta(days=max_age_days)).isoformat()
    cutoff_recent = date.today().isoformat()

    with get_db() as db:
        rows = db.execute(
            """SELECT * FROM nrfi_bets
               WHERE outcome IS NULL AND game_pk IS NOT NULL
                 AND game_date < ? AND game_date >= ?""",
            (cutoff_recent, cutoff_old),
        ).fetchall()

        graded = 0
        for row in rows:
            ls = fetch_linescore(row["game_pk"])
            if ls is None:
                continue

            h1, a1 = ls["home_1st"], ls["away_1st"]
            outcome = 1 if (h1 == 0 and a1 == 0) else 0   # 1=NRFI, 0=YRFI
            bet_side = "NRFI" if row["p_nrfi"] >= 50 else "YRFI"
            actual_side = "NRFI" if outcome == 1 else "YRFI"
            won = 1 if (row["verdict"] in ("BET", "LEAN") and bet_side == actual_side) else None
            pnl = (round(100 / 110, 4) if won else -1.0) if won is not None else None

            home_runs, away_runs = ls.get("home_runs"), ls.get("away_runs")
            ml_correct = f5_correct = ou_correct = None
            if home_runs is not None and away_runs is not None and home_runs != away_runs:
                home_won = home_runs > away_runs
                if row["ml_pick"]:
                    ml_correct = 1 if ((row["home_team"].lower() in row["ml_pick"].lower()) == home_won) else 0
                home_f5, away_f5 = ls.get("home_f5"), ls.get("away_f5")
                if row["f5_pick"] and home_f5 is not None and away_f5 is not None and home_f5 != away_f5:
                    f5_correct = 1 if ((row["home_team"].lower() in row["f5_pick"].lower()) == (home_f5 > away_f5)) else 0
                if row["ou_pick"] and row["ou_line"] is not None:
                    total = home_runs + away_runs
                    if total != row["ou_line"]:
                        ou_correct = 1 if (("over" in row["ou_pick"].lower()) == (total > row["ou_line"])) else 0

            db.execute(
                """UPDATE nrfi_bets SET
                     outcome=?, home_1st_runs=?, away_1st_runs=?, won=?, pnl_units=?,
                     home_runs=?, away_runs=?, ml_correct=?, f5_correct=?, ou_correct=?,
                     resolved_at=?
                   WHERE id=?""",
                (outcome, h1, a1, won, pnl, home_runs, away_runs,
                 ml_correct, f5_correct, ou_correct,
                 datetime.now(timezone.utc).isoformat(), row["id"]),
            )
            graded += 1

    return {"checked": len(rows), "graded": graded}


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

    # ── Live Validation Tracker (cumulative proof-of-edge snapshot) ──────────
    try:
        import nrfi_store
        vt = nrfi_store.validation_tracker()
        def _fmt(v, suffix=""):
            return f"{v}{suffix}" if v is not None else "—"
        lines += [
            "## Live Validation Tracker",
            "",
            "```",
            f"Model games resolved  : {vt['model_games_resolved']}",
            f"Model lean accuracy   : {_fmt(vt['model_lean_accuracy_pct'], '%')}",
            f"Bet plays resolved    : {vt['bet_predictions_resolved']}",
            f"Bet win rate          : {_fmt(vt['bet_win_rate_pct'], '%')}",
            f"CLV-quality verdict   : {vt['clv_quality_verdict']}",
            f"Avg entry lead time   : {_fmt(vt['avg_entry_lead_hrs'], ' hrs')} (n={vt['clv_plays']})",
            f"Avg CLV               : {_fmt(vt['avg_clv_pp'], ' pp')}",
            f"Beat the close        : {_fmt(vt['beat_close_pct'], '%')}",
            f"Stale exclusions      : {vt['stale_exclusions']}",
            "```",
            "",
        ]
    except Exception:
        pass

    # ── Feature store freshness ────────────────────────────────────────────────
    try:
        from fetchers.feature_store import store_freshness
        fresh = store_freshness()
        src = ", ".join(fresh.get("sources", [])) or "none"
        age_label = fresh["label"]
        n = fresh.get("n_pitchers", 0)
        warn = ""
        if fresh.get("is_expired"):
            warn = " ⚠️ EXPIRED (>14d) — FanGraphs features dropped to league mean"
        elif fresh.get("is_stale"):
            warn = " ⚠️ STALE (>7d) — refresh recommended"
        lines += [
            "## Feature Store Freshness",
            "",
            f"Source: **{src}** | Pitchers: {n} | Last built: **{age_label}**{warn}",
            "",
        ]
    except Exception:
        pass

    # ── All games — full model picks (moneyline + F5 + NRFI) ─────────────────
    allg = [p for p in predictions if not p.get("error")]
    if allg:
        lines += [
            "## All Games — Model Picks",
            "",
            "| | Validated | Not yet validated |",
            "|---|---|---|",
            "| **Model** | NRFI — XGBoost + Platt calibration | ML, F5, O/U — Split Poisson + Elo |",
            "| **Proof** | Walk-forward 54.9%, p=0.0049 | Pending independent validation |",
            "",
        ]
        lines += ["| Matchup | Exp. Runs | Moneyline | First 5 | O/U | NRFI ✅ | Best play |",
                  "|---------|-----------|-----------|---------|-----|---------|-----------|"]
        for p in allg:
            away = p.get("away_abbr", "?"); home = p.get("home_abbr", "?")
            def _cell(pick, prob, verd):
                if prob is None:
                    return "—"
                star = " ⭐" if verd in ("BET", "LEAN") else ""
                who = (pick or "").split(" moneyline")[0].split(" to lead")[0]
                return f"{who} {prob:.0f}%{star}"
            ml = _cell(p.get("ml_pick"), p.get("ml_prob"), p.get("ml_verdict"))
            f5 = _cell(p.get("f5_pick"), p.get("f5_prob"), p.get("f5_verdict"))
            ou = _cell(p.get("ou_pick"), p.get("ou_prob"), p.get("ou_verdict"))
            nr = f"{p['p_nrfi']:.0f}%" + (" ⭐" if p.get("verdict") in ("BET","LEAN") else "") if p.get("p_nrfi") else "—"
            exp = f"{p['expected_total']:.1f}" if p.get("expected_total") else "—"
            # Best play = highest-confidence non-SKIP across the four markets
            plays = []
            for mkt, pick, prob, verd in (
                ("ML", p.get("ml_pick"), p.get("ml_prob"), p.get("ml_verdict")),
                ("F5", p.get("f5_pick"), p.get("f5_prob"), p.get("f5_verdict")),
                ("O/U", p.get("ou_pick"), p.get("ou_prob"), p.get("ou_verdict")),
                ("NRFI", ("NRFI" if (p.get("p_nrfi") or 0) >= 50 else "YRFI"),
                 p.get("p_nrfi"), p.get("verdict"))):
                if verd in ("BET", "LEAN") and prob is not None:
                    who = (pick or "").split(" moneyline")[0].split(" to lead")[0]
                    plays.append((prob, f"{verd} {mkt}: {who} {prob:.0f}%"))
            best = max(plays, key=lambda t: t[0])[1] if plays else "no edge — pass"
            lines.append(f"| {away} @ {home} | {exp} | {ml} | {f5} | {ou} | {nr} | {best} |")
        lines += [
            "",
            "*⭐ = model flags a BET or LEAN. "
            "NRFI ✅ = walk-forward validated (54.9%, p=0.0049). "
            "ML, F5, O/U picks are model-generated but not yet independently validated. "
            "\"Best play\" = strongest edge across all four markets.*",
            "",
        ]

    if bets:
        lines += ["## Plays", ""]
        lines += ["| # | Matchup | Starter (H) | Starter (A) | p_NRFI | Verdict | CLV | Model |",
                  "|---|---------|-------------|-------------|--------|---------|-----|-------|"]
        for i, p in enumerate(bets, 1):
            home   = p.get("home_abbr", p.get("home_team","?"))
            away   = p.get("away_abbr", p.get("away_team","?"))
            hs     = p.get("home_starter","TBD")[:20]
            as_    = p.get("away_starter","TBD")[:20]
            pn     = f"{p['p_nrfi']:.1f}%" if p.get("p_nrfi") else "—"
            verd   = p.get("verdict","?")
            conf   = p.get("confidence","")
            verd_s = f"**{verd}**" + (f" ({conf})" if conf else "")
            clv    = f"{p['clv_pp']:+.2f}pp" if p.get("clv_pp") is not None else "—"
            mdl    = p.get("model","?")

            outcome_cell = ""
            if p.get("outcome"):
                won_str = "✅" if p.get("won") else "❌"
                outcome_cell = f" → {p['outcome']} {won_str}"

            lines.append(
                f"| {i} | {away} @ {home}{outcome_cell} | {hs} | {as_} | {pn} | {verd_s} | {clv} | {mdl} |"
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
        resolved_all = [p for p in predictions if p.get("outcome") is not None and not p.get("error")]
        bet_resolved = [p for p in bets if p.get("outcome") is not None]

        # ── All-market accuracy (every game, not just BET/LEAN) ─────────────
        if resolved_all:
            def _accuracy(key):
                graded = [p for p in resolved_all if p.get(key) is not None]
                if not graded:
                    return "—"
                correct = sum(1 for p in graded if p[key] == 1)
                return f"{correct}/{len(graded)} ({correct/len(graded)*100:.0f}%)"
            lines += [
                "## Results — All Markets",
                "",
                "| Market | Accuracy | Status |",
                "|--------|----------|--------|",
                f"| Moneyline | {_accuracy('ml_correct')} | not yet validated |",
                f"| First 5 (F5) | {_accuracy('f5_correct')} | not yet validated |",
                f"| Over/Under | {_accuracy('ou_correct')} | not yet validated |",
                f"| NRFI lean | {_accuracy('lean_correct')} | ✅ validated model |",
                "",
            ]

        if bet_resolved:
            wins   = sum(1 for p in bet_resolved if p.get("won") == 1)
            losses = sum(1 for p in bet_resolved if p.get("won") == 0)
            pnl    = sum(p.get("pnl_units", 0) or 0 for p in bet_resolved)
            lines += [
                "## Results — NRFI Bets",
                "",
                f"**W-L:** {wins}–{losses}  |  **P&L:** {pnl:+.3f} units",
                "",
            ]
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
