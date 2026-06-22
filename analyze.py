"""
End-to-end analyze pipeline.
Takes a raw user query → fetches data → runs model → returns full result dict.
"""
from __future__ import annotations
import traceback
from datetime import datetime, timezone
from typing import Optional

from db.database import get_db, init_db
from engine import predict_match
from fetchers.thesportsdb import fetch_match_context
from fetchers.signals import log_signal
from models.elo import EloModel
from models.devig import devig_market


def _format_markets(raw: dict, team_a: str, team_b: str) -> dict:
    """
    Transform the raw markets dict into frontend-friendly form:
    - percentages rounded to 1dp
    - human labels substituted for team_a / team_b
    - best recommendation flagged
    """
    if not raw:
        return {}

    def pct(v):
        return round(float(v) * 100, 1)

    result = {}

    # Match Result 2-Up
    mr2 = raw.get("match_result_2up", {})
    if mr2:
        home_2up = pct(mr2.get("home_win_2up", 0))
        away_2up = pct(mr2.get("away_win_2up", 0))
        result["match_result_2up"] = {
            "label": "Match Result – 2 Up",
            "options": [
                {"label": f"{team_a} to win by 2+", "prob": home_2up,
                 "best": home_2up >= away_2up},
                {"label": f"{team_b} to win by 2+", "prob": away_2up,
                 "best": away_2up > home_2up},
                {"label": "Neither wins by 2+", "prob": pct(mr2.get("not_2up", 0)),
                 "best": False},
            ],
        }

    # Correct Score (top 6)
    cs = raw.get("correct_score", [])
    if cs:
        result["correct_score"] = {
            "label": "Correct Score (most likely)",
            "scores": [
                {"label": f"{team_a} {s['score_home']}–{s['score_away']} {team_b}",
                 "prob": pct(s["prob"]),
                 "best": i == 0}
                for i, s in enumerate(cs[:6])
            ],
        }

    # Spread
    sp = raw.get("spread", [])
    if sp:
        result["spread"] = {
            "label": "Spread (Home handicap)",
            "lines": [
                {
                    "line": s["line"],
                    "label": s["label"],
                    "p_home_covers": pct(s["p_home_covers"]),
                    "p_away_covers": pct(s["p_away_covers"]),
                    "p_push": pct(s["p_push"]),
                    "team_a": team_a,
                    "team_b": team_b,
                }
                for s in sp
            ],
        }

    # Winner Push if Tied
    wp = raw.get("winner_push_if_tied", {})
    if wp:
        home_nd = pct(wp.get("p_home_no_draw", 0))
        away_nd = pct(wp.get("p_away_no_draw", 0))
        result["winner_push_if_tied"] = {
            "label": "Winner (Push if Tied)",
            "options": [
                {"label": team_a, "prob": home_nd, "best": home_nd >= away_nd},
                {"label": team_b, "prob": away_nd, "best": away_nd > home_nd},
            ],
            "draw_prob": pct(wp.get("p_draw", 0)),
        }

    # Next Shot on Target
    sot = raw.get("next_shot_on_target", {})
    if sot:
        home_sot = pct(sot.get("p_home_next_sot", 0))
        away_sot = pct(sot.get("p_away_next_sot", 0))
        result["next_shot_on_target"] = {
            "label": "Next Shot on Target",
            "options": [
                {"label": team_a, "prob": home_sot, "best": home_sot >= away_sot},
                {"label": team_b, "prob": away_sot, "best": away_sot > home_sot},
            ],
            "note": sot.get("note", ""),
        }

    # Method of Goal 2
    mg = raw.get("method_of_goal_2", {})
    if mg:
        methods = [
            {"label": "Foot",    "prob": pct(mg.get("foot", 0))},
            {"label": "Header",  "prob": pct(mg.get("header", 0))},
            {"label": "Penalty", "prob": pct(mg.get("penalty", 0))},
        ]
        best = max(methods, key=lambda x: x["prob"])
        for m in methods:
            m["best"] = m["label"] == best["label"]
        result["method_of_goal_2"] = {
            "label": "Method of Goal 2",
            "options": methods,
            "note": mg.get("note", ""),
        }

    return result


