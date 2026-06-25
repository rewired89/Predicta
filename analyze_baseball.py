"""
End-to-end baseball analysis pipeline.
query → MLB Stats API → Poisson run model → Elo blend → markets → narrative → result dict
"""
from __future__ import annotations
import env_loader  # noqa: F401 — loads .env on import
import math
import traceback
from datetime import datetime, timezone
from typing import Optional

from db.database import get_db, init_db
from fetchers.baseball import fetch_baseball_context, LEAGUE_AVG_FIP
from fetchers.signals import log_signal
from fetchers.weather import fetch_game_weather, weather_to_signals, team_to_stadium_code
from models.baseball_market import (
    expected_runs_split, compute_baseball_markets,
    platoon_wrc_adjust, LEAGUE_BULLPEN_FIP,
)
from models.elo import EloModel
from models.kelly import kelly_stake


def _elo_from_winpct(win_pct: float) -> float:
    """Seed Elo from current-season win% so the blend uses real team strength."""
    wp = max(0.01, min(0.99, win_pct))
    return 1500.0 - 400.0 * math.log10((1 - wp) / wp)


def _derive_bullpen_fip(team_era: float, starter_fip: float,
                         starter_avg_ip: Optional[float] = None,
                         starter_era: Optional[float] = None) -> float:
    """
    Estimate bullpen quality from team ERA and starter stats.

    ERA decomposition (consistent units throughout):
      bullpen_ERA = (team_ERA × 9 - starter_ERA × avg_ip) / (9 - avg_ip)

    starter_ERA is used instead of starter_FIP because team_ERA measures actual
    runs allowed (including defense), and decomposition requires both sides to
    be in ERA units. starter_FIP is defense-independent and mixes unit systems.
    Falls back to starter_FIP when starter_ERA is unavailable.

    Fallback when avg_ip unknown:
      (team_ERA × 9 - starter_rate × 5) / 4  (classic 5/4 split)

    Clamped to [3.0, 7.5]; returns LEAGUE_BULLPEN_FIP when team_era is missing.
    """
    if not team_era or team_era <= 0:
        return LEAGUE_BULLPEN_FIP
    # Use ERA for decomposition (consistent units); fall back to FIP if ERA unavailable
    decomp_rate = starter_era if (starter_era and starter_era > 0) else starter_fip
    if starter_avg_ip and 0 < starter_avg_ip < 8.5:
        bullpen_innings = 9.0 - starter_avg_ip
        derived = (team_era * 9.0 - decomp_rate * starter_avg_ip) / bullpen_innings
    else:
        derived = (team_era * 9.0 - decomp_rate * 5.0) / 4.0
    return max(3.0, min(derived, 7.5))


