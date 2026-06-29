"""
End-to-end soccer analysis pipeline.

  query → Understat (xG/npxG + venue + situation splits)
        → FBref (PSxG, PPDA, possession, aerial)
        → Dixon-Coles open-play+set-piece split Poisson
        → Elo blend
        → market comparison + Kelly stake
        → narrative
        → result dict

The legacy /analyze endpoint in analyze.py is unchanged. This module powers
the new /analyze-soccer endpoint and replaces the AI-hallucinated xG values
with live Understat + FBref data when those sources are reachable.
"""
from __future__ import annotations
import env_loader  # noqa: F401 — loads .env on import
import math
import traceback
from datetime import datetime, timezone
from typing import Optional

from db.database import get_db, init_db
from fetchers.signals import log_signal
from fetchers.understat import enrich_soccer_teams
from fetchers.fbref import enrich_soccer_advanced
from models.dixon_coles import strengths_from_xg, predict_xg
from models.markets import compute_all_markets
from models.elo import EloModel
from models.kelly import kelly_stake, american_to_decimal, market_edge_summary
import models.soccer_leagues as leagues


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe_float(val, default=None):
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _current_season() -> int:
    """Understat season number = start year. June–July rolls to the new season."""
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


def _aerial_index(aerials_won_pct: Optional[float]) -> float:
    """
    Convert aerial-duel % to a multiplier on set-piece μ.
    League average ~50%. ±5pp → ±10% set-piece scoring; capped [0.85, 1.20].
    """
    if aerials_won_pct is None:
        return 1.0
    delta = (aerials_won_pct - 50.0) / 50.0
    mult = 1.0 + delta * 1.0  # 1pp swing = 2% mult swing
    return max(0.85, min(1.20, mult))


def _set_piece_share(team_sit: dict, league: Optional[str]) -> float:
    """
    Fraction of μ coming from set pieces (corners + free kicks + indirect set play).
    Penalties stay outside open-play but are noise so we exclude them from the set
    bucket too — penalty-stripped scoring is what we model.
    """
    league_default = 1.0 - leagues.open_play_share(league)
    if not team_sit:
        return league_default
    s = team_sit.get("set_piece_xg_share")
    if s is None:
        return league_default
    return max(0.05, min(0.45, float(s)))


def _build_strengths(
    team_data: dict,
    opp_data: dict,
    league_avg_goals: float,
    is_home: bool,
) -> dict:
    """
    Convert Understat enriched dict → Dixon-Coles attack/defense.

    Uses venue split when available (home_xg_per_game / home_xga_per_game etc.),
    falls back to overall xG. Picks npxG when present (penalty-stripped).
    """
    season_for     = team_data.get("npxg_per_game") or team_data.get("xg_per_game")
    season_against = team_data.get("npxga_per_game") or team_data.get("xga_per_game")
    recent_for     = team_data.get("recent_xg_per_game")
    recent_against = team_data.get("recent_xga_per_game")

    if is_home:
        venue_for     = team_data.get("home_npxg_per_game")  or team_data.get("home_xg_per_game")
        venue_against = team_data.get("home_npxga_per_game") or team_data.get("home_xga_per_game")
        venue_matches = team_data.get("home_matches", 0)
    else:
        venue_for     = team_data.get("away_npxg_per_game")  or team_data.get("away_xg_per_game")
        venue_against = team_data.get("away_npxga_per_game") or team_data.get("away_xga_per_game")
        venue_matches = team_data.get("away_matches", 0)

    return strengths_from_xg(
        season_xg_for      = season_for,
        season_xg_against  = season_against,
        recent_xg_for      = recent_for,
        recent_xg_against  = recent_against,
        venue_xg_for       = venue_for,
        venue_xg_against   = venue_against,
        league_avg_goals   = league_avg_goals,
        matches_played     = team_data.get("matches", 0),
        venue_matches      = venue_matches,
        goal_overperform   = team_data.get("goal_overperform") or 1.0,
        is_home            = is_home,
    )