def _safe_float(val, default=None):
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def run_analysis(user_query: str) -> dict:
    """
    Full pipeline:
    1. Parse query with AI
    2. Fetch web data
    3. AI interprets data → signals
    4. Run prediction engine
    5. AI generates narrative
    Returns a dict ready to JSON-serialize and send to the frontend.
    """
    init_db()
    steps = []  # debug trace shown in the "raw data" panel

    # ── Step 1: Parse query ──────────────────────────────────────────────────
    try:
        from ai_agent import parse_query
        parsed = parse_query(user_query)
        steps.append({"step": "parse_query", "status": "ok", "data": parsed})
    except Exception as exc:
        return {"error": f"Could not parse query: {exc}", "steps": steps}

    team_a = parsed.get("team_a", "Team A")
    team_b = parsed.get("team_b", "Team B")
    sport = parsed.get("sport", "soccer")
    match_date = parsed.get("date") or datetime.now(timezone.utc).date().isoformat()
    notes = parsed.get("notes", "")

    # ── Step 2: Fetch web data ────────────────────────────────────────────────
    fetched: dict = {}
    live_data_available = False
    if sport == "soccer":
        try:
            fetched = fetch_match_context(team_a, team_b)
            has_last5 = bool(fetched.get("team_a", {}).get("last5") or fetched.get("team_b", {}).get("last5"))
            live_data_available = has_last5
            steps.append({
                "step": "web_fetch",
                "status": "ok" if live_data_available else "partial",
                "live_data": live_data_available,
                "sources": fetched.get("sources", []),
            })
        except Exception as exc:
            steps.append({"step": "web_fetch", "status": "error", "error": str(exc)})

    if not live_data_available:
        fetched.setdefault("sources", []).append({
            "label": "No live data fetched",
            "url": "",
            "snippet": (
                "External sports API was unreachable or returned no results. "
                "The AI will use training knowledge to estimate signals — "
                "set confidence=low. To enable live data, set SPORTSDB_API_KEY "
                "or ODDS_API_KEY environment variables."
            ),
        })

    # ── Step 3: AI interprets data → signals ──────────────────────────────────
    signals: dict = {}
    try:
        from ai_agent import interpret_signals
        signals = interpret_signals(team_a, team_b, {**fetched, "notes": notes})
        steps.append({"step": "interpret_signals", "status": "ok", "data": signals})
    except Exception as exc:
        steps.append({"step": "interpret_signals", "status": "error", "error": str(exc)})
        signals = {"team_a": {}, "team_b": {}, "neutral_site": 1, "confidence": "low"}

    # ── Step 4: Create match + log signals + run engine ───────────────────────
    match_id: Optional[int] = None
    prediction: dict = {}

    try:
        elo_model = EloModel()
        sig_a = signals.get("team_a", {})
        sig_b = signals.get("team_b", {})

        # Set Elo if AI provided ratings; otherwise leave at default
        if sig_a.get("elo_rating"):
            elo_model.set_rating(team_a, float(sig_a["elo_rating"]))
        if sig_b.get("elo_rating"):
            elo_model.set_rating(team_b, float(sig_b["elo_rating"]))

        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO matches
                       (sport, league, participant_a, participant_b,
                        scheduled_at, venue, neutral_site)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    sport,
                    parsed.get("league") or "International",
                    team_a,
                    team_b,
                    f"{match_date}T12:00:00",
                    parsed.get("venue") or "",
                    int(signals.get("neutral_site", 1)),
                ),
            )
            match_id = cur.lastrowid

        # Log all signals the AI extracted
        signal_map_a = {
            "xg_for_avg5":       _safe_float(sig_a.get("xg_for_avg5")),
            "xg_against_avg5":   _safe_float(sig_a.get("xg_against_avg5")),
            "form_weighted10":   _safe_float(sig_a.get("form_weighted10")),
            "rest_days":         _safe_float(sig_a.get("rest_days")),
            "key_player_out_flag": _safe_float(sig_a.get("key_player_out_flag"), 0),
        }
        signal_map_b = {
            "xg_for_avg5":       _safe_float(sig_b.get("xg_for_avg5")),
            "xg_against_avg5":   _safe_float(sig_b.get("xg_against_avg5")),
            "form_weighted10":   _safe_float(sig_b.get("form_weighted10")),
            "rest_days":         _safe_float(sig_b.get("rest_days")),
            "key_player_out_flag": _safe_float(sig_b.get("key_player_out_flag"), 0),
        }
        if sig_a.get("injury_note"):
            log_signal(match_id, "injury_note", team_a, signal_text=sig_a["injury_note"])
        if sig_b.get("injury_note"):
            log_signal(match_id, "injury_note", team_b, signal_text=sig_b["injury_note"])

        for name, val in signal_map_a.items():
            if val is not None:
                log_signal(match_id, name, team_a, signal_value=val, source="ai_interpreted")
        for name, val in signal_map_b.items():
            if val is not None:
                log_signal(match_id, name, team_b, signal_value=val, source="ai_interpreted")

        prediction = predict_match(match_id, method="ai_analysis", bankroll=1000.0)
        steps.append({"step": "engine", "status": "ok", "prob_a": prediction.get("prob_a")})

    except Exception as exc:
        steps.append({"step": "engine", "status": "error", "error": str(exc),
                      "trace": traceback.format_exc()})
        return {"error": f"Prediction engine failed: {exc}", "steps": steps,
                "team_a": team_a, "team_b": team_b}

    # ── Step 5: AI generates narrative ────────────────────────────────────────
    narrative = ""
    try:
        from ai_agent import generate_narrative
        narrative = generate_narrative(
            team_a, team_b,
            prediction["prob_a"], prediction["prob_b"], prediction.get("prob_draw", 0),
            prediction.get("explanation", ""),
            signals,
            fetched,
        )
        steps.append({"step": "narrative", "status": "ok"})
    except Exception as exc:
        narrative = prediction.get("explanation", "Model prediction complete.")
        steps.append({"step": "narrative", "status": "error", "error": str(exc)})

    # ── Build raw sources list for the frontend transparency panel ────────────
    raw_sources = fetched.get("sources", [])

    # Add what the AI inferred (so user can spot hallucinations)
    ai_signal_summary = []
    for team_key, team_name in [("team_a", team_a), ("team_b", team_b)]:
        sig = signals.get(team_key, {})
        for k, v in sig.items():
            if v is not None and k != "injury_note":
                ai_signal_summary.append({"team": team_name, "signal": k, "value": v,
                                          "source": "AI-interpreted from fetched data"})

    # Compact fetched team data for display
    team_a_raw = {
        "last5": fetched.get("team_a", {}).get("last5", []),
        "key_players": fetched.get("team_a", {}).get("key_players", []),
        "description": fetched.get("team_a", {}).get("description", ""),
    }
    team_b_raw = {
        "last5": fetched.get("team_b", {}).get("last5", []),
        "key_players": fetched.get("team_b", {}).get("key_players", []),
        "description": fetched.get("team_b", {}).get("description", ""),
    }

    # ── Format markets for frontend ───────────────────────────────────────────
    raw_markets = prediction.get("markets", {})
    formatted_markets = _format_markets(raw_markets, team_a, team_b)

    return {
        "match_id": match_id,
        "team_a": team_a,
        "team_b": team_b,
        "sport": sport,
        "date": match_date,
        "narrative": narrative,
        "prob_a": round(prediction["prob_a"] * 100, 1),
        "prob_draw": round((prediction.get("prob_draw") or 0) * 100, 1),
        "prob_b": round(prediction["prob_b"] * 100, 1),
        "likely_scorer_a": signals.get("likely_scorer_a"),
        "likely_scorer_b": signals.get("likely_scorer_b"),
        "model_explanation": prediction.get("explanation", ""),
        "kelly_note": prediction.get("kelly_note", ""),
        "data_confidence": signals.get("confidence", "low"),
        "signal_notes": signals.get("signal_notes", ""),
        "ai_signals": ai_signal_summary,
        "raw_sources": raw_sources,
        "team_a_raw": team_a_raw,
        "team_b_raw": team_b_raw,
        "markets": formatted_markets,
        "steps": steps,
    }