def _format_baseball_markets(markets: dict, team_home: str, team_away: str) -> dict:
    def pct(v):
        return round(float(v) * 100, 1)

    result: dict = {}

    ml = markets.get("moneyline", {})
    if ml:
        hp, ap = pct(ml["p_home_win"]), pct(ml["p_away_win"])
        result["moneyline"] = {
            "label": "Moneyline",
            "options": [
                {"label": team_home, "prob": hp, "best": hp >= ap},
                {"label": team_away, "prob": ap, "best": ap > hp},
            ],
        }

    rl = markets.get("run_line", [])
    if rl:
        r = rl[0]
        hp, ap = pct(r["p_home_covers"]), pct(r["p_away_covers"])
        result["run_line"] = {
            "label": "Run Line",
            "options": [
                {"label": f"{team_home} -{r['line']}", "prob": hp, "best": hp >= ap},
                {"label": f"{team_away} +{r['line']}", "prob": ap, "best": ap > hp},
            ],
            "p_push": pct(r["p_push"]),
        }

    totals = markets.get("totals", [])
    if totals:
        result["totals"] = {
            "label": "Game Total (O/U)",
            "lines": [
                {
                    "line":      t["line"],
                    "label":     t["label"],
                    "p_over":    pct(t["p_over"]),
                    "p_under":   pct(t["p_under"]),
                    "best_side": "over" if t["p_over"] >= t["p_under"] else "under",
                }
                for t in totals
            ],
        }

    f5 = markets.get("first_five", {})
    if f5:
        hp, ap = pct(f5["p_home_win"]), pct(f5["p_away_win"])
        result["first_five"] = {
            "label": "First 5 Innings (Starter)",
            "options": [
                {"label": team_home, "prob": hp, "best": hp >= ap},
                {"label": team_away, "prob": ap, "best": ap > hp},
            ],
            "mu_home_f5": f5.get("mu_home_f5"),
            "mu_away_f5": f5.get("mu_away_f5"),
            "note": f5.get("note", ""),
        }

    l4 = markets.get("last_four", {})
    if l4:
        hp, ap = pct(l4["p_home_win"]), pct(l4["p_away_win"])
        result["last_four"] = {
            "label": "Innings 6–9 (Bullpen)",
            "options": [
                {"label": team_home, "prob": hp, "best": hp >= ap},
                {"label": team_away, "prob": ap, "best": ap > hp},
            ],
            "mu_home_l4": l4.get("mu_home_l4"),
            "mu_away_l4": l4.get("mu_away_l4"),
            "note": l4.get("note", ""),
        }

    nrfi = markets.get("nrfi", {})
    if nrfi:
        np_, yp = pct(nrfi["p_nrfi"]), pct(nrfi["p_yrfi"])
        result["nrfi"] = {
            "label": "NRFI / YRFI",
            "options": [
                {"label": "NRFI (No Run First Inning)", "prob": np_, "best": np_ >= yp},
                {"label": "YRFI (Yes Run First Inning)", "prob": yp, "best": yp > np_},
            ],
            "note": nrfi.get("note", ""),
        }

    tt_home = markets.get("team_total_home", [])
    tt_away = markets.get("team_total_away", [])
    if tt_home or tt_away:
        result["team_totals"] = {
            "label": "Team Totals",
            "home": {
                "team":  team_home,
                "mu":    markets.get("mu_home"),
                "lines": [{"line": t["line"], "p_over": pct(t["p_over"]),
                            "p_under": pct(t["p_under"])} for t in tt_home],
            },
            "away": {
                "team":  team_away,
                "mu":    markets.get("mu_away"),
                "lines": [{"line": t["line"], "p_over": pct(t["p_over"]),
                            "p_under": pct(t["p_under"])} for t in tt_away],
            },
        }

    return result


