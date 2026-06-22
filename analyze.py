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
        "steps": steps,
    }