def _build_explanation(
    home_team: str, away_team: str,
    dc: dict, str_h: dict, str_a: dict,
    league_avg: float,
    keeper_h: Optional[float], keeper_a: Optional[float],
    ppda_h: Optional[float], ppda_a: Optional[float],
    elo_h: float, elo_a: float,
) -> str:
    bits = [
        f"Dixon-Coles split Poisson: μ_{home_team}={dc['mu_home']:.2f} "
        f"(open {dc['mu_home_open']:.2f} + set {dc['mu_home_set']:.2f}), "
        f"μ_{away_team}={dc['mu_away']:.2f} "
        f"(open {dc['mu_away_open']:.2f} + set {dc['mu_away_set']:.2f}).",
        f"League avg {league_avg:.2f}; attack/defense {home_team}=({str_h['attack']:.2f},{str_h['defense']:.2f}), "
        f"{away_team}=({str_a['attack']:.2f},{str_a['defense']:.2f}).",
    ]
    if keeper_h is not None or keeper_a is not None:
        bits.append(
            f"GK quality (PSxG-GA/90): {home_team}={keeper_h or 0:+.2f}, {away_team}={keeper_a or 0:+.2f}."
        )
    if ppda_h is not None or ppda_a is not None:
        bits.append(
            f"Pressing PPDA proxy: {home_team}={ppda_h or 0:.1f}, {away_team}={ppda_a or 0:.1f} (lower = more pressing)."
        )
    bits.append(f"Elo win-prob: {home_team} {elo_h*100:.1f}% / {away_team} {elo_a*100:.1f}%.")
    return " ".join(bits)


def _bet_recommendations(
    prob_home: float, prob_draw: float, prob_away: float,
    home_team: str, away_team: str,
    edge_summary: Optional[dict],
) -> list[dict]:
    """
    Translate model probs (+ optional market edge) into actionable advice.

    Two-way "winner / no draw" verdict: if a clear side has ≥58% conditional
    win probability AND a positive edge against the market (when odds are
    supplied) we surface a BET. Otherwise PASS.
    """
    decisive = prob_home + prob_away
    p_home_no_draw = prob_home / decisive if decisive else 0.5
    p_away_no_draw = prob_away / decisive if decisive else 0.5
    leader_no_draw = max(p_home_no_draw, p_away_no_draw)
    leader_team    = home_team if p_home_no_draw >= p_away_no_draw else away_team

    recs: list[dict] = []
    if edge_summary and edge_summary.get("has_real_odds"):
        edge_h = edge_summary["edge_a"]
        edge_a = edge_summary["edge_b"]
        best_edge = max(edge_h, edge_a)
        best_side = home_team if edge_h >= edge_a else away_team
        if best_edge >= 0.03:
            verdict = "BET"
            confidence = "high" if best_edge >= 0.06 else "medium"
            reasons = [f"+{best_edge*100:.1f}pp edge vs market on {best_side}"]
            skip = ""
        elif best_edge >= 0.005:
            verdict = "LEAN"
            confidence = "low"
            reasons = [f"+{best_edge*100:.1f}pp slight edge on {best_side}"]
            skip = ""
        else:
            verdict = "PASS"
            confidence = None
            reasons = []
            skip = f"No 3pp edge vs market (best {best_edge*100:.1f}pp on {best_side})"
        recs.append({
            "market":      "Match Winner (vs market)",
            "verdict":     verdict,
            "bet":         f"{best_side} moneyline" if verdict in ("BET", "LEAN") else "",
            "edge_pp":     round(best_edge * 100, 2),
            "confidence":  confidence,
            "reasons":     reasons,
            "skip_reason": skip,
        })
    else:
        if leader_no_draw >= 0.62:
            verdict = "BET"
            confidence = "high" if leader_no_draw >= 0.70 else "medium"
            reasons = [f"{leader_team} {leader_no_draw*100:.1f}% in winner-push-if-tied"]
            skip = ""
        elif leader_no_draw >= 0.55:
            verdict = "LEAN"
            confidence = "low"
            reasons = [f"{leader_team} marginal favorite ({leader_no_draw*100:.1f}%)"]
            skip = ""
        else:
            verdict = "PASS"
            confidence = None
            reasons = []
            skip = f"Coin-flip range — {leader_team} only {leader_no_draw*100:.1f}%"
        recs.append({
            "market":      "Winner (push if draw)",
            "verdict":     verdict,
            "bet":         f"{leader_team}" if verdict in ("BET", "LEAN") else "",
            "model_prob":  round(leader_no_draw * 100, 1),
            "threshold":   62.0,
            "confidence":  confidence,
            "reasons":     reasons,
            "skip_reason": skip,
        })
    return recs