def run_baseball_analysis(user_query: str, bankroll: float = 1000.0) -> dict:
    """
    Full baseball pipeline:
    1. Parse query (Claude)
    2. Fetch MLB Stats API — starters, team stats, park factor
    3. Compute expected runs (Poisson model)
    4. Blend with Elo seeded from win%
    5. Compute all markets
    6. Persist match + signals + prediction to DB
    7. Kelly stake sizing
    8. Generate narrative (Claude)
    """
    init_db()
    steps: list[dict] = []

    # ── 1. Parse ─────────────────────────────────────────────────────────────
    try:
        from ai_agent_baseball import parse_baseball_query
        parsed = parse_baseball_query(user_query)
        steps.append({"step": "parse_query", "status": "ok", "data": parsed})
    except Exception as exc:
        return {"error": f"Could not parse query: {exc}", "steps": steps}

    team_a_raw = parsed.get("team_a", "Team A")
    team_b_raw = parsed.get("team_b", "Team B")
    game_date  = parsed.get("date") or datetime.now(timezone.utc).date().isoformat()

    # ── 2. Fetch MLB data (with AI fallback when API is unreachable) ────────────
    context: dict = {}
    ai_fallback = False

    try:
        context = fetch_baseball_context(team_a_raw, team_b_raw, game_date)
        if "error" in context:
            raise ValueError(context["error"])
        steps.append({"step": "mlb_fetch", "status": "ok",
                      "sources": context.get("sources", [])})
    except Exception as exc:
        steps.append({"step": "mlb_fetch", "status": "fallback",
                      "error": str(exc),
                      "note": "ESPN API unreachable — using AI signal estimates from training knowledge."})
        ai_fallback = True

    if ai_fallback:
        # Claude fills in wRC+, FIP, park factor from training knowledge
        try:
            from ai_agent_baseball import interpret_baseball_signals
            sigs = interpret_baseball_signals(team_a_raw, team_b_raw,
                                              parsed.get("notes", ""))
            steps.append({"step": "ai_signals", "status": "ok", "data": sigs})
        except Exception as exc2:
            steps.append({"step": "ai_signals", "status": "error", "error": str(exc2)})
            sigs = {}

        home_key   = sigs.get("home_team", "team_a")
        is_home_a  = (home_key == "team_a")
        pf         = float(sigs.get("park_factor", 1.0))
        sig_a      = sigs.get("team_a", {})
        sig_b      = sigs.get("team_b", {})
        team_a     = team_a_raw
        team_b     = team_b_raw
        team_home  = team_a_raw if is_home_a else team_b_raw
        team_away  = team_b_raw if is_home_a else team_a_raw
        park_factor = pf
        wrc_a      = float(sig_a.get("wrc_plus", 100) or 100)
        wrc_b      = float(sig_b.get("wrc_plus", 100) or 100)
        fip_a      = float(sig_a.get("starter_fip", LEAGUE_AVG_FIP) or LEAGUE_AVG_FIP)
        fip_b      = float(sig_b.get("starter_fip", LEAGUE_AVG_FIP) or LEAGUE_AVG_FIP)
        win_pct_a  = float(sig_a.get("record_win_pct", 0.5) or 0.5)
        win_pct_b  = float(sig_b.get("record_win_pct", 0.5) or 0.5)
        starter_a  = {"name": sig_a.get("starter_name", "Unknown"),
                      "fip": fip_a, "era": float(sig_a.get("starter_era", LEAGUE_AVG_FIP) or LEAGUE_AVG_FIP),
                      "whip": 0.0, "k9": 0.0, "bb9": 0.0,
                      "innings_pitched": 0, "recent_games": [], "throws": "R"}
        starter_b  = {"name": sig_b.get("starter_name", "Unknown"),
                      "fip": fip_b, "era": float(sig_b.get("starter_era", LEAGUE_AVG_FIP) or LEAGUE_AVG_FIP),
                      "whip": 0.0, "k9": 0.0, "bb9": 0.0,
                      "innings_pitched": 0, "recent_games": [], "throws": "R"}
        record_a   = {"win_pct": win_pct_a, "games_played": 0}
        record_b   = {"win_pct": win_pct_b, "games_played": 0}
        hitting_a  = {"wrc_plus": wrc_a, "ops": 0.0, "runs_per_game": 0.0}
        hitting_b  = {"wrc_plus": wrc_b, "ops": 0.0, "runs_per_game": 0.0}
        context = {
            "team_a": {"name": team_a, "is_home": is_home_a, "hitting": hitting_a,
                       "starter": starter_a, "record": record_a,
                       "team_pitching": {"era": LEAGUE_BULLPEN_FIP}},
            "team_b": {"name": team_b, "is_home": not is_home_a, "hitting": hitting_b,
                       "starter": starter_b, "record": record_b,
                       "team_pitching": {"era": LEAGUE_BULLPEN_FIP}},
            "game": {"date": game_date, "venue": "", "park_factor": pf,
                     "home_team": team_home, "away_team": team_away},
            "sources": [{"label": "AI signal estimation",
                         "url": "", "snippet": sigs.get("notes", "")}],
        }
    else:
        team_a      = context["team_a"]["name"]
        team_b      = context["team_b"]["name"]
        is_home_a   = context["team_a"]["is_home"]
        park_factor = context["game"]["park_factor"]
        team_home   = context["game"]["home_team"]
        team_away   = context["game"]["away_team"]
        hitting_a   = context["team_a"].get("hitting", {})
        hitting_b   = context["team_b"].get("hitting", {})
        starter_a   = context["team_a"].get("starter", {})
        starter_b   = context["team_b"].get("starter", {})
        record_a    = context["team_a"].get("record", {})
        record_b    = context["team_b"].get("record", {})
        wrc_a = hitting_a.get("wrc_plus") or 100
        wrc_b = hitting_b.get("wrc_plus") or 100
        fip_a = starter_a.get("fip") or LEAGUE_AVG_FIP
        fip_b = starter_b.get("fip") or LEAGUE_AVG_FIP

    # ── 3. Bullpen FIP derivation + platoon adjustment ────────────────────────
    # avg IP needed here (bullpen derivation) AND below (expected_runs_split).
    def _avg_ip(starter: dict) -> Optional[float]:
        ip = starter.get("innings_pitched") or 0
        gs = starter.get("games_started") or 0
        return round(ip / gs, 2) if gs >= 3 else None

    avg_ip_a = _avg_ip(starter_a)
    avg_ip_b = _avg_ip(starter_b)

    team_pit_a    = context["team_a"].get("team_pitching", {})
    team_pit_b    = context["team_b"].get("team_pitching", {})
    era_a = starter_a.get("era") or None   # None → falls back to FIP in decomposition
    era_b = starter_b.get("era") or None
    bullpen_fip_a = _derive_bullpen_fip(team_pit_a.get("era", 0), fip_a, avg_ip_a, era_a)
    bullpen_fip_b = _derive_bullpen_fip(team_pit_b.get("era", 0), fip_b, avg_ip_b, era_b)

    throws_a = starter_a.get("throws", "R")
    throws_b = starter_b.get("throws", "R")
    # team_a bats against team_b's starter (throws_b); team_b bats against team_a's starter (throws_a)
    wrc_a_adj = platoon_wrc_adjust(wrc_a, throws_b)
    wrc_b_adj = platoon_wrc_adjust(wrc_b, throws_a)

    steps.append({"step": "bullpen_platoon", "status": "ok",
                  "bullpen_fip_a": round(bullpen_fip_a, 2),
                  "bullpen_fip_b": round(bullpen_fip_b, 2),
                  "throws_a": throws_a, "throws_b": throws_b,
                  "wrc_a_adj": round(wrc_a_adj, 1), "wrc_b_adj": round(wrc_b_adj, 1)})

    # ── 4. Expected runs (split F5 / L4) ─────────────────────────────────────
    # Home bats against away starter (F5) + away bullpen (L4); vice versa for away.
    if is_home_a:
        mu_home_f5, mu_home_l4 = expected_runs_split(wrc_a_adj, fip_b, bullpen_fip_b, park_factor, True,  avg_ip_b)
        mu_away_f5, mu_away_l4 = expected_runs_split(wrc_b_adj, fip_a, bullpen_fip_a, park_factor, False, avg_ip_a)
    else:
        mu_home_f5, mu_home_l4 = expected_runs_split(wrc_b_adj, fip_a, bullpen_fip_a, park_factor, True,  avg_ip_a)
        mu_away_f5, mu_away_l4 = expected_runs_split(wrc_a_adj, fip_b, bullpen_fip_b, park_factor, False, avg_ip_b)

    mu_home = mu_home_f5 + mu_home_l4
    mu_away = mu_away_f5 + mu_away_l4

    steps.append({"step": "run_model", "status": "ok",
                  "mu_home": round(mu_home, 2), "mu_away": round(mu_away, 2),
                  "mu_home_f5": round(mu_home_f5, 2), "mu_home_l4": round(mu_home_l4, 2),
                  "mu_away_f5": round(mu_away_f5, 2), "mu_away_l4": round(mu_away_l4, 2)})

    # ── 5. Compute markets ────────────────────────────────────────────────────
    markets_raw = compute_baseball_markets(
        mu_home, mu_away, mu_home_f5, mu_away_f5, mu_home_l4, mu_away_l4
    )
    ml          = markets_raw["moneyline"]
    prob_home   = ml["p_home_win"]
    prob_away   = ml["p_away_win"]

    # Map back to team_a / team_b perspective
    prob_a = prob_home if is_home_a else prob_away
    prob_b = prob_away if is_home_a else prob_home

    # ── 6. Elo blend (30% weight) ────────────────────────────────────────────
    elo_explanation = ""
    try:
        elo = EloModel()
        if record_a.get("games_played", 0) >= 10:
            elo.set_rating(f"MLB:{team_a}", _elo_from_winpct(record_a["win_pct"]))
        if record_b.get("games_played", 0) >= 10:
            elo.set_rating(f"MLB:{team_b}", _elo_from_winpct(record_b["win_pct"]))
        elo_a, elo_b = elo.win_probability(f"MLB:{team_a}", f"MLB:{team_b}")
        prob_a = 0.7 * prob_a + 0.3 * elo_a
        prob_b = 0.7 * prob_b + 0.3 * elo_b
        total  = prob_a + prob_b
        prob_a /= total
        prob_b /= total
        elo_explanation = (
            f" Elo (from win%): {team_a} {elo_a*100:.1f}% / {team_b} {elo_b*100:.1f}%."
        )
        steps.append({"step": "elo_blend", "status": "ok",
                      "elo_a": round(elo_a, 3), "elo_b": round(elo_b, 3)})
    except Exception as exc:
        steps.append({"step": "elo_blend", "status": "skipped", "error": str(exc)})

    home_starter_name = starter_a["name"] if is_home_a else starter_b["name"]
    away_starter_name = starter_b["name"] if is_home_a else starter_a["name"]
    home_starter_fip  = fip_b if is_home_a else fip_a   # home bats vs away starter
    away_starter_fip  = fip_a if is_home_a else fip_b
    home_starter_hand = throws_b if is_home_a else throws_a
    away_starter_hand = throws_a if is_home_a else throws_b
    home_bp_fip = bullpen_fip_b if is_home_a else bullpen_fip_a
    away_bp_fip = bullpen_fip_a if is_home_a else bullpen_fip_b

    explanation = (
        f"Split Poisson: F5 μ_home={mu_home_f5:.2f}+L4 {mu_home_l4:.2f}={mu_home:.2f} runs; "
        f"F5 μ_away={mu_away_f5:.2f}+L4 {mu_away_l4:.2f}={mu_away:.2f} runs. "
        f"Park factor={park_factor}. "
        f"Home starter: {home_starter_name} [{home_starter_hand}HP] FIP {home_starter_fip:.2f}, "
        f"bullpen FIP {home_bp_fip:.2f}. "
        f"Away starter: {away_starter_name} [{away_starter_hand}HP] FIP {away_starter_fip:.2f}, "
        f"bullpen FIP {away_bp_fip:.2f}."
        + elo_explanation
    )

    # ── 6.5. Weather signals (stored only; NOT applied to run model yet) ────
    # Enable the wind/temp adjustment in expected_runs_split after 50+
    # baseball predictions confirm the effect on scoring.
    weather_data = None
    try:
        stadium_code = team_to_stadium_code(team_home)
        if stadium_code:
            weather_data = fetch_game_weather(stadium_code, game_date)
    except Exception:
        pass
    weather_sigs = weather_to_signals(weather_data)

    # ── 7. Persist ───────────────────────────────────────────────────────────
    match_id: Optional[int] = None
    try:
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO matches
                       (sport, league, participant_a, participant_b,
                        scheduled_at, venue, neutral_site)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "baseball", "MLB",
                    team_a, team_b,
                    f"{game_date}T12:00:00",
                    context["game"].get("venue", ""),
                    0,
                ),
            )
            match_id = cur.lastrowid

        signals_to_log = [
            ("wrc_plus",        team_a,    wrc_a),
            ("wrc_plus",        team_b,    wrc_b),
            ("starter_fip",     team_a,    fip_a),
            ("starter_fip",     team_b,    fip_b),
            ("bullpen_fip",     team_a,    bullpen_fip_a),
            ("bullpen_fip",     team_b,    bullpen_fip_b),
            ("park_factor",     None,      park_factor),
            ("mu_runs",         team_home, mu_home),
            ("mu_runs",         team_away, mu_away),
            ("mu_runs_f5",      team_home, mu_home_f5),
            ("mu_runs_f5",      team_away, mu_away_f5),
        ]
        if avg_ip_a is not None:
            signals_to_log.append(("starter_avg_ip", team_a, avg_ip_a))
        if avg_ip_b is not None:
            signals_to_log.append(("starter_avg_ip", team_b, avg_ip_b))
        for sig_name, participant, val in signals_to_log:
            log_signal(match_id, sig_name, participant,
                       signal_value=float(val), source="mlb_api")

        # Weather signals (participant=None → game-level, not team-specific)
        forecast_ts = weather_data.get("forecast_time", "") if weather_data else None
        weather_source = "openweather" if weather_data else "fallback"
        for sig_name, sig_val in weather_sigs.items():
            log_signal(match_id, sig_name, None,
                       signal_value=sig_val,
                       signal_text=forecast_ts,
                       source=weather_source)

        with get_db() as conn:
            conn.execute(
                """INSERT INTO predictions
                       (match_id, method, prob_a, prob_b, prob_draw, explanation, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    match_id, "baseball_v2",
                    round(prob_a, 6), round(prob_b, 6), 0.0,
                    explanation,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        steps.append({"step": "persist", "status": "ok", "match_id": match_id})
    except Exception as exc:
        steps.append({"step": "persist", "status": "error", "error": str(exc),
                      "trace": traceback.format_exc()})

    # ── 8. Kelly stake ───────────────────────────────────────────────────────
    kelly = kelly_stake(prob_a, 1.909, bankroll)

    # ── 9. Narrative ─────────────────────────────────────────────────────────
    narrative = ""
    try:
        from ai_agent_baseball import generate_baseball_narrative
        narrative_context = {
            **context,
            "mu_home": round(mu_home, 2),
            "mu_away": round(mu_away, 2),
        }
        narrative = generate_baseball_narrative(
            team_home, team_away,
            prob_home, prob_away,
            explanation, narrative_context,
        )
        steps.append({"step": "narrative", "status": "ok"})
    except Exception as exc:
        narrative = (
            f"{team_home} win probability {prob_home*100:.1f}%, "
            f"{team_away} {prob_away*100:.1f}%. "
            f"Expected runs: home {mu_home:.1f}, away {mu_away:.1f}."
        )
        steps.append({"step": "narrative", "status": "error", "error": str(exc)})

    # ── Format ────────────────────────────────────────────────────────────────
    formatted_markets = _format_baseball_markets(markets_raw, team_home, team_away)

    # Starters in home/away order for easy frontend rendering
    home_starter = starter_a if is_home_a else starter_b
    away_starter = starter_b if is_home_a else starter_a
    home_hitting  = hitting_a if is_home_a else hitting_b
    away_hitting  = hitting_b if is_home_a else hitting_a
    home_record   = record_a  if is_home_a else record_b
    away_record   = record_b  if is_home_a else record_a

    home_bp_fip_out = bullpen_fip_b if is_home_a else bullpen_fip_a
    away_bp_fip_out = bullpen_fip_a if is_home_a else bullpen_fip_b

    return {
        "match_id":   match_id,
        "team_a":     team_a,
        "team_b":     team_b,
        "team_home":  team_home,
        "team_away":  team_away,
        "sport":      "baseball",
        "date":       game_date,
        "venue":      context["game"].get("venue", ""),
        "park_factor": park_factor,
        "narrative":  narrative,
        "prob_a":     round(prob_a * 100, 1),
        "prob_b":     round(prob_b * 100, 1),
        "prob_draw":  0.0,
        "mu_home":    round(mu_home, 2),
        "mu_away":    round(mu_away, 2),
        "mu_home_f5": round(mu_home_f5, 2),
        "mu_away_f5": round(mu_away_f5, 2),
        "mu_home_l4": round(mu_home_l4, 2),
        "mu_away_l4": round(mu_away_l4, 2),
        "starters": {
            "home": {
                "team":            team_home,
                "name":            home_starter.get("name", "TBD"),
                "throws":          home_starter.get("throws", "R"),
                "fip":             home_starter.get("fip"),
                "era":             home_starter.get("era"),
                "whip":            home_starter.get("whip"),
                "k9":              home_starter.get("k9"),
                "bb9":             home_starter.get("bb9"),
                "innings_pitched": home_starter.get("innings_pitched"),
                "recent_games":    home_starter.get("recent_games", []),
                "bullpen_fip":     round(home_bp_fip_out, 2),
            },
            "away": {
                "team":            team_away,
                "name":            away_starter.get("name", "TBD"),
                "throws":          away_starter.get("throws", "R"),
                "fip":             away_starter.get("fip"),
                "era":             away_starter.get("era"),
                "whip":            away_starter.get("whip"),
                "k9":              away_starter.get("k9"),
                "bb9":             away_starter.get("bb9"),
                "innings_pitched": away_starter.get("innings_pitched"),
                "recent_games":    away_starter.get("recent_games", []),
                "bullpen_fip":     round(away_bp_fip_out, 2),
            },
        },
        "team_stats": {
            "home": {
                "team":       team_home,
                "record":     home_record,
                "wrc_plus":   home_hitting.get("wrc_plus"),
                "ops":        home_hitting.get("ops"),
                "runs_per_game": home_hitting.get("runs_per_game"),
            },
            "away": {
                "team":       team_away,
                "record":     away_record,
                "wrc_plus":   away_hitting.get("wrc_plus"),
                "ops":        away_hitting.get("ops"),
                "runs_per_game": away_hitting.get("runs_per_game"),
            },
        },
        "model_explanation": explanation,
        "kelly_note":        kelly.get("note", ""),
        "markets":           formatted_markets,
        "raw_sources":       context.get("sources", []),
        "steps":             steps,
    }