def _format_markets(raw: dict, home: str, away: str) -> dict:
    """Frontend-friendly market dict — mirrors analyze.py shape but home/away keyed."""
    def pct(v: float) -> float:
        return round(float(v) * 100, 1)

    out: dict = {}
    mr2 = raw.get("match_result_2up", {})
    if mr2:
        out["match_result_2up"] = {
            "label": "Match Result – 2 Up",
            "options": [
                {"label": f"{home} by 2+", "prob": pct(mr2.get("home_win_2up", 0))},
                {"label": f"{away} by 2+", "prob": pct(mr2.get("away_win_2up", 0))},
                {"label": "Neither",       "prob": pct(mr2.get("not_2up", 0))},
            ],
        }
    cs = raw.get("correct_score", [])
    if cs:
        out["correct_score"] = {
            "label": "Correct Score (top 6)",
            "scores": [
                {"label": f"{home} {s['score_home']}–{s['score_away']} {away}",
                 "prob": pct(s["prob"])}
                for s in cs[:6]
            ],
        }
    sp = raw.get("spread", [])
    if sp:
        out["spread"] = {
            "label": "Asian Handicap",
            "lines": [
                {"line": s["line"], "label": s["label"],
                 "p_home_covers": pct(s["p_home_covers"]),
                 "p_away_covers": pct(s["p_away_covers"]),
                 "p_push":        pct(s["p_push"])}
                for s in sp
            ],
        }
    wp = raw.get("winner_push_if_tied", {})
    if wp:
        out["winner_push_if_tied"] = {
            "label": "Winner (Draw No Bet)",
            "options": [
                {"label": home, "prob": pct(wp.get("p_home_no_draw", 0))},
                {"label": away, "prob": pct(wp.get("p_away_no_draw", 0))},
            ],
            "draw_prob": pct(wp.get("p_draw", 0)),
        }
    return out


# ── Main entry point ─────────────────────────────────────────────────────────

def run_soccer_analysis(user_query: str, bankroll: float = 1000.0) -> dict:
    """
    Full soccer pipeline.
      1. Parse query → home/away/league/season/odds via Claude
      2. Enrich via Understat (xG + venue + situation)
      3. Enrich via FBref (PSxG, PPDA, possession, aerial)
      4. Build attack/defense strengths (npxG, blended, shrunk)
      5. Run Dixon-Coles open-play + set-piece split Poisson
      6. Blend with Elo (65/35)
      7. Compute markets + bet recs (with market comparison if odds present)
      8. Persist match + signals + prediction
      9. Narrate
    """
    init_db()
    steps: list[dict] = []

    # ── 1. Parse ─────────────────────────────────────────────────────────────
    try:
        from ai_agent_soccer import parse_soccer_query
        parsed = parse_soccer_query(user_query)
        steps.append({"step": "parse_query", "status": "ok", "data": parsed})
    except Exception as exc:
        return {"error": f"Could not parse query: {exc}", "steps": steps}

    home_team = parsed.get("home_team") or "Home"
    away_team = parsed.get("away_team") or "Away"
    league    = parsed.get("league")
    season    = parsed.get("season") or _current_season()
    neutral   = bool(parsed.get("neutral", False))
    match_date = parsed.get("date") or datetime.now(timezone.utc).date().isoformat()
    notes      = parsed.get("notes") or ""

    # Optional market odds
    def _as_decimal(american, decimal_):
        if decimal_ is not None:
            try: return float(decimal_)
            except Exception: pass
        if american is not None:
            try: return american_to_decimal(float(american))
            except Exception: pass
        return None

    odds_home = _as_decimal(parsed.get("odds_home_american"), parsed.get("odds_home_decimal"))
    odds_draw = _as_decimal(parsed.get("odds_draw_american"), parsed.get("odds_draw_decimal"))
    odds_away = _as_decimal(parsed.get("odds_away_american"), parsed.get("odds_away_decimal"))
    has_odds = (odds_home is not None and odds_away is not None)

    # ── 2. Understat enrichment ─────────────────────────────────────────────
    enriched: dict = {"home": {}, "away": {}, "league_avg_goals": None}
    ai_fallback = False
    if league:
        try:
            enriched = enrich_soccer_teams(home_team, away_team, league, int(season))
            steps.append({
                "step": "understat_enrich",
                "status": "ok" if (enriched["home"] and enriched["away"]) else "partial",
                "league_avg_goals": enriched.get("league_avg_goals"),
                "home_fields": len(enriched["home"]),
                "away_fields": len(enriched["away"]),
            })
            # Try season-1 fallback if mid-season data is missing
            if not enriched["home"] and not enriched["away"]:
                fb_season = int(season) - 1
                enriched = enrich_soccer_teams(home_team, away_team, league, fb_season)
                if enriched["home"] or enriched["away"]:
                    steps.append({"step": "understat_enrich_fallback", "status": "ok",
                                  "season_used": fb_season})
        except Exception as exc:
            steps.append({"step": "understat_enrich", "status": "error", "error": str(exc)})

    if not enriched["home"] and not enriched["away"]:
        ai_fallback = True
        try:
            from ai_agent_soccer import interpret_soccer_signals_fallback
            ai_sigs = interpret_soccer_signals_fallback(home_team, away_team, notes)
            steps.append({"step": "ai_fallback_signals", "status": "ok"})
            enriched = {
                "home": {
                    "xg_per_game":         ai_sigs.get("home", {}).get("season_xg_for"),
                    "xga_per_game":        ai_sigs.get("home", {}).get("season_xg_against"),
                    "recent_xg_per_game":  ai_sigs.get("home", {}).get("recent_xg_for"),
                    "recent_xga_per_game": ai_sigs.get("home", {}).get("recent_xg_against"),
                    "goal_overperform":    ai_sigs.get("home", {}).get("goal_overperform") or 1.0,
                    "matches":             19,
                },
                "away": {
                    "xg_per_game":         ai_sigs.get("away", {}).get("season_xg_for"),
                    "xga_per_game":        ai_sigs.get("away", {}).get("season_xg_against"),
                    "recent_xg_per_game":  ai_sigs.get("away", {}).get("recent_xg_for"),
                    "recent_xga_per_game": ai_sigs.get("away", {}).get("recent_xg_against"),
                    "goal_overperform":    ai_sigs.get("away", {}).get("goal_overperform") or 1.0,
                    "matches":             19,
                },
                "league_avg_goals": ai_sigs.get("league_avg_goals"),
            }
        except Exception as exc:
            steps.append({"step": "ai_fallback_signals", "status": "error", "error": str(exc)})

    # ── 3. FBref enrichment ──────────────────────────────────────────────────
    fbref: dict = {"home": {}, "away": {}}
    if league:
        try:
            fbref = enrich_soccer_advanced(home_team, away_team, league, int(season))
            steps.append({
                "step": "fbref_enrich",
                "status": "ok" if (fbref["home"] or fbref["away"]) else "partial",
                "home_fields": len(fbref["home"]),
                "away_fields": len(fbref["away"]),
            })
        except Exception as exc:
            steps.append({"step": "fbref_enrich", "status": "error", "error": str(exc)})

    # ── 4. Determine league average + home advantage ────────────────────────
    league_avg_goals = enriched.get("league_avg_goals") or leagues.league_avg_goals(league)
    if league_avg_goals <= 0:
        league_avg_goals = 1.40
    league_ha = 1.0 if neutral else leagues.home_advantage(league)

    # ── 5. Build attack/defense strengths ───────────────────────────────────
    str_h = _build_strengths(enriched["home"], enriched["away"], league_avg_goals, is_home=True)
    str_a = _build_strengths(enriched["away"], enriched["home"], league_avg_goals, is_home=False)
    steps.append({
        "step": "strengths",
        "status": "ok",
        "home_attack":  round(str_h["attack"], 3),
        "home_defense": round(str_h["defense"], 3),
        "away_attack":  round(str_a["attack"], 3),
        "away_defense": round(str_a["defense"], 3),
        "league_avg_goals": round(league_avg_goals, 3),
    })

    # ── 6. Set-piece + GK + aerial layer ────────────────────────────────────
    set_share_h = _set_piece_share(enriched["home"], league)
    set_share_a = _set_piece_share(enriched["away"], league)
    aerial_h = _aerial_index(fbref["home"].get("aerials_won_pct"))
    aerial_a = _aerial_index(fbref["away"].get("aerials_won_pct"))
    # Home's set-piece μ is amplified if AWAY's aerial defense is weak (i.e. low aerial%)
    # Use inverse: weaker opp aerial → higher set-piece μ
    set_mult_h = 2.0 - aerial_a   # if opp wins 50%, mult=1.0; 40% → 1.20; 60% → 0.85
    set_mult_a = 2.0 - aerial_h
    keeper_h = fbref["home"].get("psxg_ga_per_90")
    keeper_a = fbref["away"].get("psxg_ga_per_90")

    # ── 7. Dixon-Coles predict ──────────────────────────────────────────────
    dc = predict_xg(
        home_attack       = str_h["attack"],
        home_defense      = str_h["defense"],
        away_attack       = str_a["attack"],
        away_defense      = str_a["defense"],
        league_avg_goals  = league_avg_goals,
        home_advantage    = league_ha,
        neutral           = neutral,
        keeper_adj_home   = keeper_h or 0.0,
        keeper_adj_away   = keeper_a or 0.0,
        set_piece_share_home = set_share_h,
        set_piece_share_away = set_share_a,
        set_piece_aerial_mult_home = set_mult_h,
        set_piece_aerial_mult_away = set_mult_a,
    )

    # ── 8. Elo blend (65/35; DC keeps draw probability intact) ──────────────
    elo = EloModel()
    elo_home, elo_away = elo.win_probability(home_team, away_team)
    p_decisive  = 1.0 - dc["prob_draw"]
    dc_home_ratio = dc["prob_home"] / (dc["prob_home"] + dc["prob_away"]) \
        if (dc["prob_home"] + dc["prob_away"]) > 0 else 0.5
    blended_ratio = 0.65 * dc_home_ratio + 0.35 * elo_home
    prob_home = p_decisive * blended_ratio
    prob_away = 1.0 - prob_home - dc["prob_draw"]
    prob_draw = dc["prob_draw"]

    steps.append({
        "step": "elo_blend",
        "status": "ok",
        "elo_home": round(elo_home, 3),
        "elo_away": round(elo_away, 3),
        "dc_home_ratio": round(dc_home_ratio, 3),
        "blend_weights": "65% DC / 35% Elo",
    })

    # ── 9. Markets + market comparison ──────────────────────────────────────
    markets_raw = compute_all_markets(
        attack_home = str_h["attack"], defense_home = str_h["defense"],
        attack_away = str_a["attack"], defense_away = str_a["defense"],
        league_avg_goals = league_avg_goals,
        neutral = neutral,
        home_aerial_index = aerial_h, away_aerial_index = aerial_a,
    )
    markets = _format_markets(markets_raw, home_team, away_team)

    edge_summary = None
    if has_odds:
        edge_summary = market_edge_summary(prob_home, prob_away, odds_home, odds_away)
        steps.append({
            "step": "market_compare",
            "status": "ok",
            "edge_home_pp": round(edge_summary["edge_a"] * 100, 2),
            "edge_away_pp": round(edge_summary["edge_b"] * 100, 2),
            "vig":          edge_summary["vig"],
        })

    recs = _bet_recommendations(prob_home, prob_draw, prob_away,
                                home_team, away_team, edge_summary)
    kelly = None
    if has_odds:
        # Stake the side the recs flag, only if BET/LEAN
        first = recs[0]
        if first.get("verdict") in ("BET", "LEAN"):
            if first.get("bet", "").startswith(home_team):
                kelly = kelly_stake(prob_home, odds_home, bankroll)
            else:
                kelly = kelly_stake(prob_away, odds_away, bankroll)

    # ── 10. Persist match + signals + prediction ────────────────────────────
    match_id: Optional[int] = None
    try:
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO matches
                       (sport, league, participant_a, participant_b,
                        scheduled_at, venue, neutral_site)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "soccer",
                    league or "International",
                    home_team, away_team,
                    f"{match_date}T15:00:00",
                    "",
                    int(neutral),
                ),
            )
            match_id = cur.lastrowid

        sigs_to_log = [
            ("xg_for_avg5",         home_team, enriched["home"].get("recent_xg_per_game")),
            ("xg_against_avg5",     home_team, enriched["home"].get("recent_xga_per_game")),
            ("npxg_per_game",       home_team, enriched["home"].get("npxg_per_game")),
            ("npxga_per_game",      home_team, enriched["home"].get("npxga_per_game")),
            ("home_xg_per_game",    home_team, enriched["home"].get("home_xg_per_game")),
            ("away_xg_per_game",    home_team, enriched["home"].get("away_xg_per_game")),
            ("goal_overperform",    home_team, enriched["home"].get("goal_overperform")),
            ("open_play_xg_share",  home_team, enriched["home"].get("open_play_xg_share")),
            ("set_piece_xg_share",  home_team, enriched["home"].get("set_piece_xg_share")),
            ("psxg_ga_per_90",      home_team, keeper_h),
            ("ppda_proxy",          home_team, fbref["home"].get("ppda_proxy")),
            ("possession_pct",      home_team, fbref["home"].get("possession_pct")),
            ("att_3rd_touch_pct",   home_team, fbref["home"].get("att_3rd_touch_pct")),
            ("aerials_won_pct",     home_team, fbref["home"].get("aerials_won_pct")),
            ("xg_for_avg5",         away_team, enriched["away"].get("recent_xg_per_game")),
            ("xg_against_avg5",     away_team, enriched["away"].get("recent_xga_per_game")),
            ("npxg_per_game",       away_team, enriched["away"].get("npxg_per_game")),
            ("npxga_per_game",      away_team, enriched["away"].get("npxga_per_game")),
            ("home_xg_per_game",    away_team, enriched["away"].get("home_xg_per_game")),
            ("away_xg_per_game",    away_team, enriched["away"].get("away_xg_per_game")),
            ("goal_overperform",    away_team, enriched["away"].get("goal_overperform")),
            ("open_play_xg_share",  away_team, enriched["away"].get("open_play_xg_share")),
            ("set_piece_xg_share",  away_team, enriched["away"].get("set_piece_xg_share")),
            ("psxg_ga_per_90",      away_team, keeper_a),
            ("ppda_proxy",          away_team, fbref["away"].get("ppda_proxy")),
            ("possession_pct",      away_team, fbref["away"].get("possession_pct")),
            ("att_3rd_touch_pct",   away_team, fbref["away"].get("att_3rd_touch_pct")),
            ("aerials_won_pct",     away_team, fbref["away"].get("aerials_won_pct")),
        ]
        for name, team, val in sigs_to_log:
            v = _safe_float(val)
            if v is not None:
                log_signal(match_id, name, team, signal_value=v,
                           source=("ai" if ai_fallback else "understat_fbref"))

        explanation = _build_explanation(
            home_team, away_team, dc, str_h, str_a, league_avg_goals,
            keeper_h, keeper_a,
            fbref["home"].get("ppda_proxy"), fbref["away"].get("ppda_proxy"),
            elo_home, elo_away,
        )

        with get_db() as conn:
            conn.execute(
                """INSERT INTO predictions
                       (match_id, method, prob_a, prob_b, prob_draw, explanation, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    match_id,
                    "soccer_v2_npxg_split",
                    prob_home, prob_away, prob_draw,
                    explanation,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    except Exception as exc:
        steps.append({"step": "persist", "status": "error", "error": str(exc),
                      "trace": traceback.format_exc()})
        explanation = "Persist failure — model output below."

    # ── 11. Narrative ───────────────────────────────────────────────────────
    confidence = "high" if not ai_fallback and (fbref["home"] or fbref["away"]) \
        else "medium" if not ai_fallback else "low"
    try:
        from ai_agent_soccer import generate_soccer_narrative
        narrative = generate_soccer_narrative(
            home_team, away_team, prob_home, prob_draw, prob_away,
            explanation,
            verdict=recs[0].get("verdict", "") if recs else "",
            confidence=confidence,
        )
    except Exception:
        narrative = explanation

    return {
        "match_id":  match_id,
        "sport":     "soccer",
        "home_team": home_team,
        "away_team": away_team,
        "league":    league,
        "season":    season,
        "date":      match_date,
        "neutral":   neutral,
        "narrative": narrative,
        "prob_home": round(prob_home * 100, 1),
        "prob_draw": round(prob_draw * 100, 1),
        "prob_away": round(prob_away * 100, 1),
        "mu_home":   round(dc["mu_home"], 3),
        "mu_away":   round(dc["mu_away"], 3),
        "mu_home_open": round(dc["mu_home_open"], 3),
        "mu_home_set":  round(dc["mu_home_set"],  3),
        "mu_away_open": round(dc["mu_away_open"], 3),
        "mu_away_set":  round(dc["mu_away_set"],  3),
        "data_confidence": confidence,
        "data_sources": [
            "understat" if not ai_fallback else "ai_fallback",
            "fbref" if (fbref["home"] or fbref["away"]) else None,
            "elo",
        ],
        "model_explanation": explanation,
        "bet_recommendations": recs,
        "kelly": kelly,
        "market_comparison": edge_summary,
        "markets":   markets,
        "ai_signals": {
            "home": enriched["home"],
            "away": enriched["away"],
            "home_fbref": fbref["home"],
            "away_fbref": fbref["away"],
        },
        "steps": steps,
    }
