"""
FastAPI local API layer for Predicta.
Run with: uvicorn app:app --reload
"""
from __future__ import annotations
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import env_loader  # noqa: F401 — loads .env on import, handles CRLF/BOM/quotes

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api_auth import require_api_key, auth_enabled, require_trade_passcode

from db.database import init_db, get_db
from engine import predict_match, record_outcome
from models.calibration import compute_metrics_from_db
from models.devig import devig_market

TEMPLATES_DIR = Path(__file__).parent / "templates"
from fetchers.odds import log_manual_odds
from fetchers.signals import log_signal

app = FastAPI(title="Predicta", description="Multi-sport prediction & calibration tracker", version="1.0.0")


@app.on_event("startup")
def startup():
    init_db()
    import threading

    def _bg_resolve():
        try:
            from tasks.auto_resolve import run_auto_resolve
            run_auto_resolve()
        except Exception:
            pass
    threading.Thread(target=_bg_resolve, daemon=True).start()

    # Start automated hypothetical paper trading runner (Kimi phase-1 protocol).
    # Runs Mon-Fri during market hours: morning scan at 9:35 ET, position checks
    # every 30 min, force-close at 15:50 ET. No Alpaca orders placed.
    try:
        from fetchers.high_value_runner import start_runner
        start_runner()
    except Exception:
        pass

    # Start the Low Value engine's own runner (2026-07-12 fix — this call
    # was missing entirely). Without it, _runner_loop() never runs, so the
    # daily 8:00-8:14 AM ET universe scan + entry/exit check never fires on
    # its own; the engine only ever did anything if someone manually hit
    # POST /trade/low-value/runner/start, and that in-memory state resets
    # on every Railway restart/redeploy — which, given how often this repo
    # deploys, meant the "automatic" daily collection had effectively never
    # been running. Independent thread/globals from the High Value runner
    # above (fetchers/low_value_runner.py).
    try:
        from fetchers.low_value_runner import start_runner as start_low_value_runner
        start_low_value_runner()
    except Exception:
        pass

    # Start the soccer auto-collection loop:
    #   scan_fixtures every 4 h, resolve_finished every 2 h, weekly_report
    #   every Monday 08:00 UTC (writes to reports/ and pushes to GitHub if
    #   GITHUB_TOKEN + GITHUB_REPO env vars are set). Disable by setting
    #   SOCCER_AUTO_DISABLED=1.
    if not os.environ.get("SOCCER_AUTO_DISABLED"):
        try:
            from tasks.soccer_auto import start_soccer_auto
            start_soccer_auto()
        except Exception:
            pass

    # Start the tennis auto-resolve loop: resolve_finished() every 3 h, so
    # predictions get graded against real ESPN results without a manual
    # POST /tennis-auto/resolve call after every round of matches. No
    # fixture-scan or weekly-report job yet (tennis has neither an
    # auto-predict job nor a report renderer). Disable with TENNIS_AUTO_DISABLED=1.
    if not os.environ.get("TENNIS_AUTO_DISABLED"):
        try:
            from tasks.tennis_auto import start_tennis_auto
            start_tennis_auto()
        except Exception:
            pass

    # Always-on NRFI daily pipeline (predict 13:00 / capture 23:00 / resolve
    # 05:00 UTC), pushing results to GitHub. Replaces the unreliable GitHub
    # Actions cron. Disable with NRFI_AUTO_DISABLED=1.
    try:
        from tasks.nrfi_auto import start_nrfi_auto
        start_nrfi_auto()
    except Exception:
        pass


@app.get("/ping")
def ping():
    """Lightweight health check for Railway (returns immediately, no DB call)."""
    return {"ok": True}


# ── Match endpoints ─────────────────────────────────────────────────────────

class MatchCreate(BaseModel):
    sport: str
    league: Optional[str] = None
    participant_a: str
    participant_b: str
    scheduled_at: str
    venue: Optional[str] = None
    neutral_site: bool = False


@app.post("/matches", status_code=201)
def create_match(body: MatchCreate):
    valid_sports = ("soccer", "table_tennis", "tennis", "baseball", "esports", "rugby", "ufc")
    if body.sport not in valid_sports:
        raise HTTPException(400, f"sport must be one of: {', '.join(valid_sports)}")
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO matches (sport, league, participant_a, participant_b,
                   scheduled_at, venue, neutral_site)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (body.sport, body.league, body.participant_a, body.participant_b,
             body.scheduled_at, body.venue, int(body.neutral_site)),
        )
        return {"match_id": cur.lastrowid}


@app.get("/matches")
def list_matches(sport: Optional[str] = None, status: Optional[str] = None):
    with get_db() as conn:
        query = "SELECT * FROM matches WHERE 1=1"
        params = []
        if sport:
            query += " AND sport=?"
            params.append(sport)
        if status:
            query += " AND status=?"
            params.append(status)
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/matches/{match_id}")
def get_match(match_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Match not found")
    return dict(row)


# ── Signal endpoints ─────────────────────────────────────────────────────────

class SignalCreate(BaseModel):
    signal_name: str
    participant: Optional[str] = None
    signal_value: Optional[float] = None
    signal_text: Optional[str] = None
    source: str = "manual"


@app.post("/matches/{match_id}/signals", status_code=201)
def add_signal(match_id: int, body: SignalCreate):
    log_signal(match_id, body.signal_name, body.participant,
               body.signal_value, body.signal_text, body.source)
    return {"status": "ok"}


@app.get("/matches/{match_id}/signals")
def get_signals(match_id: int):
    from fetchers.signals import get_signals_for_match
    return get_signals_for_match(match_id)


# ── Prediction endpoints ──────────────────────────────────────────────────────

class PredictRequest(BaseModel):
    method: str = "model_v1"
    bankroll: float = 1000.0
    surface: str = "all"


@app.post("/matches/{match_id}/predict")
def predict(match_id: int, body: PredictRequest):
    result = predict_match(match_id, body.method, body.bankroll, body.surface)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.get("/matches/{match_id}/predictions")
def list_predictions(match_id: int):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM predictions WHERE match_id=?", (match_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Odds endpoints ────────────────────────────────────────────────────────────

class OddsCreate(BaseModel):
    book: str
    market: str = "moneyline"
    price_a: float
    price_b: float
    price_draw: Optional[float] = None


@app.post("/matches/{match_id}/odds", status_code=201)
def add_odds(match_id: int, body: OddsCreate):
    row_id = log_manual_odds(match_id, body.book, body.market,
                              body.price_a, body.price_b, body.price_draw)
    prices = {"a": body.price_a, "b": body.price_b}
    if body.price_draw:
        prices["draw"] = body.price_draw
    fair = devig_market(prices)
    return {"snapshot_id": row_id, "fair_probs": fair}


@app.get("/matches/{match_id}/odds")
def get_odds(match_id: int):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM odds_snapshots WHERE match_id=?", (match_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Outcome endpoints ─────────────────────────────────────────────────────────

class OutcomeCreate(BaseModel):
    result: str
    score_a: Optional[int] = None
    score_b: Optional[int] = None
    update_ratings: bool = True
    importance: str = "default"
    surface: str = "all"


class BatchOutcome(BaseModel):
    match_id: int
    result: str
    score_a: Optional[int] = None
    score_b: Optional[int] = None


@app.post("/matches/{match_id}/outcome", status_code=201)
def add_outcome(match_id: int, body: OutcomeCreate):
    result = record_outcome(
        match_id, body.result, body.score_a, body.score_b,
        body.update_ratings, body.importance, body.surface
    )
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.post("/outcomes/batch", status_code=201)
def add_outcomes_batch(outcomes: list[BatchOutcome]):
    """
    Record multiple match outcomes at once.
    Body: [{match_id, result, score_a?, score_b?}, ...]
    result must be 'a', 'b', or 'draw'.
    """
    results = []
    for item in outcomes:
        r = record_outcome(item.match_id, item.result, item.score_a, item.score_b)
        results.append({"match_id": item.match_id, "status": "ok" if "error" not in r else "error",
                        **r})
    return results


@app.get("/pending-outcomes")
def pending_outcomes():
    """
    List matches that have a prediction but no recorded outcome yet.
    Useful for quickly knowing which results to enter.
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT m.id, m.sport, m.participant_a, m.participant_b,
                   m.scheduled_at, p.prob_a, p.prob_b, p.method,
                   s_rec.signal_text  AS recommendation,
                   s_conf.signal_text AS data_confidence
            FROM matches m
            JOIN predictions p    ON p.match_id = m.id
            LEFT JOIN outcomes o  ON o.match_id = m.id
            LEFT JOIN signals s_rec  ON (s_rec.match_id = m.id AND s_rec.signal_name  = 'recommendation')
            LEFT JOIN signals s_conf ON (s_conf.match_id = m.id AND s_conf.signal_name = 'data_confidence')
            WHERE o.match_id IS NULL
            ORDER BY m.scheduled_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


@app.post("/resolve-pending")
def resolve_pending(dry_run: bool = False):
    """
    Trigger auto-resolve: scan all predictions past their scheduled time,
    fetch actual results from Setka Cup / TT Cup, and record outcomes.

    dry_run=true: fetch results but don't write to DB (preview mode).
    Returns a summary with per-match status and source.
    """
    from tasks.auto_resolve import run_auto_resolve
    return run_auto_resolve(dry_run=dry_run)


@app.get("/signal-accuracy")
def signal_accuracy():
    """
    Show which signals have the highest lift (accuracy when signal is high vs low).
    Useful for identifying which model inputs are actually predictive and
    should have higher blend weights.
    Requires at least 5 resolved predictions per signal.
    """
    from tasks.auto_resolve import _signal_accuracy_summary
    result = _signal_accuracy_summary()
    if not result:
        return {"message": "Not enough resolved predictions yet. Needs at least 5 per signal."}
    return result


# ── Prediction Audit ──────────────────────────────────────────────────────────

def _categorize_prediction(
    was_correct: bool,
    prob_a: float,
    score_a,
    score_b,
    sport: str,
    signals: dict,
) -> str:
    """Assign a failure (or success) category to a resolved prediction."""
    max_prob = max(prob_a, 1.0 - prob_a)

    if was_correct:
        return "CORRECT_HIGH_CONF" if max_prob >= 0.65 else "CORRECT"

    # Data quality gate
    for key, val in signals.items():
        if "data_confidence" in key and isinstance((val or {}).get("text"), str):
            if "Low" in val["text"]:
                return "LOW_DATA_QUALITY"

    # Coin flip — less than 4pp model edge, outcome is noise
    if max_prob < 0.54:
        return "COIN_FLIP"

    # Close game / match (within 1 run/goal/game)
    if score_a is not None and score_b is not None:
        if abs(score_a - score_b) <= 1:
            return "CLOSE_GAME"

    # High confidence miss
    if max_prob >= 0.65:
        return "HIGH_CONFIDENCE_MISS"

    # Weather impact (baseball / soccer)
    if sport in ("baseball", "soccer"):
        wind = (signals.get("wind_factor") or {}).get("value")
        if wind is not None and abs(wind - 1.0) > 0.03:
            return "WEATHER_IMPACT"

    return "NORMAL_VARIANCE"


@app.get("/prediction-audit")
def prediction_audit(sport: Optional[str] = None, limit: int = 100):
    """
    Resolved predictions with failure categories and all logged signals.

    Use this to diagnose model weaknesses: filter by sport or category,
    then copy the output via GET /audit for AI review.

    Categories:
      CORRECT / CORRECT_HIGH_CONF — model was right
      HIGH_CONFIDENCE_MISS — model >65% confident, still wrong
      CLOSE_GAME — outcome within 1 run/goal (variance, not model error)
      COIN_FLIP — edge <4pp, outcome is noise
      WEATHER_IMPACT — wind/temp signals were non-neutral
      LOW_DATA_QUALITY — data_confidence=Low at prediction time
      NORMAL_VARIANCE — within normal model range, no clear signal
    """
    with get_db() as conn:
        q = """
            SELECT m.id, m.sport, m.league, m.participant_a, m.participant_b,
                   m.scheduled_at, m.venue,
                   p.prob_a, p.prob_b, p.method, p.explanation,
                   o.result, o.score_a, o.score_b, o.recorded_at
            FROM matches m
            JOIN predictions p ON p.match_id = m.id
            JOIN outcomes    o ON o.match_id = m.id
            WHERE 1=1
        """
        params: list = []
        if sport:
            q += " AND m.sport = ?"
            params.append(sport)
        q += " ORDER BY m.scheduled_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()

        results = []
        for row in rows:
            r = dict(row)
            mid = r["id"]

            # Pull all signals for this match
            sigs_raw = conn.execute(
                "SELECT signal_name, participant, signal_value, signal_text "
                "FROM signals WHERE match_id=?", (mid,)
            ).fetchall()

            signals: dict = {}
            for s in sigs_raw:
                key = s["signal_name"]
                if s["participant"]:
                    key = f"{key}_{s['participant']}"
                signals[key] = {"value": s["signal_value"], "text": s["signal_text"]}

            prob_a = r["prob_a"] or 0.5
            predicted = "a" if prob_a >= 0.5 else "b"
            was_correct = predicted == r["result"]

            r["signals"] = signals
            r["predicted_winner"] = predicted
            r["was_correct"] = was_correct
            r["failure_category"] = _categorize_prediction(
                was_correct, prob_a, r["score_a"], r["score_b"], r["sport"], signals
            )
            results.append(r)

    # Summary stats
    total = len(results)
    correct = sum(1 for r in results if r["was_correct"])
    cat_counts: dict = {}
    for r in results:
        cat = r["failure_category"]
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    return {
        "total": total,
        "correct": correct,
        "accuracy_pct": round(correct / total * 100, 1) if total else 0,
        "category_counts": cat_counts,
        "predictions": results,
    }


@app.get("/audit", response_class=HTMLResponse)
def audit_ui():
    return HTMLResponse(content=(TEMPLATES_DIR / "audit.html").read_text(encoding="utf-8"))


# ── Accuracy & Calibration ────────────────────────────────────────────────────

@app.get("/accuracy")
def accuracy(sport: Optional[str] = None, method: Optional[str] = None):
    """
    Full accuracy report: win % prediction accuracy, bet accuracy, ROI,
    Brier score, log-loss, calibration curve, benchmarks.
    Break down by sport, confidence level, and method.
    """
    return compute_metrics_from_db(method=method, sport=sport)


@app.get("/calibration")
def calibration(method: Optional[str] = None):
    return compute_metrics_from_db(method=method)


@app.get("/tt-performance")
def tt_performance():
    """
    Table tennis model validation report — Kimi's proof-of-edge workflow.

    Shows: resolved predictions, Brier score, ROI, predictions with real odds
    where model beat market by >5pp (value bets) vs outcomes, and a plain-English
    verdict: EDGE PROVEN / EDGE EXISTS / NO EDGE / NOT ENOUGH DATA.

    Call this after each week of paper trading to see if TT model beats the market.
    Requires at least 10 resolved TT predictions to show meaningful metrics.
    """
    metrics = compute_metrics_from_db(sport="table_tennis")
    overall = metrics.get("overall", {})
    n = overall.get("n", 0)

    if n < 10:
        return {
            "verdict": "NOT ENOUGH DATA",
            "n_resolved": n,
            "needed": 10,
            "message": (
                f"Only {n} resolved TT predictions. Need at least 10 to compute "
                "meaningful metrics. Keep running predictions and use "
                "POST /resolve-pending after each match day."
            ),
            "next_step": "Run 20+ TT predictions with market odds included, then call this endpoint.",
        }

    brier = metrics.get("brier_score", 0.25)
    roi   = overall.get("roi", None)
    acc   = overall.get("accuracy", 0.5)

    if roi is not None and roi > 5.0:
        verdict = "EDGE PROVEN"
        note = (
            f"ROI +{roi:.1f}% across {n} predictions. "
            "Scale to real money — Kelly stake, tight bankroll management."
        )
    elif roi is not None and roi > 0:
        verdict = "EDGE EXISTS"
        note = (
            f"ROI +{roi:.1f}% — small edge, not yet conclusive. "
            "Need 50+ predictions to distinguish skill from variance."
        )
    elif brier < 0.22:
        verdict = "CALIBRATED — CHECK MARKET ODDS"
        note = (
            f"Brier {brier:.3f} < 0.22 (better than random). "
            "Model is calibrated but ROI unclear — were market odds included?"
        )
    else:
        verdict = "NO EDGE DETECTED"
        note = (
            f"ROI {roi:.1f}% and Brier {brier:.3f}. "
            "Model is not outperforming the market. Consider pivoting to esports."
        )

    return {
        "verdict":       verdict,
        "note":          note,
        "n_resolved":    n,
        "accuracy":      round(acc * 100, 1),
        "brier_score":   brier,
        "roi":           roi,
        "full_metrics":  metrics,
    }


# ── NRFI Performance & API ───────────────────────────────────────────────────

@app.get("/nrfi-performance")
def nrfi_performance():
    """
    NRFI model live performance dashboard.

    Reads nrfi_bets table and computes:
      - Win rate at each threshold (55%, 56%, 57%)
      - ROI in units (at -110 juice: +0.909 per win, -1.0 per loss)
      - Kelly-adjusted profit
      - Verdict: EDGE PROVEN / EDGE EXISTS / NOT ENOUGH DATA / NO EDGE

    Outcome column is filled by POST /nrfi-resolve after each game.
    """
    from db.database import get_db
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM nrfi_bets ORDER BY game_date DESC"
        ).fetchall()

    total    = len(rows)
    resolved = [r for r in rows if r["outcome"] is not None]
    pending  = total - len(resolved)

    if len(resolved) < 10:
        return {
            "verdict":       "NOT ENOUGH DATA",
            "n_total":       total,
            "n_resolved":    len(resolved),
            "n_pending":     pending,
            "needed":        20,
            "message":       (
                f"{len(resolved)} resolved predictions. Need 20+ to compute meaningful metrics. "
                "Query more games and resolve outcomes via POST /nrfi-resolve."
            ),
        }

    JUICE = -110.0
    WIN_PAYOUT = 100 / 110   # 0.9090 units per win at -110

    def _thresh_stats(thresh: float) -> dict:
        bets = [r for r in resolved if r["verdict"] in ("BET", "LEAN")
                and r["p_nrfi"] >= thresh]
        if not bets:
            return {"n": 0}
        wins = sum(1 for b in bets if b["won"] == 1)
        wr   = wins / len(bets)
        pnl  = wins * WIN_PAYOUT - (len(bets) - wins) * 1.0
        roi  = pnl / len(bets) * 100
        import math
        se   = math.sqrt(wr * (1 - wr) / len(bets)) if len(bets) > 1 else 0
        ci_lo = max(0, wr - 1.96 * se) * 100
        ci_hi = min(1, wr + 1.96 * se) * 100
        z    = (wr - 0.524) / math.sqrt(0.524 * 0.476 / len(bets)) if len(bets) > 1 else 0
        import scipy.stats as _st
        p_val = float(1 - _st.norm.cdf(z))
        return {
            "n": len(bets), "wins": wins,
            "win_rate_pct": round(wr * 100, 1),
            "roi_pct": round(roi, 2),
            "pnl_units": round(pnl, 3),
            "ci_95": [round(ci_lo, 1), round(ci_hi, 1)],
            "p_value": round(p_val, 4),
            "significant": p_val < 0.05,
        }

    bets_only  = [r for r in resolved if r["verdict"] == "BET"]
    wins_total = sum(1 for r in bets_only if r["won"] == 1)
    wr_overall = wins_total / len(bets_only) if bets_only else 0

    stats_55 = _thresh_stats(55.0)
    stats_57 = _thresh_stats(57.0)

    if stats_55.get("significant") and stats_55.get("roi_pct", 0) > 3:
        verdict = "EDGE PROVEN"
        note    = (f"Win rate {stats_55['win_rate_pct']}% on {stats_55['n']} BET games, "
                   f"ROI +{stats_55['roi_pct']}%, p={stats_55['p_value']} — statistically significant.")
    elif stats_55.get("n", 0) > 0 and stats_55.get("win_rate_pct", 0) > 52.4:
        verdict = "EDGE EXISTS"
        note    = (f"Win rate {stats_55.get('win_rate_pct')}% — above breakeven but not yet "
                   f"statistically significant (n={stats_55.get('n')}). Keep logging.")
    elif len(resolved) < 50:
        verdict = "TOO EARLY"
        note    = f"Only {len(resolved)} resolved. Need 50+ to distinguish skill from variance."
    else:
        verdict = "NO EDGE DETECTED"
        note    = "Win rate at or below breakeven (52.4%) — model is not outperforming at this threshold."

    all_bets_rows = [
        {
            "id":           r["id"],
            "game_date":    r["game_date"],
            "matchup":      f"{r['home_team']} vs {r['away_team']}",
            "home_starter": r["home_starter"],
            "away_starter": r["away_starter"],
            "p_nrfi":       r["p_nrfi"],
            "verdict":      r["verdict"],
            "confidence":   r["confidence"],
            "kelly_half":   r["kelly_half_pct"],
            "stake":        r["recommended_stake"],
            "outcome":      "NRFI" if r["outcome"] == 1 else ("YRFI" if r["outcome"] == 0 else "pending"),
            "won":          r["won"],
            "pnl_units":    r["pnl_units"],
            "ml_pick":      r["ml_pick"],
            "ml_correct":   r["ml_correct"],
            "f5_pick":      r["f5_pick"],
            "f5_correct":   r["f5_correct"],
            "ou_pick":      r["ou_pick"],
            "ou_correct":   r["ou_correct"],
        }
        for r in rows
    ]

    return {
        "verdict":        verdict,
        "note":           note,
        "n_total":        total,
        "n_resolved":     len(resolved),
        "n_pending":      pending,
        "breakeven_pct":  52.4,
        "threshold_55":   stats_55,
        "threshold_57":   stats_57,
        "all_bets":       all_bets_rows,
    }


@app.get("/market-performance")
def market_performance(days: int = 3650):
    """
    Moneyline / F5 / Over-Under win-rate rollup across all committed daily
    prediction files. resolve_predictions() already grades these markets into
    ml_correct/f5_correct/ou_correct every night — this aggregates them across
    days automatically so losses don't need to be tallied by hand from
    individual data/nrfi_predictions/*.json files. Diagnostic only: unlike
    NRFI, these markets aren't walk-forward validated yet.
    """
    import nrfi_store
    return nrfi_store.aggregate_market_performance(days)


@app.post("/nrfi-resolve")
def nrfi_resolve(body: dict):
    """
    Mark an NRFI bet as resolved after the game is played.

    Body: { "id": 42, "home_1st_runs": 0, "away_1st_runs": 0 }
    OR:   { "game_date": "2026-06-29", "home_team": "HOU", "away_team": "DET",
             "home_1st_runs": 1, "away_1st_runs": 0 }

    Automatically computes: outcome (1=NRFI/0=YRFI), won, pnl_units.
    """
    from db.database import get_db
    from datetime import datetime, timezone

    h1 = int(body.get("home_1st_runs", 0))
    a1 = int(body.get("away_1st_runs", 0))
    outcome = 1 if (h1 == 0 and a1 == 0) else 0

    with get_db() as db:
        if "id" in body:
            row = db.execute("SELECT * FROM nrfi_bets WHERE id=?", (body["id"],)).fetchone()
        else:
            gd  = body.get("game_date")
            ht  = body.get("home_team", "")
            at  = body.get("away_team", "")
            # Try exact match first, then LIKE (handles full name vs abbreviation)
            row = db.execute(
                "SELECT * FROM nrfi_bets WHERE game_date=? AND home_team=? AND away_team=? LIMIT 1",
                (gd, ht, at),
            ).fetchone()
            if not row:
                row = db.execute(
                    """SELECT * FROM nrfi_bets
                       WHERE game_date=?
                         AND (home_team LIKE ? OR home_team=?)
                         AND (away_team LIKE ? OR away_team=?)
                       ORDER BY logged_at DESC LIMIT 1""",
                    (gd, f"%{ht}%", ht, f"%{at}%", at),
                ).fetchone()

        if not row:
            raise HTTPException(404, "NRFI bet not found")

        bet_side = "NRFI" if row["p_nrfi"] >= 50 else "YRFI"
        won = 1 if (bet_side == "NRFI" and outcome == 1) or (bet_side == "YRFI" and outcome == 0) else 0
        pnl = round(100 / 110, 4) if won else -1.0

        db.execute(
            """UPDATE nrfi_bets SET outcome=?, home_1st_runs=?, away_1st_runs=?,
               won=?, pnl_units=?, resolved_at=? WHERE id=?""",
            (outcome, h1, a1, won, pnl, datetime.now(timezone.utc).isoformat(), row["id"]),
        )

    return {"id": row["id"], "outcome": "NRFI" if outcome else "YRFI",
            "won": bool(won), "pnl_units": pnl}


@app.get("/nrfi-auto/status")
def nrfi_auto_status():
    """Is the always-on NRFI scheduler running? When did each job last fire?"""
    from tasks.nrfi_auto import status
    return status()


@app.get("/nrfi-auto/odds-diag")
def nrfi_odds_diag():
    """
    Diagnose why NRFI odds capture (CLV) works or fails — reports the raw Odds
    API status, quota remaining, and whether the first-inning market comes back
    or is rejected (wrong market / plan not included). Use this instead of
    guessing at config.
    """
    from fetchers.nrfi_odds import diagnose
    return diagnose()


@app.get("/nrfi-auto/anthropic-diag")
def nrfi_anthropic_diag():
    """
    Diagnose why run_baseball_analysis silently failed for a whole day's slate
    (2026-07-05 root-cause: query parsing calls Anthropic first — if that call
    fails, every downstream game produces a blank record). Actually calls
    parse_baseball_query with a trivial query and reports success/failure with
    the real exception, instead of guessing whether it's a missing key, an
    expired key, a rate limit, or a network egress block.
    """
    import os
    key_present = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    out = {"key_present": key_present}
    try:
        from ai_agent_baseball import parse_baseball_query
        result = parse_baseball_query("Yankees vs Red Sox tonight")
        out["status"] = "ok"
        out["parsed"] = result
    except Exception as exc:
        out["status"] = "failed"
        out["exception"] = f"{type(exc).__name__}: {exc}"
        if not key_present:
            out["note"] = "ANTHROPIC_API_KEY not set in this environment."
        elif "rate" in str(exc).lower() or "429" in str(exc):
            out["note"] = "Looks like a rate limit / quota error — check usage in the Anthropic console."
        elif "401" in str(exc) or "authentication" in str(exc).lower():
            out["note"] = "Looks like an invalid/expired key — check ANTHROPIC_API_KEY on Railway."
        else:
            out["note"] = "Unrecognized failure — see the raw exception above."
    return out


@app.api_route("/nrfi-auto/run", methods=["GET", "POST"])
def nrfi_auto_run(job: str = "predict", date: Optional[str] = None,
                  background: bool = True):
    """
    Manually fire an NRFI job now (for testing / on-demand). job = predict |
    capture | resolve.

    Defaults to background=true: launches the job in a thread and returns
    immediately (a full predict run takes ~1 min — longer than a browser waits).
    Check progress/result at GET /nrfi-auto/status. Pass background=false to run
    synchronously and get the result inline (may time out on slow runs).
    """
    from tasks import nrfi_auto
    fn = {"predict": nrfi_auto.run_predict,
          "capture": nrfi_auto.run_capture,
          "resolve": nrfi_auto.run_resolve}.get(job)
    if fn is None:
        raise HTTPException(400, "job must be one of: predict, capture, resolve")

    if background:
        import threading
        threading.Thread(target=lambda: nrfi_auto._run_job(job, lambda: fn(date),
                                                           f"last_{job}"),
                         name=f"nrfi-manual-{job}", daemon=True).start()
        return {"job": job, "status": "started",
                "note": "running in background — check GET /nrfi-auto/status"}
    try:
        return {"job": job, "result": fn(date)}
    except Exception as exc:
        raise HTTPException(500, f"{type(exc).__name__}: {exc}")


@app.get("/api/nrfi")
def api_nrfi(home: str, away: str, date: Optional[str] = None, bankroll: float = 1000.0):
    """
    Clean JSON API for programmatic NRFI probability consumption.

    GET /api/nrfi?home=HOU&away=DET&date=2026-06-29&bankroll=500

    Returns: probability, verdict, Kelly stake, and breakeven info.
    Designed for syndicates / automated bettors who want raw output.
    """
    from analyze_baseball import run_baseball_analysis
    query = f"{home} vs {away}"
    if date:
        query += f" {date}"
    result = run_baseball_analysis(query, bankroll=bankroll)

    nrfi_market = result.get("markets", {}).get("nrfi", {})
    opts = nrfi_market.get("options", [])
    p_nrfi = opts[0].get("prob") if opts else None
    p_yrfi = opts[1].get("prob") if len(opts) > 1 else None

    bet_recs = result.get("bet_recommendations", [])
    nrfi_rec = next((r for r in bet_recs if r.get("market") == "NRFI"), {})

    return {
        "home_team":      result.get("team_home"),
        "away_team":      result.get("team_away"),
        "home_starter":   result.get("starters", {}).get("home", {}).get("name"),
        "away_starter":   result.get("starters", {}).get("away", {}).get("name"),
        "game_date":      result.get("date"),
        "model":          nrfi_market.get("model", "unknown"),
        "p_nrfi":         p_nrfi,
        "p_yrfi":         p_yrfi,
        "verdict":        nrfi_rec.get("verdict", "SKIP"),
        "confidence":     nrfi_rec.get("confidence"),
        "kelly":          nrfi_rec.get("kelly"),
        "reasons":        nrfi_rec.get("reasons", []),
        "note":           nrfi_market.get("note", ""),
    }


# ── Public /v1 API (API-key gated) ──────────────────────────────────────────
# Read-only, programmatic access for syndicates and media clients. Reads the
# committed daily prediction files (the persistent, auditable record) rather
# than the live DB. Gate with PREDICTA_API_KEYS; open dev-mode when unset.

@app.get("/v1/status")
def v1_status(client: str = Depends(require_api_key)):
    """Health + data coverage. Confirms the caller's key is valid."""
    import nrfi_store
    dates = nrfi_store.list_dates()
    return {
        "ok":            True,
        "client":        client,
        "auth_enabled":  auth_enabled(),
        "dates_covered": len(dates),
        "first_date":    dates[0] if dates else None,
        "latest_date":   dates[-1] if dates else None,
    }


@app.get("/v1/nrfi/predictions")
def v1_nrfi_predictions(
    date: Optional[str] = None,
    client: str = Depends(require_api_key),
):
    """
    All NRFI prediction records for a date (default: latest available).
    GET /v1/nrfi/predictions?date=2026-07-02
    """
    import nrfi_store
    target = date or nrfi_store.latest_date()
    if not target:
        raise HTTPException(404, "No prediction data available yet.")
    records = nrfi_store.load_date(target)
    if not records:
        raise HTTPException(404, f"No predictions found for {target}.")
    return {"date": target, "count": len(records), "predictions": records}


@app.get("/v1/nrfi/plays")
def v1_nrfi_plays(
    date: Optional[str] = None,
    client: str = Depends(require_api_key),
):
    """BET/LEAN plays only for a date (default: latest available)."""
    import nrfi_store
    target = date or nrfi_store.latest_date()
    if not target:
        raise HTTPException(404, "No prediction data available yet.")
    records = nrfi_store.load_date(target)
    plays = [r for r in records
             if r.get("verdict") in ("BET", "LEAN") and not r.get("error")]
    # Strip Kelly stake-sizing fields from the public API — we publish BET/LEAN/SKIP
    # verdicts, not bet-sizing advice. Callers decide their own stake.
    stake_keys = ("kelly_half", "stake_100", "edge_pct")
    plays = [{k: v for k, v in p.items() if k not in stake_keys} for p in plays]
    return {"date": target, "count": len(plays), "plays": plays}


@app.get("/v1/nrfi/clv")
def v1_nrfi_clv(days: int = 30, client: str = Depends(require_api_key)):
    """Closing Line Value summary over the last N days (default 30)."""
    import nrfi_store
    return nrfi_store.aggregate_clv(days)


@app.get("/v1/nrfi/performance")
def v1_nrfi_performance(days: int = 3650, client: str = Depends(require_api_key)):
    """Win-rate + ROI + CLV summary over the committed prediction record."""
    import nrfi_store
    return nrfi_store.aggregate_performance(days)


@app.get("/v1/nrfi/clv-quality")
def v1_nrfi_clv_quality(days: int = 3650, client: str = Depends(require_api_key)):
    """
    CLV vs win-rate diagnostic: is positive CLV actually predictive of winners,
    or are we just following late line movement? Buckets resolved plays by CLV.
    """
    import nrfi_store
    return nrfi_store.clv_vs_winrate(days)


# ── Report ────────────────────────────────────────────────────────────────────

@app.get("/report")
def html_report():
    from report import generate_html_report
    return HTMLResponse(content=generate_html_report())


# ── Main UI ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(content=(TEMPLATES_DIR / "home.html").read_text(encoding="utf-8"))


@app.get("/sports", response_class=HTMLResponse)
def sports():
    return HTMLResponse(content=(TEMPLATES_DIR / "sports.html").read_text(encoding="utf-8"))


@app.get("/soccer", response_class=HTMLResponse)
def soccer():
    return HTMLResponse(content=(TEMPLATES_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/baseball", response_class=HTMLResponse)
def baseball_ui():
    return HTMLResponse(content=(TEMPLATES_DIR / "baseball.html").read_text(encoding="utf-8"))


@app.get("/tennis", response_class=HTMLResponse)
def tennis_ui():
    return HTMLResponse(content=(TEMPLATES_DIR / "tennis.html").read_text(encoding="utf-8"))


@app.get("/ping-pong", response_class=HTMLResponse)
def ping_pong_ui():
    return HTMLResponse(content=(TEMPLATES_DIR / "ping_pong.html").read_text(encoding="utf-8"))


@app.get("/esports", response_class=HTMLResponse)
def esports_ui():
    return HTMLResponse(content=(TEMPLATES_DIR / "esports.html").read_text(encoding="utf-8"))


@app.get("/rugby-diag")
def rugby_diag(team: str = "Rabbitohs"):
    """
    Diagnose why ESPN rugby-league/nrl enrichment returns empty — raw HTTP
    status + response body for /teams, today's /scoreboard, and a sample
    team's /schedule, instead of the silent {} the normal pipeline path
    returns on any failure. Added 2026-07-12 after a live Railway test came
    back empty for both teams in a real, in-progress fixture.
    """
    from fetchers.rugby import diagnose
    return diagnose(team)


@app.get("/rugby", response_class=HTMLResponse)
def rugby_ui():
    return HTMLResponse(content=(TEMPLATES_DIR / "rugby.html").read_text(encoding="utf-8"))


@app.get("/ufc-diag")
def ufc_diag(fighter: str = "Jones"):
    """
    Diagnose why ufcstats.com scraping returns empty — raw HTTP status +
    response body for the alphabetical fighter listing and a sample
    fighter's detail page, instead of the silent {} the normal pipeline path
    returns on any failure. Added 2026-07-12 after a user report that no
    fighter stats ever come back — same diagnostic-first approach that
    found and fixed the rugby ESPN slug bug.
    """
    from fetchers.ufc import diagnose
    return diagnose(fighter)


@app.get("/ufc", response_class=HTMLResponse)
def ufc_ui():
    return HTMLResponse(content=(TEMPLATES_DIR / "ufc.html").read_text(encoding="utf-8"))


@app.get("/trading", response_class=HTMLResponse)
def trading():
    """Stock Market hub — splits into High Value / Low Value (Kimi review, round 6 follow-up)."""
    return HTMLResponse(content=(TEMPLATES_DIR / "trading_hub.html").read_text(encoding="utf-8"))


@app.get("/trading/high-value", response_class=HTMLResponse)
def trading_high_value():
    return HTMLResponse(content=(TEMPLATES_DIR / "trading_high_value.html").read_text(encoding="utf-8"))


@app.get("/trading/low-value", response_class=HTMLResponse)
def trading_low_value():
    return HTMLResponse(content=(TEMPLATES_DIR / "trading_low_value.html").read_text(encoding="utf-8"))


@app.get("/trading/portfolio-watch", response_class=HTMLResponse)
def trading_portfolio_watch():
    """
    Portfolio Watch (added 2026-07-19) — NOT a third systematic trading engine
    like High Value/Low Value. On-demand, news-based qualitative review for
    stocks the user already owns/watches. See models/trading/portfolio_watch.py.
    """
    return HTMLResponse(content=(TEMPLATES_DIR / "portfolio_watch.html").read_text(encoding="utf-8"))


# ── Analyze (natural language → full pipeline) ────────────────────────────────

class AnalyzeRequest(BaseModel):
    query: str


@app.post("/analyze")
def analyze(body: AnalyzeRequest):
    if not body.query.strip():
        raise HTTPException(400, "Query cannot be empty")
    from analyze import run_analysis
    result = run_analysis(body.query)
    if "error" in result and not result.get("team_a"):
        raise HTTPException(500, detail=result["error"])
    return result


class SoccerRequest(BaseModel):
    query: str
    bankroll: float = 1000.0


@app.post("/analyze-soccer")
def analyze_soccer_endpoint(body: SoccerRequest):
    """
    Soccer-specific pipeline. Live Understat + FBref enrichment, npxG-based
    Dixon-Coles with open-play/set-piece split, GK adjustment, and Elo blend.
    Falls back to AI signal estimation when xG sources are unreachable.
    The legacy /analyze endpoint stays available for the old behaviour.
    """
    if not body.query.strip():
        raise HTTPException(400, "Query cannot be empty")
    from analyze_soccer import run_soccer_analysis
    result = run_soccer_analysis(body.query, body.bankroll)
    if "error" in result and not result.get("home_team"):
        raise HTTPException(500, detail=result["error"])
    return result


# ── Soccer auto-collection admin endpoints ────────────────────────────────────

@app.get("/soccer-auto/status")
def soccer_auto_status():
    """Show whether the background loop is running + last-run timestamps."""
    from tasks.soccer_auto import status
    return status()


@app.post("/soccer-auto/scan")
def soccer_auto_scan(days_ahead: int = 3):
    """Manually trigger a fixture-scan-and-predict pass."""
    from tasks.soccer_auto import scan_fixtures
    return scan_fixtures(days_ahead=days_ahead)


@app.post("/soccer-auto/resolve")
def soccer_auto_resolve():
    """Manually trigger a resolve-finished pass. Fetches ESPN results for
    every soccer prediction whose scheduled_at is in the past + no outcome
    row, then calls record_outcome."""
    from tasks.soccer_auto import resolve_finished
    return resolve_finished()


@app.post("/soccer-auto/report")
def soccer_auto_report():
    """Manually generate the weekly report. Writes reports/soccer_YYYY_WW.md
    and pushes to GitHub if GITHUB_TOKEN + GITHUB_REPO env vars are set."""
    from tasks.soccer_auto import weekly_report
    return weekly_report()


@app.get("/soccer-auto/report/latest")
def soccer_auto_report_latest():
    """Return the contents of the most recent weekly report as JSON."""
    from tasks.soccer_auto import REPORTS_DIR
    files = sorted(REPORTS_DIR.glob("soccer_*.md"), reverse=True)
    if not files:
        raise HTTPException(404, "No reports generated yet — call POST /soccer-auto/report first")
    return {
        "filename": files[0].name,
        "content":  files[0].read_text(encoding="utf-8"),
    }


class BaseballRequest(BaseModel):
    query: str
    bankroll: float = 1000.0
    odds_a: float = 1.909   # decimal odds for team_a (default ≈ -110)
    odds_b: float = 1.909   # decimal odds for team_b (default ≈ -110)


@app.post("/analyze-baseball")
def analyze_baseball(body: BaseballRequest):
    if not body.query.strip():
        raise HTTPException(400, "Query cannot be empty")
    from analyze_baseball import run_baseball_analysis
    result = run_baseball_analysis(body.query, body.bankroll, body.odds_a, body.odds_b)
    if "error" in result and not result.get("team_a"):
        raise HTTPException(500, detail=result["error"])
    return result


class PlayerImpactRequest(BaseModel):
    name: str
    team: str = ""


@app.post("/player-impact")
def player_impact(body: PlayerImpactRequest):
    from models.player_impact import compute_impact
    result = compute_impact(body.name.strip(), team_abbr=body.team.strip() or None)
    if "error" in result:
        raise HTTPException(404, detail=result["error"])
    return result


@app.post("/hitter-impact")
def hitter_impact(body: PlayerImpactRequest):
    from models.player_impact import compute_hitter_impact
    result = compute_hitter_impact(body.name.strip(), team_abbr=body.team.strip() or None)
    if "error" in result:
        raise HTTPException(404, detail=result["error"])
    return result


class HRPropRequest(BaseModel):
    name: str
    team: str = ""


@app.post("/hr-prop")
def hr_prop(body: HRPropRequest):
    from models.hr_prop import estimate_hr_probability
    result = estimate_hr_probability(body.name.strip(), team_abbr=body.team.strip() or None)
    if "error" in result:
        raise HTTPException(404, detail=result["error"])
    return result


@app.get("/player-search")
def player_search(q: str = "", type: str = ""):
    if not q.strip() or len(q.strip()) < 2:
        return {"results": []}
    if type == "hitter":
        from models.player_impact import search_hitters
        return {"results": search_hitters(q.strip())}
    if type == "pitcher":
        from models.player_impact import search_pitchers
        return {"results": search_pitchers(q.strip())}
    from models.player_impact import search_players
    return {"results": search_players(q.strip())}


class TennisRequest(BaseModel):
    query: str
    bankroll: float = 1000.0


@app.post("/analyze-tennis")
def analyze_tennis(body: TennisRequest):
    if not body.query.strip():
        raise HTTPException(400, "Query cannot be empty")
    from analyze_tennis import run_tennis_analysis
    result = run_tennis_analysis(body.query, body.bankroll)
    if "error" in result and not result.get("player_a"):
        raise HTTPException(500, detail=result["error"])
    return result


class TableTennisRequest(BaseModel):
    query: str
    bankroll: float = 1000.0
    # Opening line (American odds) — if provided, enables line movement signal
    open_odds_a: Optional[float] = None
    open_odds_b: Optional[float] = None
    # Current line — defaults to open_odds if not separately supplied
    curr_odds_a: Optional[float] = None
    curr_odds_b: Optional[float] = None
    # Matches already played today before this one (fatigue signal)
    matches_today_a: int = 0
    matches_today_b: int = 0


@app.post("/analyze-table-tennis")
def analyze_table_tennis(body: TableTennisRequest):
    if not body.query.strip():
        raise HTTPException(400, "Query cannot be empty")
    from analyze_table_tennis import run_table_tennis_analysis
    result = run_table_tennis_analysis(
        body.query,
        bankroll=body.bankroll,
        open_odds_a=body.open_odds_a,
        open_odds_b=body.open_odds_b,
        curr_odds_a=body.curr_odds_a,
        curr_odds_b=body.curr_odds_b,
        matches_today_a=body.matches_today_a,
        matches_today_b=body.matches_today_b,
    )
    if "error" in result and not result.get("player_a"):
        raise HTTPException(500, detail=result["error"])
    return result


class RugbyRequest(BaseModel):
    query: str
    bankroll: float = 1000.0


@app.post("/analyze-rugby")
def analyze_rugby_endpoint(body: RugbyRequest):
    """
    NRL (Rugby League) pipeline. ESPN schedule-derived points-for/against +
    Negative-Binomial split score model + Elo blend. See analyze_rugby.py.
    """
    if not body.query.strip():
        raise HTTPException(400, "Query cannot be empty")
    from analyze_rugby import run_rugby_analysis
    result = run_rugby_analysis(body.query, body.bankroll)
    if "error" in result and not result.get("home_team"):
        raise HTTPException(500, detail=result["error"])
    return result


@app.post("/rugby-auto/resolve")
def rugby_auto_resolve():
    """
    Manually trigger a resolve-finished pass for rugby predictions: finds
    matches with scheduled_at in the past and no outcome row, looks up the
    real result via ESPN, and records it (updates Elo too). Added 2026-07-12
    so BET/LEAN/PASS predictions can actually be graded against real results
    before betting real money — no scheduler yet, call this manually
    (or wire a cron later, same as tasks/nrfi_auto.py does for baseball).
    """
    from tasks.rugby_auto import resolve_finished
    return resolve_finished()


@app.get("/rugby-performance")
def rugby_performance():
    """
    Rugby model performance report — resolved predictions, Brier score,
    BET/LEAN hit rate + implied ROI, calibration buckets. Same
    "prove it before staking real money" purpose as GET /tt-performance.
    Call POST /rugby-auto/resolve first to grade any pending predictions.
    """
    from tasks.rugby_auto import compute_metrics
    metrics = compute_metrics()
    n = metrics.get("resolved", 0)
    if n < 10:
        return {
            "verdict": "NOT ENOUGH DATA",
            "n_resolved": n,
            "needed": 10,
            "message": (
                f"Only {n} resolved rugby predictions. Need at least 10 to compute "
                "meaningful metrics. Keep running predictions and call "
                "POST /rugby-auto/resolve after each round of games."
            ),
            "next_step": "Run 20+ predictions (with market odds where possible), resolve them, then call this endpoint.",
        }

    bet = metrics.get("bet") or {}
    brier = metrics.get("avg_brier") or 0.25
    roi = bet.get("implied_roi")
    hit_rate = bet.get("hit_rate")

    if roi is not None and roi > 0.05:
        verdict = "EDGE PROVEN"
        note = f"BET picks ROI +{roi*100:.1f}% across {bet.get('n', 0)} bets. Still small-sample — keep tracking."
    elif roi is not None and roi > 0:
        verdict = "EDGE EXISTS"
        note = f"BET picks ROI +{roi*100:.1f}% — thin edge, not conclusive. Need 50+ BET picks to separate skill from variance."
    elif hit_rate is not None and hit_rate > 0.55:
        verdict = "CALIBRATED — CHECK MARKET ODDS"
        note = f"BET hit rate {hit_rate*100:.1f}% without full odds coverage — supply odds on more queries to compute real ROI."
    else:
        verdict = "NO EDGE DETECTED"
        hr_pct = hit_rate * 100 if hit_rate is not None else 0.0
        note = f"BET hit rate {hr_pct:.1f}%, Brier {brier:.3f}. Not outperforming yet."

    return {
        "verdict": verdict, "note": note, "n_resolved": n,
        "avg_brier": brier, "bet": bet, "lean": metrics.get("lean"),
        "calibration": metrics.get("calibration"),
        "full_metrics": metrics,
    }


@app.get("/tennis-auto/status")
def tennis_auto_status():
    """Show whether the background tennis auto-resolve loop is running,
    plus its last-run timestamp and resolve interval."""
    from tasks.tennis_auto import status
    return status()


@app.post("/tennis-auto/resolve")
def tennis_auto_resolve():
    """
    Manually trigger a resolve-finished pass for tennis predictions right
    now, instead of waiting for the background loop's next 3 h tick: finds
    matches with scheduled_at in the past and no outcome row, looks up the
    real result via ESPN, and records it (updates Glicko-2 too). Added
    2026-07-23; the background loop (tasks.tennis_auto.start_tennis_auto,
    started at app startup) calls this same function automatically every
    3 h, so manual calls are only needed to force an immediate resolve.
    """
    from tasks.tennis_auto import resolve_finished
    return resolve_finished()


@app.get("/tennis-performance")
def tennis_performance():
    """
    Tennis model performance report — resolved predictions, Brier score,
    pick hit rate, calibration buckets, and a breakdown by data_confidence
    tier. Same "prove it before staking real money" purpose as
    GET /rugby-performance. The background loop resolves finished matches
    every 3 h automatically (see GET /tennis-auto/status); call
    POST /tennis-auto/resolve to force an immediate resolve instead of
    waiting for the next tick.
    """
    from tasks.tennis_auto import compute_metrics
    metrics = compute_metrics()
    n = metrics.get("resolved", 0)
    if n < 10:
        return {
            "verdict": "NOT ENOUGH DATA",
            "n_resolved": n,
            "needed": 10,
            "message": (
                f"Only {n} resolved tennis predictions. Need at least 10 to compute "
                "meaningful metrics. Keep running predictions — the background "
                "loop resolves finished matches automatically every 3 h."
            ),
            "next_step": "Run 20+ predictions and wait for them to resolve, then call this endpoint.",
        }

    picks = metrics.get("picks") or {}
    brier = metrics.get("avg_brier") or 0.25
    hit_rate = picks.get("hit_rate")

    if hit_rate is not None and hit_rate > 0.55:
        verdict = "CALIBRATED"
        note = f"Pick hit rate {hit_rate*100:.1f}% across {picks.get('n', 0)} picks, Brier {brier:.3f}."
    else:
        verdict = "NO EDGE DETECTED"
        hr_pct = hit_rate * 100 if hit_rate is not None else 0.0
        note = f"Pick hit rate {hr_pct:.1f}%, Brier {brier:.3f}. Not outperforming yet."

    return {
        "verdict": verdict, "note": note, "n_resolved": n,
        "avg_brier": brier, "picks": picks,
        "by_data_confidence": metrics.get("by_data_confidence"),
        "calibration": metrics.get("calibration"),
        "full_metrics": metrics,
    }


class UFCRequest(BaseModel):
    query: str
    bankroll: float = 1000.0


@app.post("/analyze-ufc")
def analyze_ufc_endpoint(body: UFCRequest):
    """
    UFC pipeline. ufcstats.com scrape + Glicko-2/stats-logistic blend for win
    probability, plus a separate method-of-victory (KO/sub/decision) model.
    See analyze_ufc.py.
    """
    if not body.query.strip():
        raise HTTPException(400, "Query cannot be empty")
    from analyze_ufc import run_ufc_analysis
    result = run_ufc_analysis(body.query, body.bankroll)
    if "error" in result and not result.get("fighter_a"):
        raise HTTPException(500, detail=result["error"])
    return result


class EsportsRequest(BaseModel):
    query: str
    bankroll: float = 1000.0
    odds_a_american: Optional[float] = None
    odds_b_american: Optional[float] = None


@app.post("/analyze-esports")
def analyze_esports(body: EsportsRequest):
    if not body.query.strip():
        raise HTTPException(400, "Query cannot be empty")
    from analyze_esports import run_esports_analysis
    result = run_esports_analysis(
        body.query,
        bankroll=body.bankroll,
        odds_a_american=body.odds_a_american,
        odds_b_american=body.odds_b_american,
    )
    if "error" in result and not result.get("team_a"):
        raise HTTPException(500, detail=result["error"])
    return result


class TradeRequest(BaseModel):
    query: str
    bankroll: float = 10000.0


# Phrases that mean "scan the watchlist" rather than "analyze this one ticker".
# Multi-word phrases only (not bare "scan"/"watchlist") to avoid mis-firing on
# an actual ticker query. Checked before ticker parsing so there's no fallback
# risk of these being misread as a symbol.
_BRIEF_TRIGGERS = (
    "brief", "scan the market", "scan market", "market scan", "market brief",
    "any signals", "check the market", "today's picks", "todays picks",
    "scan watchlist", "scan my watchlist",
)


@app.post("/analyze-trade")
def analyze_trade(body: TradeRequest):
    if not body.query.strip():
        raise HTTPException(400, "Query cannot be empty")

    q_lower = body.query.strip().lower()
    if any(trig in q_lower for trig in _BRIEF_TRIGGERS):
        from models.trading.screener import run_screener
        from fetchers.high_value_runner import get_active_watchlist
        result = run_screener(symbols=get_active_watchlist())
        result["mode"] = "brief"
        return result

    from analyze_trading import run_trade_analysis
    result = run_trade_analysis(body.query, body.bankroll)
    if "error" in result:
        raise HTTPException(500, detail=result["error"])
    result["mode"] = "single"
    return result


# ── Intraday / Alpaca endpoints ───────────────────────────────────────────────

class ScanRequest(BaseModel):
    symbols: Optional[list[str]] = None
    use_movers: bool = False


@app.post("/scan")
def scan_market(body: ScanRequest):
    from models.trading.screener import run_screener
    result = run_screener(body.symbols, body.use_movers)
    return result


class IntradayRequest(BaseModel):
    symbol: str
    bankroll: float = 10000.0


@app.post("/intraday")
def intraday_analysis(body: IntradayRequest):
    from fetchers.alpaca import get_bars, get_daily_bars, get_snapshot
    from models.trading.high_value.intraday import compute_intraday_signals, _avg_daily_volume

    snap = get_snapshot(body.symbol)
    if "error" in snap:
        raise HTTPException(400, snap["error"])
    intraday = get_bars(body.symbol, "5Min", 78)
    daily = get_daily_bars(body.symbol, 60)
    avg_vol = sum(b.get("v", 0) for b in daily[-20:]) / 20 if daily else 1_000_000
    result = compute_intraday_signals(intraday, daily, snap, avg_vol, symbol=body.symbol)
    if "error" in result:
        raise HTTPException(400, result["error"])

    # Kelly position sizing
    from models.trading.shared.kelly import kelly_from_signals
    score = result["score"]["value"]
    atr_pct = result["levels"].get("atr", 0) / snap["price"] * 100 if snap.get("price") else 1.5
    kelly = kelly_from_signals(score, atr_pct, body.bankroll)

    return {
        "symbol": body.symbol,
        "snapshot": snap,
        "intraday_bars": intraday[-30:],
        "score": result["score"],
        "signals": result["signals"],
        "levels": result["levels"],
        "kelly": kelly,
    }


class OrderRequest(BaseModel):
    symbol: str
    qty: float
    side: str               # "buy" or "sell"
    order_type: str = "limit"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    use_bracket: bool = False
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None


@app.post("/trade/order", status_code=201)
def place_trade(body: OrderRequest):
    from fetchers.alpaca import place_order, place_bracket_order
    from fetchers.trading_logger import log_manual_override
    if body.use_bracket and body.take_profit and body.stop_loss:
        result = place_bracket_order(
            body.symbol, body.qty, body.side,
            body.limit_price, body.take_profit, body.stop_loss,
        )
    else:
        result = place_order(
            body.symbol, body.qty, body.side, body.order_type,
            body.limit_price, body.stop_price,
        )
    try:
        log_manual_override("MANUAL_ORDER", body.symbol, body.dict())
    except Exception:
        pass  # logging the override must never block the actual order
    if "error" in result:
        raise HTTPException(400, result.get("detail") or result["error"])
    return result


@app.get("/trade/manual-overrides")
def manual_overrides(days: int = 30):
    """Jane Street 'never override the computer' visibility — every manual order, logged."""
    from fetchers.trading_logger import get_manual_overrides
    overrides = get_manual_overrides(days)
    return {"period_days": days, "count": len(overrides), "overrides": overrides}


@app.get("/trade/orders")
def list_orders(status: str = "open"):
    from fetchers.alpaca import get_orders
    return get_orders(status)


@app.delete("/trade/orders/{order_id}")
def cancel_trade(order_id: str):
    from fetchers.alpaca import cancel_order
    return cancel_order(order_id)


@app.get("/trade/positions")
def get_positions():
    from fetchers.alpaca import get_positions
    return get_positions()


@app.get("/trade/account")
def get_account():
    from fetchers.alpaca import get_account
    return get_account()


# ── Signal-driven smart order ─────────────────────────────────────────────────

class SmartOrderRequest(BaseModel):
    symbol: str
    account_value: float = 10000.0
    hold_bars: int = 6          # 5-min bars to hold (6 = 30-min scalp)
    min_score: float = 30.0     # minimum |score| to trade (default 30)


@app.post("/trade/smart-order", status_code=201)
def smart_trade(body: SmartOrderRequest):
    """
    Signal-driven bracket order. Computes intraday signals, applies
    liquidity and time-of-day vetoes, sizes position via intraday EM,
    places bracket order with Alpaca, and logs the entry automatically.

    Rejection reasons (status=REJECTED): liquidity fail, lunch chop,
    weak signal (|score| < min_score), missing trade levels.
    """
    from fetchers.alpaca import get_snapshot, get_bars, get_daily_bars, place_bracket_order, friendly_order_error
    from models.trading.high_value.intraday import compute_intraday_signals
    from fetchers.trading_logger import log_trade_entry

    # ── Data fetch ────────────────────────────────────────────────────────────
    snap = get_snapshot(body.symbol)
    if "error" in snap:
        raise HTTPException(400, snap["error"])

    intraday = get_bars(body.symbol, "5Min", 78)
    daily    = get_daily_bars(body.symbol, days=60)
    avg_vol  = sum(b.get("v", 0) for b in daily[-20:]) / 20 if len(daily) >= 20 else 1_000_000

    # ── Signal computation ────────────────────────────────────────────────────
    signals = compute_intraday_signals(
        intraday, daily, snap, avg_vol, hold_bars=body.hold_bars
    )
    if "error" in signals:
        raise HTTPException(400, signals["error"])

    liq   = signals["liquidity"]
    score = signals["score"]
    score_val = score["value"]

    # ── Liquidity veto ────────────────────────────────────────────────────────
    if not liq.get("pass", True):
        return {
            "status":    "REJECTED",
            "reason":    f"Liquidity gate: {liq['label']} ({liq['spread_pct']:.3f}% spread)",
            "liquidity": liq,
            "symbol":    body.symbol,
        }

    # ── Time-of-day veto ─────────────────────────────────────────────────────
    tod_label = score.get("time_label", "UNKNOWN")
    if tod_label == "MARKET_CLOSED":
        return {
            "status": "REJECTED",
            "reason": "Market closed",
            "symbol": body.symbol,
        }
    if tod_label == "LUNCH_CHOP" and abs(score_val) < 40:
        return {
            "status":    "REJECTED",
            "reason":    "Lunch chop — score below 40 conviction threshold",
            "score":     score,
            "symbol":    body.symbol,
        }

    # ── Signal gate ───────────────────────────────────────────────────────────
    if abs(score_val) < body.min_score:
        return {
            "status": "REJECTED",
            "reason": f"Score {score_val:+.1f} below min_score ±{body.min_score}",
            "score":  score,
            "symbol": body.symbol,
        }

    side = "long" if score_val > 0 else "short"

    # ── Trade levels ──────────────────────────────────────────────────────────
    levels = signals.get("levels", {})
    if not levels or not levels.get("stop") or not levels.get("target2"):
        return {
            "status": "REJECTED",
            "reason": "Trade levels not computed (insufficient bar history)",
            "symbol": body.symbol,
        }

    qty            = levels["shares"]
    entry_price    = levels["entry"]
    stop_loss      = levels["stop"]
    take_profit    = levels["target2"]
    position_value = levels["position_value"]
    risk_dollars   = levels["risk_dollars"]

    if qty <= 0:
        return {
            "status": "REJECTED",
            "reason": "Position size is zero (score below ATR sizing threshold)",
            "levels": levels,
            "symbol": body.symbol,
        }

    # ── Place bracket order ───────────────────────────────────────────────────
    alpaca_side  = "buy" if side == "long" else "sell"
    order_result = place_bracket_order(
        symbol      = body.symbol,
        qty         = qty,
        side        = alpaca_side,
        entry_price = None,           # market entry — faster fill
        take_profit = take_profit,
        stop_loss   = stop_loss,
    )
    if "error" in order_result:
        raise HTTPException(400, friendly_order_error(order_result))

    alpaca_order_id = order_result.get("id")

    # ── Log entry ─────────────────────────────────────────────────────────────
    trade_id = log_trade_entry(
        symbol          = body.symbol,
        side            = side,
        entry_price     = entry_price,
        qty             = qty,
        position_value  = position_value,
        entry_score     = score_val,
        time_of_day_label = tod_label,
        spread_pct      = liq.get("spread_pct", 0.0),
        planned_hold_bars = body.hold_bars,
        stop_price      = stop_loss,
        target_price    = take_profit,
        risk_dollars    = risk_dollars,
        alpaca_order_id = alpaca_order_id,
        signal_scores   = signals,
    )

    from fetchers import hsip_client
    hsip_client.attest_transaction(
        decision_type = alpaca_side,
        strategy_id   = "high_value_intraday_manual",
        model_version = "v4",
        payload = {
            "predicta_trade_id": trade_id,
            "alpaca_order_id":   alpaca_order_id,
            "symbol":            body.symbol,
            "side":              alpaca_side,
            "qty":               qty,
            "entry_price":       entry_price,
            "stop_price":        stop_loss,
            "target_price":      take_profit,
        },
    )

    return {
        "status":             "SUBMITTED",
        "predicta_trade_id":  trade_id,
        "alpaca_order_id":    alpaca_order_id,
        "symbol":             body.symbol,
        "side":               side,
        "qty":                qty,
        "entry":              entry_price,
        "stop":               stop_loss,
        "target":             take_profit,
        "position_value":     position_value,
        "risk_dollars":       risk_dollars,
        "score":              score,
        "liquidity":          liq,
        "rr_ratio":           levels.get("rr_ratio"),
        "stop_basis":         levels.get("stop_basis"),
    }


# ── Exit sync ─────────────────────────────────────────────────────────────────

def _determine_exit_reason(order: dict) -> str:
    """
    Map Alpaca bracket order leg status to our exit reason codes.
    Bracket orders have legs: take-profit (limit) and stop-loss (stop).
    """
    legs = order.get("legs") or []
    for leg in legs:
        if leg.get("status") == "filled":
            otype = (leg.get("type") or leg.get("order_type") or "").lower()
            if "limit" in otype:
                return "TARGET2"
            if "stop" in otype:
                return "STOP"
    # Parent order cancelled or manually closed
    if order.get("status") == "canceled":
        return "MANUAL_CANCEL"
    return "MANUAL"


@app.post("/trade/sync-exits")
def sync_trade_exits():
    """
    Poll Alpaca for filled/closed bracket orders and log exits for any
    open trade records we have on file. Call every 5–10 minutes while
    the market is open, or after session close.
    """
    from fetchers.alpaca import get_orders
    from fetchers.trading_logger import log_trade_exit

    closed_orders = get_orders(status="closed")
    if not closed_orders:
        return {"synced": 0, "checked": 0, "message": "No closed orders from Alpaca"}

    synced = 0
    skipped = 0
    errors = []

    for order in closed_orders:
        alpaca_id = order.get("id")
        if not alpaca_id:
            continue

        with get_db() as conn:
            row = conn.execute(
                "SELECT id FROM intraday_trades WHERE alpaca_order_id = ? AND exit_time IS NULL",
                (alpaca_id,),
            ).fetchone()
            if not row:
                skipped += 1
                continue
            trade_id = row["id"]

        fill_price = order.get("filled_avg_price")
        if not fill_price:
            skipped += 1
            continue

        try:
            fill_price = float(fill_price)
        except (TypeError, ValueError):
            errors.append({"alpaca_id": alpaca_id, "error": "bad fill price"})
            continue

        exit_reason = _determine_exit_reason(order)
        result = log_trade_exit(
            trade_id    = trade_id,
            exit_price  = fill_price,
            exit_reason = exit_reason,
        )
        if "error" in result:
            errors.append({"trade_id": trade_id, "error": result["error"]})
        else:
            synced += 1

    return {
        "synced":  synced,
        "skipped": skipped,
        "checked": len(closed_orders),
        "errors":  errors,
    }


# ── Performance analytics ─────────────────────────────────────────────────────

@app.get("/trade/performance")
def trade_performance(days: int = 7):
    """
    Paper trading performance for the last N days.
    Returns win rate, P&L, adjusted P&L (slippage-corrected), and
    breakdown by time-of-day session and exit reason.
    """
    from fetchers.trading_logger import get_trade_stats
    return get_trade_stats(days)


# ── Signal-only / dry-run mode ────────────────────────────────────────────────

@app.post("/trade/signal-only", status_code=201)
def signal_only(body: SmartOrderRequest):
    """
    Log signal + hypothetical trade WITHOUT placing any Alpaca order.
    Use for Week 1-2 to validate signal quality before risking capital.

    Also logs WOULD_REJECT signals so you can study what the model filtered
    out — important for catching over-rejection bias in the lunch/liquidity gates.

    Resolve outcomes at end of session via POST /trade/resolve-hypothetical/{id}.
    """
    from fetchers.alpaca import get_snapshot, get_bars, get_daily_bars
    from models.trading.high_value.intraday import compute_intraday_signals
    from fetchers.trading_logger import log_hypothetical_trade

    snap = get_snapshot(body.symbol)
    if "error" in snap:
        raise HTTPException(400, snap["error"])

    intraday = get_bars(body.symbol, "5Min", 78)
    daily    = get_daily_bars(body.symbol, days=60)
    avg_vol  = sum(b.get("v", 0) for b in daily[-20:]) / 20 if len(daily) >= 20 else 1_000_000

    signals   = compute_intraday_signals(intraday, daily, snap, avg_vol, hold_bars=body.hold_bars)
    if "error" in signals:
        raise HTTPException(400, signals["error"])

    liq       = signals["liquidity"]
    score     = signals["score"]
    score_val = score["value"]
    tod_label = score.get("time_label", "UNKNOWN")
    levels    = signals.get("levels", {})
    side      = "long" if score_val >= 0 else "short"

    # Determine what the smart-order gate WOULD have done
    would_reject = None
    if not liq.get("pass", True):
        would_reject = f"Liquidity gate: {liq['label']} ({liq.get('spread_pct', 0):.3f}% spread)"
    elif tod_label == "MARKET_CLOSED":
        would_reject = "Market closed"
    elif tod_label == "LUNCH_CHOP" and abs(score_val) < 40:
        would_reject = "Lunch chop — score below 40 conviction threshold"
    elif abs(score_val) < body.min_score:
        would_reject = f"|score| {abs(score_val):.1f} < min_score {body.min_score}"
    elif not levels or not levels.get("stop"):
        would_reject = "No trade levels (insufficient bar history)"

    trade_id = log_hypothetical_trade(
        symbol        = body.symbol,
        side          = side,
        score_value   = score_val,
        signals       = signals,
        levels        = levels or {},
        hold_bars     = body.hold_bars,
    )

    return {
        "status":            "SIGNAL_ONLY_WOULD_REJECT" if would_reject else "SIGNAL_ONLY",
        "predicta_trade_id": trade_id,
        "symbol":            body.symbol,
        "side":              side,
        "entry":             levels.get("entry"),
        "stop":              levels.get("stop"),
        "target1":           levels.get("target1"),
        "target2":           levels.get("target2"),
        "score":             score,
        "liquidity":         liq,
        "would_reject":      would_reject,
        "note": (
            "Order WOULD have been rejected — logged for over-rejection analysis."
            if would_reject else
            "No order placed. Resolve outcome via POST /trade/resolve-hypothetical/{trade_id}."
        ),
    }


class HypotheticalOutcome(BaseModel):
    exit_price: float
    exit_reason: str          # "TARGET_HIT", "STOP_HIT", "TIME_EXPIRED", "MANUAL"
    actual_hold_bars: Optional[int] = None


@app.post("/trade/resolve-hypothetical/{trade_id}")
def resolve_hypothetical(trade_id: int, body: HypotheticalOutcome):
    """
    Record the outcome of a signal-only (hypothetical) trade.
    Call at end of session or when stop/target would have been hit.
    Computes the same P&L metrics as a real closed trade so results are
    directly comparable across hypothetical and paper modes.
    """
    from fetchers.trading_logger import log_trade_exit
    result = log_trade_exit(
        trade_id         = trade_id,
        exit_price       = body.exit_price,
        exit_reason      = body.exit_reason,
        actual_hold_bars = body.actual_hold_bars,
    )
    if "error" in result:
        raise HTTPException(404, result["error"])
    return {"status": "RESOLVED", **result}


# ── Pairs trading ─────────────────────────────────────────────────────────────

_KNOWN_PAIRS = [
    ("XOM", "CVX"),   # oil majors
    ("PEP", "KO"),    # beverages
    ("JPM", "BAC"),   # money center banks
    ("AAPL", "MSFT"), # big tech (weaker, included for monitoring)
]

DEFAULT_WATCHLIST = [sym for pair in _KNOWN_PAIRS for sym in pair]


class PairsScanRequest(BaseModel):
    symbols: list[str] = DEFAULT_WATCHLIST
    days: int = 120
    pvalue_threshold: float = 0.05
    account_value: float = 10_000.0
    risk_pct: float = 0.01


@app.post("/trade/pairs-scan")
def pairs_scan(body: PairsScanRequest):
    """
    Scan a watchlist for cointegrated pairs.
    Returns all pairs with ADF p-value < pvalue_threshold, sorted by strength.
    For each cointegrated pair also computes the current spread z-score.

    Typical use: run weekly to refresh the active pairs universe.
    """
    from fetchers.pairs_data import fetch_pair_history, prices_to_series
    from models.trading.pairs import find_cointegrated_pairs, pairs_signal

    symbols = [s.upper().strip() for s in body.symbols if s.strip()]
    if len(symbols) < 2:
        raise HTTPException(400, "At least 2 symbols required")

    history = fetch_pair_history(symbols, days=body.days)
    if not history:
        raise HTTPException(503, "Could not fetch price history (check Alpaca credentials)")

    series = prices_to_series(history)
    available = list(series.keys())
    if len(available) < 2:
        raise HTTPException(503, f"Price data only available for: {available}")

    pairs = find_cointegrated_pairs(series, pvalue_threshold=body.pvalue_threshold)

    results = []
    for p in pairs:
        sig = pairs_signal(
            sym1=p["sym1"], sym2=p["sym2"],
            beta=p["beta"], series=series,
        )
        results.append({
            **p,
            "current_zscore":  sig.get("zscore"),
            "signal_action":   sig.get("action"),
            "long_sym":        sig.get("long_sym"),
            "short_sym":       sig.get("short_sym"),
            "signal_note":     sig.get("note"),
        })

    return {
        "symbols_scanned": available,
        "days_history":    body.days,
        "pairs_found":     len(results),
        "pairs":           results,
    }


class PairsSignalRequest(BaseModel):
    sym1: str
    sym2: str
    beta: float
    days: int = 120
    lookback: int = 60
    entry_z: float = 2.0
    exit_z: float = 0.5
    account_value: float = 10_000.0
    risk_pct: float = 0.01


@app.post("/trade/pairs-signal")
def pairs_signal_endpoint(body: PairsSignalRequest):
    """
    Get the current spread signal and position sizing for a specific pair.
    Use after pairs-scan to get actionable entry details for a detected pair.

    Returns: signal action, z-score, long/short symbols, and position levels.
    """
    from fetchers.pairs_data import fetch_pair_history, prices_to_series
    from models.trading.pairs import pairs_signal, compute_pairs_levels

    sym1 = body.sym1.upper().strip()
    sym2 = body.sym2.upper().strip()

    history = fetch_pair_history([sym1, sym2], days=body.days)
    if not history:
        raise HTTPException(503, "Could not fetch price history")

    series = prices_to_series(history)
    if sym1 not in series or sym2 not in series:
        missing = [s for s in [sym1, sym2] if s not in series]
        raise HTTPException(404, f"No price data for: {missing}")

    sig = pairs_signal(
        sym1=sym1, sym2=sym2, beta=body.beta,
        series=series, lookback=body.lookback,
        entry_z=body.entry_z, exit_z=body.exit_z,
    )

    levels = None
    if sig["action"] in ("LONG_SPREAD", "SHORT_SPREAD"):
        long_sym  = sig["long_sym"]
        short_sym = sig["short_sym"]
        levels = compute_pairs_levels(
            long_sym=long_sym, short_sym=short_sym,
            series=series, beta=body.beta,
            zscore=sig["zscore"], spread_std=sig["spread_std"],
            account_value=body.account_value, risk_pct=body.risk_pct,
        )

    return {
        "pair":   f"{sym1}/{sym2}",
        "beta":   body.beta,
        "signal": sig,
        "levels": levels,
    }


class PairsCandidatesRequest(BaseModel):
    days: int = 90
    min_half_life: float = 1.0
    max_half_life: float = 30.0


@app.post("/trade/pairs-candidates")
def pairs_candidates(body: PairsCandidatesRequest):
    """
    Scan the 8 pre-defined CANDIDATE_PAIRS for cointegration using the
    full Engle-Granger test (statsmodels). Filters by half-life so only
    pairs that mean-revert within a tradeable window are returned.

    Run weekly (Sunday before market open) via scripts/weekly_build.py.
    Results are stable week-to-week — daily re-scanning is wasteful.

    Returns cointegrated pairs with current z-score and trade signal.
    """
    from models.trading.pairs import find_all_pairs, generate_pair_signal, CANDIDATE_PAIRS

    pairs = find_all_pairs(
        days=body.days,
        min_half_life=body.min_half_life,
        max_half_life=body.max_half_life,
    )

    results = []
    for p in pairs:
        sig = generate_pair_signal(p)
        results.append({**p, "signal": sig})

    return {
        "candidate_pairs_tested": len(CANDIDATE_PAIRS),
        "cointegrated_found":     len(results),
        "filters": {
            "days": body.days,
            "min_half_life_days": body.min_half_life,
            "max_half_life_days": body.max_half_life,
        },
        "pairs": results,
    }


class NgramBuildRequest(BaseModel):
    symbols: list[str]
    months: int = 6


@app.post("/trade/ngram-build")
def ngram_build(body: NgramBuildRequest):
    """
    Build or refresh n-gram pattern tables for a list of symbols.
    Fetches up to 10,000 5-min bars (~6 months) from Alpaca and stores
    frequency tables in the ngram_models DB table.

    Call weekly per symbol. Takes ~2s per symbol (one API fetch each).
    Returns per-symbol success/fail status.
    """
    from models.trading.high_value.ngram import build_ngram_from_alpaca

    results = {}
    for sym in body.symbols:
        sym = sym.upper().strip()
        try:
            ok = build_ngram_from_alpaca(sym, months=body.months)
            results[sym] = "built" if ok else "insufficient_data"
        except Exception as e:
            results[sym] = f"error: {e}"

    return {
        "symbols_requested": len(body.symbols),
        "results":           results,
    }


# ── Signal calibration / feedback loop ─────────────────────────────────────────

@app.get("/trade/calibration")
def calibration_status():
    """
    Readiness summary: how many closed trades exist, whether Kelly sizing is
    safe to enable, and the empirically observed score threshold for >50% win rate.

    Re-runs on every call — no caching — so it reflects the latest closed trades.
    """
    from models.trading.shared.signal_calibration import calibration_summary
    return calibration_summary()


@app.get("/trade/calibration/scores")
def calibration_scores(min_trades: int = 5):
    """
    Win rate and avg P&L (in R) per composite score bucket.
    Buckets: Strong Sell / Sell / Neutral / Buy / Strong Buy.
    Returns None for any bucket with fewer than min_trades closed trades.
    """
    from models.trading.shared.signal_calibration import score_accuracy_report
    return score_accuracy_report(min_trades=min_trades)


@app.get("/trade/calibration/time")
def calibration_time(min_trades: int = 5):
    """
    Win rate per time-of-day session (MORNING_TREND, LUNCH_CHOP, etc.).
    Useful for tuning the time-of-day modifier in intraday.py.
    """
    from models.trading.shared.signal_calibration import time_accuracy_report
    return time_accuracy_report(min_trades=min_trades)


@app.get("/trade/calibration/pairs")
def calibration_pairs(min_trades: int = 3):
    """
    Per-pair realized edge from all closed pair_signals rows.
    Returns win rate, avg P&L %, avg hold time, and UNDERPERFORMING flag
    for pairs with < 50% win rate or negative avg P&L.
    """
    from models.trading.shared.signal_calibration import pairs_calibration_summary
    return pairs_calibration_summary(min_trades=min_trades)


@app.get("/trade/ngram-validate/{symbol}")
def ngram_validate(symbol: str, significance: float = 0.05):
    """
    Run binomial significance test on all patterns in the symbol's n-gram table.
    Only patterns with p < significance have a demonstrated edge.

    Call after /trade/ngram-build to audit which patterns are statistically sound
    before trusting their directional signal in the composite score.
    """
    from models.trading.high_value.ngram import validate_ngram_patterns
    return validate_ngram_patterns(symbol.upper(), significance=significance)


class PairsExitRequest(BaseModel):
    exit_zscore: float
    exit_reason: str
    pnl_pct: Optional[float] = None


@app.post("/trade/pairs-exit/{signal_id}")
def pairs_exit(signal_id: int, body: PairsExitRequest):
    """
    Close an open pair_signals row with exit z-score, reason, and optional P&L %.
    Mirrors /trade/resolve-hypothetical for intraday trades.

    exit_reason: "TARGET_HIT" | "STOP_HIT" | "TIME_EXIT" | "MANUAL"
    pnl_pct: percentage P&L on the spread leg (positive = profit).
    """
    from models.trading.pairs import log_pair_exit
    ok = log_pair_exit(signal_id, body.exit_zscore, body.exit_reason, body.pnl_pct)
    if not ok:
        raise HTTPException(404, f"Signal {signal_id} not found or already closed")
    return {"signal_id": signal_id, "closed": True, "exit_reason": body.exit_reason}


@app.get("/trade/calibration/status")
def calibration_readiness():
    """
    Per-feature calibration readiness: which ML/sizing features are unlocked.

    Features tracked:
      kelly_sizing         — 50 closed trades
      dynamic_weights      — 30 closed trades
      per_signal_accuracy  — 10 active trades per individual signal
      ngram_blend_weight   — 20 agree + 20 disagree cohort trades
      time_session_accuracy — 20 trades per time-of-day session

    Also returns compute_dynamic_weights() and compute_ngram_blend_weight()
    when their thresholds are met.
    """
    from models.trading.shared.signal_calibration import (
        calibration_readiness_status,
        compute_dynamic_weights,
        compute_ngram_blend_weight,
        per_signal_accuracy_report,
    )
    status = calibration_readiness_status()
    status["dynamic_weights"]   = compute_dynamic_weights()
    status["ngram_blend"]       = compute_ngram_blend_weight()
    status["per_signal_report"] = per_signal_accuracy_report()
    return status


@app.get("/trade/exposure")
def trade_exposure():
    """
    Cross-engine open-position counts (Tier 2 of the 2026-07-16/17 trading-
    model audit). Read-only — each engine still gates its own concurrent
    positions independently; see models/trading/shared/exposure.py's
    docstring for why this doesn't (yet) enforce a combined limit.
    """
    from models.trading.shared.exposure import get_cross_engine_exposure
    return get_cross_engine_exposure()


@app.get("/trade/calibration/kill-switches")
def calibration_kill_switches():
    """
    Citadel 'pod kill switch' status (Kimi review, round 5). Runs
    check_signal_kill_switches() to evaluate current data, then returns
    get_signal_kill_status() for the persisted history — including which
    killed signals are now eligible for a manual resurrection review.
    """
    from models.trading.shared.signal_calibration import (
        check_signal_kill_switches, get_signal_kill_status,
    )
    live_check = check_signal_kill_switches()
    return {"live_check": live_check, "persisted": get_signal_kill_status()}


@app.post("/trade/calibration/resurrect/{signal}")
def calibration_resurrect_signal(signal: str):
    """Manually resurrect a killed signal — human-in-the-loop per the Citadel pod model."""
    from models.trading.shared.signal_calibration import resurrect_signal
    return resurrect_signal(signal)


@app.get("/trade/review-queue")
def trade_review_queue(status: Optional[str] = None, days: int = 7):
    """
    D.E. Shaw hybrid-model human review queue (Kimi review, round 6) — optional
    safety valve for EXTREME-regime days. Auto-skips after 5 min if not
    reviewed, so this is diagnostic/action, never a blocking dependency.
    """
    from fetchers.high_value_runner import check_review_queue_timeouts, get_review_queue
    check_review_queue_timeouts()
    return {"queue": get_review_queue(status=status, days=days)}


@app.post("/trade/review-queue/{review_id}/approve")
def trade_review_queue_approve(review_id: int):
    """Approve a queued EXTREME-day signal — replays the original model call into a logged trade."""
    from fetchers.high_value_runner import approve_review
    return approve_review(review_id)


@app.post("/trade/review-queue/{review_id}/skip")
def trade_review_queue_skip(review_id: int):
    """Manually skip a queued EXTREME-day signal."""
    from fetchers.high_value_runner import skip_review
    return skip_review(review_id)


class PairsSuspendRequest(BaseModel):
    reason: str = ""
    reinstate_after: Optional[str] = None   # ISO datetime UTC, or omit for indefinite


@app.post("/trade/pairs-suspend/{sym1}/{sym2}")
def pairs_suspend(sym1: str, body: PairsSuspendRequest, sym2: str):
    """
    Suspend a pair from the weekly find_all_pairs scan.

    reinstate_after: ISO datetime UTC string (e.g. "2026-09-25T00:00:00")
    or omit for indefinite suspension. Use auto_cull_pairs() to suspend
    automatically based on performance.
    """
    from models.trading.pairs import suspend_pair
    ok = suspend_pair(sym1.upper(), sym2.upper(), body.reason, body.reinstate_after)
    if not ok:
        raise HTTPException(500, "Failed to suspend pair")
    return {"pair": f"{sym1.upper()}/{sym2.upper()}", "suspended": True,
            "reinstate_after": body.reinstate_after}


@app.delete("/trade/pairs-suspend/{sym1}/{sym2}")
def pairs_reinstate(sym1: str, sym2: str):
    """Reinstate a suspended pair — removes it from the suspended_pairs table."""
    from models.trading.pairs import reinstate_pair
    ok = reinstate_pair(sym1.upper(), sym2.upper())
    if not ok:
        raise HTTPException(404, f"Pair {sym1}/{sym2} not found in suspended list")
    return {"pair": f"{sym1.upper()}/{sym2.upper()}", "reinstated": True}


@app.get("/trade/pairs-suspend")
def list_suspended_pairs():
    """List all currently suspended pairs."""
    from models.trading.pairs import get_suspended_pairs
    return {"suspended": get_suspended_pairs()}


@app.post("/trade/pairs-auto-cull")
def pairs_auto_cull(min_trades: int = 10, win_rate_floor: float = 0.40):
    """
    Auto-suspend underperforming pairs based on realized P&L data.

    Suspends any pair where (with >= min_trades closed):
      win_rate < win_rate_floor  OR  avg_pnl_pct < 0

    Pairs are suspended for 90 days, then automatically re-eligible.
    """
    from models.trading.pairs import auto_cull_pairs
    culled = auto_cull_pairs(min_trades=min_trades, win_rate_floor=win_rate_floor)
    return {"culled": culled, "n_culled": len(culled)}


# ── Paper runner endpoints ────────────────────────────────────────────────────

@app.get("/trade/paper-runner/status")
def paper_runner_status():
    """
    Status of the automated paper trading runner.
    Returns: active flag, config, open position count, last 20 log events.
    """
    from fetchers.high_value_runner import get_runner_status
    return get_runner_status()


@app.post("/trade/paper-runner/scan-now")
def paper_runner_scan_now(min_score: int = 20):
    """
    Manually trigger a signal scan outside the scheduled window.
    Useful for testing or catching afternoon setups.
    """
    from fetchers.high_value_runner import run_open_scan
    ids = run_open_scan(min_score=min_score)
    return {"logged": len(ids), "trade_ids": ids}


@app.post("/trade/execute/{trade_id}", dependencies=[Depends(require_trade_passcode)])
def execute_high_value(trade_id: int):
    """
    Human-triggered only. Places a real Alpaca paper order for a High Value
    candidate the automated scan already suggested — the scan itself never
    places an order on its own. See fetchers.high_value_runner.execute_high_value_trade.
    """
    from fetchers.high_value_runner import execute_high_value_trade
    result = execute_high_value_trade(trade_id)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.post("/trade/close/{trade_id}", dependencies=[Depends(require_trade_passcode)])
def close_high_value(trade_id: int):
    """Human-triggered only. Closes a real open High Value position early, before its stop/target/time-limit is hit."""
    from fetchers.high_value_runner import close_high_value_trade
    result = close_high_value_trade(trade_id)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.post("/trade/paper-runner/start")
def paper_runner_start():
    """Start the background runner if it is not already running."""
    from fetchers.high_value_runner import start_runner
    started = start_runner()
    return {"started": started}


@app.post("/trade/paper-runner/stop")
def paper_runner_stop():
    """Signal the background runner to stop on its next tick."""
    from fetchers.high_value_runner import stop_runner
    stop_runner()
    return {"ok": True}


@app.get("/trade/dashboard", response_class=HTMLResponse)
def trade_dashboard():
    """
    Plain-English trading monitor. No jargon — just what the numbers mean
    and whether you're ready to use real money. Auto-refreshes every 5 min.
    """
    # ── Pull raw data ──────────────────────────────────────────────────────
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT symbol, side, entry_time, exit_time, pnl_dollars,
                   adjusted_pnl, pnl_r, exit_reason, time_of_day_label,
                   entry_score, stop_price, target_price
            FROM intraday_trades
            WHERE is_hypothetical = 1 AND exit_time IS NOT NULL
            AND entry_time >= datetime('now', '-60 days')
            ORDER BY exit_time DESC
            """
        ).fetchall()
        open_rows = conn.execute(
            """
            SELECT id, symbol, side, entry_time, entry_score, entry_price,
                   stop_price, target1_price, target_price,
                   vwap_score, or_score, rsi_score, relvol_score,
                   gap_score, trend_score, bollinger_score, volsurge_score,
                   ngram_signal, ngram_confidence, is_hypothetical, alpaca_order_id
            FROM intraday_trades
            WHERE exit_time IS NULL AND engine != 'low_value'
            ORDER BY entry_time DESC
            """
        ).fetchall()

    trades  = [dict(r) for r in rows]
    open_t  = [dict(r) for r in open_rows]
    n_closed = len(trades)
    n_open   = len(open_t)

    # ── Inventory / exposure snapshot (Kimi review, Jane Street "inventory
    # risk" concept — round 5) ──────────────────────────────────────────────
    try:
        from fetchers.high_value_runner import get_active_watchlist
        with get_db() as conn:
            last_seen_rows = conn.execute(
                """
                SELECT symbol, MAX(entry_time) as last_entry
                FROM intraday_trades WHERE is_hypothetical = 1
                GROUP BY symbol
                """
            ).fetchall()
        last_seen = {r["symbol"]: r["last_entry"] for r in last_seen_rows}
        now_utc = datetime.now(timezone.utc)
        inventory_rows = []
        for sym in get_active_watchlist():
            last_entry = last_seen.get(sym)
            days_since = None
            if last_entry:
                try:
                    dt = datetime.fromisoformat(last_entry.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    days_since = (now_utc - dt).days
                except Exception:
                    days_since = None
            open_pos = next((t for t in open_t if t.get("symbol") == sym), None)
            inventory_rows.append({
                "symbol": sym,
                "open": open_pos is not None,
                "side": open_pos.get("side") if open_pos else None,
                "days_since_last_trade": days_since,
            })
        long_count  = sum(1 for t in open_t if t.get("side") == "long")
        short_count = sum(1 for t in open_t if t.get("side") == "short")
    except Exception:
        inventory_rows = []
        long_count = short_count = 0

    # Cross-engine exposure (Tier 2, 2026-07-16/17 trading-model audit) — see
    # models/trading/shared/exposure.py's docstring for why this is
    # visibility-only, not a combined position cap.
    try:
        from models.trading.shared.exposure import get_cross_engine_exposure
        cross_exposure = get_cross_engine_exposure()
    except Exception:
        cross_exposure = None

    # ── Per-signal P&L attribution (Kimi review, Citadel "pod" concept — round 5)
    try:
        from models.trading.shared.signal_calibration import per_signal_accuracy_report
        signal_report = per_signal_accuracy_report(min_trades=10, active_threshold=10.0)
    except Exception:
        signal_report = {"by_signal": [], "note": "Unavailable"}

    # ── Compute stats ──────────────────────────────────────────────────────
    win_rate = avg_pnl = avg_r = adj_pnl_total = None
    if n_closed:
        wins      = sum(1 for t in trades if (t.get("pnl_dollars") or 0) > 0)
        win_rate  = round(wins / n_closed, 3)
        avg_pnl   = round(sum(t.get("pnl_dollars") or 0 for t in trades) / n_closed, 2)
        adj_pnl_total = round(sum(t.get("adjusted_pnl") or t.get("pnl_dollars") or 0 for t in trades), 2)
        r_vals    = [t["pnl_r"] for t in trades if t.get("pnl_r") is not None]
        if r_vals:
            avg_r = round(sum(r_vals) / len(r_vals), 3)

    # Time-of-day breakdown
    tod_stats: dict = {}
    for label in ("MORNING_TREND", "AFTERNOON_TREND", "CLOSE_REVERSAL"):
        sub = [t for t in trades if t.get("time_of_day_label") == label]
        if sub:
            s_wins = sum(1 for t in sub if (t.get("pnl_dollars") or 0) > 0)
            tod_stats[label] = {
                "n": len(sub),
                "win_rate": round(s_wins / len(sub), 3),
            }

    # Exit reason breakdown
    exit_counts: dict = {}
    for t in trades:
        r = t.get("exit_reason") or "UNKNOWN"
        exit_counts[r] = exit_counts.get(r, 0) + 1

    # ── Phase + readiness ──────────────────────────────────────────────────
    if n_closed < 10:
        phase       = "WATCHING"
        phase_color = "#f59e0b"
        phase_label = "Watching"
        phase_desc  = f"The model is logging signals. You need at least 10 closed trades to see patterns. You have {n_closed} so far."
        need_more   = 10 - n_closed
    elif n_closed < 30:
        phase       = "COLLECTING"
        phase_color = "#f59e0b"
        phase_label = "Collecting Data"
        phase_desc  = f"Good start. The model needs 30 closed trades to start adjusting its own weights. You have {n_closed}."
        need_more   = 30 - n_closed
    elif n_closed < 50:
        phase       = "CALIBRATING"
        phase_color = "#38bdf8"
        phase_label = "Calibrating"
        phase_desc  = f"The model is learning which signals work for your watchlist. At 50 trades, position sizing based on real performance unlocks."
        need_more   = 50 - n_closed
    else:
        phase       = "SIZING"
        phase_color = "#a78bfa"
        phase_label = "Sized Paper Trading"
        phase_desc  = "Enough data collected. Position sizes now reflect actual signal quality."
        need_more   = 0

    # Readiness verdict
    is_ready    = False
    verdict_msg = ""
    if n_closed < 30:
        verdict_color = "#f59e0b"
        verdict_icon  = "🔴"
        verdict_title = "Not ready for real money"
        verdict_msg   = f"You need {30 - n_closed} more closed trades before the model has learned anything meaningful."
    elif win_rate is not None and win_rate < 0.50:
        verdict_color = "#ef4444"
        verdict_icon  = "🔴"
        verdict_title = "Not ready — win rate too low"
        verdict_msg   = f"Win rate of {win_rate*100:.0f}% means the model is wrong more than it's right. This would lose money in real trading. Keep collecting data."
    elif win_rate is not None and avg_r is not None and win_rate >= 0.54 and avg_r >= 0.5 and adj_pnl_total is not None and adj_pnl_total > 0:
        is_ready      = True
        verdict_color = "#22c55e"
        verdict_icon  = "🟢"
        verdict_title = "Consider graduating to real money"
        verdict_msg   = f"Win rate {win_rate*100:.0f}%, avg {avg_r:.2f}R per trade, positive spread-adjusted P&L. These are good signals. Start with very small size — the real market is harder than paper."
    elif win_rate is not None and win_rate >= 0.52:
        verdict_color = "#38bdf8"
        verdict_icon  = "🔵"
        verdict_title = "Getting there"
        verdict_msg   = f"Win rate {win_rate*100:.0f}% is a slight edge but not enough to be confident. Target is 54%+ with 0.5R+ average. Keep collecting."
    else:
        verdict_color = "#f59e0b"
        verdict_icon  = "🟡"
        verdict_title = "Too early to tell"
        verdict_msg   = "Not enough closed trades or the numbers are mixed. Keep the runner going."

    # ── Plain-English translations ─────────────────────────────────────────
    def _wr_text(wr):
        if wr is None:    return "No data yet."
        p = wr * 100
        if p < 40:   return "The model is losing more than it should. Something may be wrong."
        if p < 48:   return "Below average. Could be bad luck — keep collecting data."
        if p < 52:   return "Coin flip range. Not enough to make money yet."
        if p < 57:   return "Slight edge. Promising — keep watching."
        if p < 62:   return "Real edge. Worth serious attention."
        return "Very strong. Rare — double-check the data."

    def _r_text(r):
        if r is None:  return "No data yet."
        if r < 0:      return "Losing money on average."
        if r < 0.25:   return "Tiny edge. Not worth real money yet."
        if r < 0.5:    return "Developing edge. Getting closer."
        if r < 1.0:    return "Solid edge. Real money territory."
        return "Exceptional. Verify the numbers are correct."

    def _exit_plain(reason):
        m = {
            "STOP_LOSS":       "Hit stop → loss",
            "TARGET_1_HIT":    "Hit target 1 → partial win",
            "TARGET_2_HIT":    "Hit full target → full win",
            "TIME_STOP":       "Timed out — no strong move",
            "FORCE_CLOSE_EOD": "Closed at end of day",
        }
        return m.get(reason, reason)

    def _tod_plain(label):
        m = {
            "MORNING_TREND":   "Morning (9:35–11 AM)",
            "AFTERNOON_TREND": "Afternoon (1–3 PM)",
            "CLOSE_REVERSAL":  "Near Close (3–4 PM)",
        }
        return m.get(label, label)

    # Added 2026-07-18, direct user request — same plain-language treatment
    # already built for the Low Value dashboard (fetchers/low_value_dashboard.py:
    # _plain_why/_plain_confidence/_plain_exit_instructions), ported to High
    # Value. High Value already stored stop_price/target_price/target1_price
    # and every individual signal's raw score per trade, but the open-position
    # card only ever showed "BUY · score 62" — no why, no target, no stop.
    # Reconstructs a plain "why" from the stored per-signal scores (the
    # human-readable label text intraday.py's _sig_* functions generate at
    # scan time is never persisted, only the numeric score, so this is a
    # sign/magnitude-based re-translation grounded in what each signal
    # literally measures — not a byte-exact replay of the original label).
    def _hv_signal_phrase(name, score):
        if score is None or abs(score) < 10:
            return None
        pos = score > 0
        if name == "vwap":
            return ("trading above its average price today, a sign of buying pressure" if pos else
                    "trading below its average price today, a sign of selling pressure")
        if name == "or":
            return ("broken out above this morning's early trading range" if pos else
                    "broken down below this morning's early trading range")
        if name == "rsi":
            if abs(score) >= 20:
                return ("oversold on a short-term basis and due for a bounce" if pos else
                        "overbought on a short-term basis and due for a pullback")
            return "showing short-term bullish momentum" if pos else "showing short-term bearish momentum"
        if name == "relvol":
            return ("trading on unusually heavy volume for this time of day" if pos else
                    "trading on unusually light volume for this time of day")
        if name == "gap":
            return "today's opening gap supports the bullish case" if pos else "today's opening gap supports the bearish case"
        if name == "trend":
            return ("above its short-term trend average, a bullish backdrop" if pos else
                    "below its short-term trend average, a bearish backdrop")
        if name == "bollinger":
            return ("near the bottom of its recent trading range, a spot that often bounces" if pos else
                    "stretched near the top of its recent trading range, arguably overbought")
        if name == "volsurge":
            return "seeing a burst of volume in the last few minutes" if pos else None
        return None

    def _hv_plain_why(t):
        cols = [
            ("vwap", t.get("vwap_score")), ("or", t.get("or_score")), ("rsi", t.get("rsi_score")),
            ("relvol", t.get("relvol_score")), ("gap", t.get("gap_score")), ("trend", t.get("trend_score")),
            ("bollinger", t.get("bollinger_score")), ("volsurge", t.get("volsurge_score")),
        ]
        scored = [(name, sc, _hv_signal_phrase(name, sc)) for name, sc in cols]
        scored = [(name, sc, ph) for name, sc, ph in scored if ph]
        scored.sort(key=lambda x: abs(x[1]), reverse=True)
        top = scored[:2]
        if not top:
            return "No single signal stood out strongly — this entry came from several smaller factors adding up together."
        sentence = " and ".join(ph for _, _, ph in top)
        sentence = sentence[0].upper() + sentence[1:] + "."
        ngram_signal = t.get("ngram_signal")
        ngram_conf = t.get("ngram_confidence")
        if ngram_signal and ngram_signal != "NONE" and ngram_conf and ngram_conf > 20:
            side_word = "UP" if t.get("side") == "long" else "DOWN"
            if ngram_signal == side_word:
                sentence += " A recurring short-term price pattern also points the same direction."
        return sentence

    def _hv_plain_confidence(score):
        if score is None:
            return "Confidence: unknown"
        a = abs(score)
        if a >= 60:
            return "Confidence: very strong — multiple signals strongly agree"
        if a >= 20:
            return "Confidence: moderate — the minimum bar the model requires to act at all"
        return "Confidence: weak"

    def _hv_plain_exit(t):
        side = t.get("side")
        stop = t.get("stop_price")
        target1 = t.get("target1_price")
        target = t.get("target_price")
        lines = []
        if target:
            lines.append(f"✅ {'Sell' if side == 'long' else 'Buy it back'} for a profit if the price "
                         f"{'rises' if side == 'long' else 'drops'} to ${target:,.2f}.")
        if target1 and target1 != target:
            lines.append(f"🎯 A partial profit-taking price is ${target1:,.2f} — the model may "
                         f"{'sell' if side == 'long' else 'buy back'} part of the position there first.")
        if stop:
            lines.append(f"⚠️ {'Sell' if side == 'long' else 'Buy it back'} to limit the loss if the price "
                         f"{'drops' if side == 'long' else 'rises'} to ${stop:,.2f}.")
        lines.append("⏰ If neither of those happens, the position closes automatically by the end of "
                     "today's trading session — High Value never holds overnight.")
        return "".join(f"<div>{l}</div>" for l in lines)

    def _pnl_color(pnl):
        if pnl is None: return "#64748b"
        return "#22c55e" if pnl >= 0 else "#ef4444"

    def _pnl_sign(pnl):
        if pnl is None: return "?"
        return f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"

    # Runner status
    try:
        from fetchers.high_value_runner import get_runner_status, _is_market_open
        rs = get_runner_status()
        runner_active = rs.get("active", False)
        runner_open   = rs.get("open_positions", 0)
        market_is_open = rs.get("market_open", False)
        universe_mode  = rs.get("universe_mode", "large_cap")
        low_price_ceiling = rs.get("low_price_ceiling")
    except Exception:
        runner_active = False
        runner_open   = 0
        market_is_open = False
        universe_mode  = "large_cap"
        low_price_ceiling = None

    try:
        from fetchers.high_value_runner import _et_now
        now_str = _et_now().strftime("%I:%M %p ET, %b %d")
    except Exception:
        now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")

    # Progress bar for phase
    phase_targets = {"WATCHING": 10, "COLLECTING": 30, "CALIBRATING": 50, "SIZING": 50}
    phase_target  = phase_targets.get(phase, 50)
    progress_pct  = min(100, int(n_closed / phase_target * 100))

    # ── Build HTML ─────────────────────────────────────────────────────────
    recent_html = ""
    for t in trades[:8]:
        pnl       = t.get("pnl_dollars")
        icon      = "✅" if (pnl or 0) >= 0 else "❌"
        sym       = t.get("symbol", "?")
        side_lbl  = "BUY" if t.get("side") == "long" else "SELL"
        reason    = _exit_plain(t.get("exit_reason") or "")
        ex_time   = (t.get("exit_time") or "")[:16].replace("T", " ")
        recent_html += f"""
        <div class="trade-row">
          <span class="trade-icon">{icon}</span>
          <div class="trade-info">
            <span class="trade-sym">{sym}</span>
            <span class="trade-detail">{side_lbl} → {reason}</span>
            <span class="trade-time">{ex_time}</span>
          </div>
          <span class="trade-pnl" style="color:{_pnl_color(pnl)}">{_pnl_sign(pnl)}</span>
        </div>"""

    open_html = ""
    for t in open_t[:5]:
        tid   = t.get("id")
        is_real = not t.get("is_hypothetical", 1)
        sym   = t.get("symbol", "?")
        side  = "BUY" if t.get("side") == "long" else "SELL"
        score = t.get("entry_score") or 0
        entry_price = t.get("entry_price")
        entry_str = (t.get("entry_time") or "")[:16].replace("T", " ")
        if is_real:
            action_html = (
                f'<span class="real-badge">🔴 REAL ORDER PLACED</span> '
                f'<button class="trade-btn sell-btn" onclick="predictaAction(\'/trade/close/{tid}\', \'SELL — close this position now\')">Sell Now</button>'
            )
        else:
            action_html = (
                f'<button class="trade-btn buy-btn" onclick="predictaAction(\'/trade/execute/{tid}\', \'{side} {sym} — place a real paper order\')">{side} — place real order</button>'
            )
        open_html += f"""
        <div class="trade-row">
          <span class="trade-icon">⏳</span>
          <div class="trade-info">
            <span class="trade-sym">{side} {sym}</span>
            <div class="trade-why">{_hv_plain_why(t)}</div>
            <span class="trade-detail">{"Bought" if t.get("side") == "long" else "Shorted"} at ${entry_price:,.2f}/share · {_hv_plain_confidence(score)}</span>
            {_hv_plain_exit(t)}
            <span class="trade-time">Entered {entry_str}</span>
            <div style="margin-top:8px;">{action_html}</div>
            <details style="margin-top:6px;">
              <summary style="cursor:pointer; font-size:.78rem; color:var(--muted);">See the technical details (raw score {score:.0f}/100)</summary>
              <div style="font-size:.78rem; color:var(--muted); margin-top:6px;">
                VWAP {t.get("vwap_score") or 0:+.0f} · Opening range {t.get("or_score") or 0:+.0f} · RSI {t.get("rsi_score") or 0:+.0f} ·
                Rel. volume {t.get("relvol_score") or 0:+.0f} · Gap {t.get("gap_score") or 0:+.0f} · Trend {t.get("trend_score") or 0:+.0f} ·
                Bollinger {t.get("bollinger_score") or 0:+.0f} · Volume surge {t.get("volsurge_score") or 0:+.0f}
              </div>
            </details>
          </div>
        </div>"""

    tod_html = ""
    for label, stats in tod_stats.items():
        wr    = stats["win_rate"] * 100
        color = "#22c55e" if wr >= 54 else "#f59e0b" if wr >= 48 else "#ef4444"
        bar   = int(wr)
        tod_html += f"""
        <div class="tod-row">
          <div class="tod-label">{_tod_plain(label)}</div>
          <div class="tod-bar-wrap">
            <div class="tod-bar" style="width:{bar}%;background:{color}"></div>
          </div>
          <div class="tod-pct" style="color:{color}">{wr:.0f}% win rate ({stats['n']} trades)</div>
        </div>"""
    if not tod_html:
        tod_html = "<p class='muted-note'>No data yet — need at least a few closed trades per time slot.</p>"

    exit_html = ""
    for reason, count in sorted(exit_counts.items(), key=lambda x: -x[1]):
        pct = count / n_closed * 100 if n_closed else 0
        exit_html += f"<div class='exit-item'><span>{_exit_plain(reason)}</span><span class='exit-count'>{count}× ({pct:.0f}%)</span></div>"

    inventory_html = ""
    for row in inventory_rows:
        status = f"OPEN ({row['side'].upper()})" if row["open"] else "flat"
        days   = row["days_since_last_trade"]
        days_str = f"{days}d ago" if days is not None else "never"
        color = "#22c55e" if row["open"] and row["side"] == "long" else "#ef4444" if row["open"] else "#64748b"
        inventory_html += f"""
        <div class="exit-item">
          <span><strong>{row['symbol']}</strong> — {status}</span>
          <span class="exit-count" style="color:{color}">last trade: {days_str}</span>
        </div>"""
    exposure_line = f"{long_count} long · {short_count} short · {n_open} open of {len(inventory_rows)}"

    signal_html = ""
    for s in signal_report.get("by_signal", []):
        wr = s.get("win_rate")
        if wr is None:
            signal_html += f"<div class='exit-item'><span>{s['signal']}</span><span class='exit-count'>{s.get('note', 'insufficient data')}</span></div>"
            continue
        color = "#22c55e" if wr >= 0.54 else "#f59e0b" if wr >= 0.48 else "#ef4444"
        avg_r_s = s.get("avg_r")
        avg_r_str = f"{avg_r_s:+.2f}R" if avg_r_s is not None else "—"
        signal_html += f"""
        <div class="exit-item">
          <span>{s['signal']}</span>
          <span class="exit-count" style="color:{color}">{wr*100:.0f}% WR · {avg_r_str} · n={s['n']}</span>
        </div>"""
    if not signal_html:
        signal_html = "<p class='muted-note'>No per-signal data yet — need 10+ active trades per signal.</p>"

    wr_display  = f"{win_rate*100:.1f}%" if win_rate is not None else "—"
    r_display   = f"{avg_r:.2f}R" if avg_r is not None else "—"
    pnl_display = _pnl_sign(avg_pnl) if avg_pnl is not None else "—"
    adj_display = _pnl_sign(adj_pnl_total) if adj_pnl_total is not None else "—"
    adj_color   = _pnl_color(adj_pnl_total)
    runner_dot  = "#22c55e" if runner_active else "#ef4444"
    runner_lbl  = "Running" if runner_active else "Stopped"
    market_lbl  = "Market open" if market_is_open else "Market closed"
    universe_mode_lbl = (
        f" &nbsp;·&nbsp; <span style=\"color:#a78bfa;\">Low-Price Mode (under ${low_price_ceiling:.0f})</span>"
        if universe_mode == "low_price"
        else " &nbsp;·&nbsp; <span style=\"color:var(--muted);\">Large-Cap Mode</span>"
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Predicta — Trading Monitor</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg: #0b1120; --surface: #131d30; --border: #1e2d47;
    --blue: #38bdf8; --green: #22c55e; --amber: #f59e0b;
    --red: #ef4444; --purple: #a78bfa; --text: #e2e8f0; --muted: #64748b;
  }}
  body {{ background: var(--bg); color: var(--text); font-family: system-ui, -apple-system, sans-serif; min-height: 100vh; padding-bottom: 60px; }}
  header {{ background: var(--surface); border-bottom: 1px solid var(--border); padding: 16px 20px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
  .logo {{ font-size: 1.2rem; font-weight: 700; color: var(--blue); }}
  .header-meta {{ margin-left: auto; font-size: .78rem; color: var(--muted); }}
  .runner-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: {runner_dot}; margin-right: 4px; vertical-align: middle; }}
  main {{ max-width: 680px; margin: 0 auto; padding: 20px 16px; display: flex; flex-direction: column; gap: 16px; }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }}
  .card-title {{ font-size: .7rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); margin-bottom: 14px; }}
  .phase-banner {{ border-radius: 12px; padding: 20px; border: 2px solid {phase_color}; background: color-mix(in srgb, {phase_color} 8%, var(--surface)); }}
  .phase-name {{ font-size: 1.4rem; font-weight: 800; color: {phase_color}; margin-bottom: 4px; }}
  .phase-count {{ font-size: 2.8rem; font-weight: 900; color: {phase_color}; line-height: 1; margin-bottom: 8px; }}
  .phase-sub {{ font-size: .8rem; color: var(--muted); margin-bottom: 14px; }}
  .phase-desc {{ font-size: .9rem; line-height: 1.55; }}
  .progress-wrap {{ background: var(--border); border-radius: 99px; height: 8px; margin-top: 16px; overflow: hidden; }}
  .progress-bar {{ height: 100%; border-radius: 99px; background: {phase_color}; width: {progress_pct}%; transition: width .4s; }}
  .progress-label {{ font-size: .72rem; color: var(--muted); margin-top: 6px; text-align: right; }}
  .stat-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  .stat-box {{ background: var(--bg); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }}
  .stat-value {{ font-size: 2rem; font-weight: 800; line-height: 1; margin-bottom: 4px; }}
  .stat-label {{ font-size: .7rem; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 8px; }}
  .stat-meaning {{ font-size: .78rem; color: var(--text); line-height: 1.4; }}
  .trade-row {{ display: flex; align-items: center; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--border); }}
  .trade-row:last-child {{ border-bottom: none; }}
  .trade-icon {{ font-size: 1.1rem; flex-shrink: 0; width: 22px; text-align: center; }}
  .trade-info {{ flex: 1; min-width: 0; }}
  .trade-sym {{ font-weight: 700; font-size: .95rem; margin-right: 6px; }}
  .trade-detail {{ font-size: .8rem; color: var(--muted); }}
  .trade-why {{ font-size: .88rem; color: var(--text); margin: 4px 0; line-height: 1.5; }}
  .trade-time {{ display: block; font-size: .72rem; color: var(--muted); margin-top: 2px; }}
  .trade-pnl {{ font-weight: 700; font-size: .9rem; flex-shrink: 0; }}
  .trade-btn {{ background: var(--blue); color: #04121f; border: none; border-radius: 8px; padding: 8px 14px; font-size: .82rem; font-weight: 700; cursor: pointer; }}
  .trade-btn:hover {{ filter: brightness(1.1); }}
  .buy-btn {{ background: var(--green); }}
  .sell-btn {{ background: var(--red); color: #fff; }}
  .real-badge {{ display: inline-block; font-size: .7rem; font-weight: 700; color: var(--red); margin-right: 8px; }}
  .tod-row {{ margin-bottom: 12px; }}
  .tod-label {{ font-size: .82rem; color: var(--muted); margin-bottom: 4px; }}
  .tod-bar-wrap {{ background: var(--bg); border-radius: 99px; height: 6px; overflow: hidden; margin-bottom: 4px; }}
  .tod-bar {{ height: 100%; border-radius: 99px; }}
  .tod-pct {{ font-size: .78rem; }}
  .exit-item {{ display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid var(--border); font-size: .83rem; }}
  .exit-item:last-child {{ border-bottom: none; }}
  .exit-count {{ color: var(--muted); }}
  .verdict-card {{ border: 2px solid {verdict_color}; border-radius: 12px; padding: 20px; background: color-mix(in srgb, {verdict_color} 6%, var(--surface)); }}
  .verdict-icon {{ font-size: 2rem; margin-bottom: 8px; }}
  .verdict-title {{ font-size: 1.15rem; font-weight: 700; color: {verdict_color}; margin-bottom: 10px; }}
  .verdict-body {{ font-size: .88rem; line-height: 1.6; }}
  .muted-note {{ font-size: .82rem; color: var(--muted); font-style: italic; }}
  .runner-card {{ display: flex; align-items: center; gap: 14px; }}
  .runner-info {{ flex: 1; }}
  .runner-status {{ font-size: 1rem; font-weight: 600; }}
  .runner-detail {{ font-size: .8rem; color: var(--muted); margin-top: 4px; line-height: 1.5; }}
  footer {{ text-align: center; font-size: .75rem; color: var(--muted); padding: 20px; }}
  .nav-row {{ display: flex; gap: 10px; align-items: center; font-size: .8rem; color: var(--muted); flex-wrap: wrap; }}
  .nav-row a {{ color: var(--muted); text-decoration: none; }}
  .nav-row a:hover {{ color: var(--blue); text-decoration: underline; }}
</style>
</head>
<body>
<header>
  <div class="logo">Predicta · Trading Monitor</div>
  <div class="header-meta">
    <span class="runner-dot"></span>{runner_lbl} &nbsp;·&nbsp; {market_lbl} &nbsp;·&nbsp; {now_str}{universe_mode_lbl}
  </div>
</header>
<main>

  <div class="nav-row">
    <a href="/">← Home</a><span>·</span>
    <a href="/trading">Stock Market</a><span>·</span>
    <a href="/trading/high-value">High Value (query box)</a><span>·</span>
    <a href="/trade/low-value/dashboard">Low Value dashboard</a>
  </div>

  <!-- Phase banner -->
  <div class="phase-banner">
    <div class="card-title">Current Phase</div>
    <div class="phase-name">{phase_label}</div>
    <div class="phase-count">{n_closed} trades</div>
    <div class="phase-sub">closed hypothetical trades collected</div>
    <div class="phase-desc">{phase_desc}</div>
    <div class="progress-wrap"><div class="progress-bar"></div></div>
    <div class="progress-label">{n_closed} / {phase_target} trades · {progress_pct}%</div>
  </div>

  <!-- Accuracy stats -->
  <div class="card">
    <div class="card-title">Accuracy — last {n_closed} closed trades</div>
    <div class="stat-grid">
      <div class="stat-box">
        <div class="stat-label">Win Rate</div>
        <div class="stat-value" style="color:{'#22c55e' if win_rate and win_rate >= 0.54 else '#f59e0b' if win_rate and win_rate >= 0.5 else '#ef4444' if win_rate else '#64748b'}">{wr_display}</div>
        <div class="stat-meaning">{_wr_text(win_rate)}</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Avg R per Trade</div>
        <div class="stat-value" style="color:{'#22c55e' if avg_r and avg_r >= 0.5 else '#f59e0b' if avg_r and avg_r >= 0.25 else '#ef4444' if avg_r else '#64748b'}">{r_display}</div>
        <div class="stat-meaning">{_r_text(avg_r)}</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Avg P&amp;L / Trade</div>
        <div class="stat-value" style="color:{_pnl_color(avg_pnl)}">{pnl_display}</div>
        <div class="stat-meaning">Raw paper fill (Alpaca price, before spread cost).</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Total Adj P&amp;L</div>
        <div class="stat-value" style="color:{adj_color}">{adj_display}</div>
        <div class="stat-meaning">After spread cost deducted — closer to what you'd actually make.</div>
      </div>
    </div>
  </div>

  <!-- Best time of day -->
  <div class="card">
    <div class="card-title">Best Time of Day</div>
    {tod_html}
  </div>

  <!-- Exit breakdown -->
  <div class="card">
    <div class="card-title">How Trades Are Closing</div>
    {exit_html if exit_html else "<p class='muted-note'>No closed trades yet.</p>"}
  </div>

  <!-- Open positions -->
  <div class="card">
    <div class="card-title">Open Right Now ({n_open} positions)</div>
    {open_html if open_html else "<p class='muted-note'>No open positions. The next scan runs at 9:35 AM ET on weekdays.</p>"}
  </div>

  <!-- Inventory / exposure snapshot -->
  <div class="card">
    <div class="card-title">Inventory — Exposure by Ticker ({exposure_line})</div>
    {inventory_html if inventory_html else "<p class='muted-note'>No data yet.</p>"}
  </div>

  <!-- Cross-engine exposure (Tier 2, 2026-07-16/17 trading-model audit) -->
  <div class="card">
    <div class="card-title">Cross-Engine Exposure</div>
    {f'''<div class="exit-item"><span>High Value</span><span class="exit-count">{cross_exposure["high_value_open"]} open / cap {cross_exposure["high_value_cap"]}</span></div>
    <div class="exit-item"><span>Low Value</span><span class="exit-count">{cross_exposure["low_value_open"]} open / cap {cross_exposure["low_value_cap"]}</span></div>
    <div class="exit-item"><span>Pairs</span><span class="exit-count">{cross_exposure["pairs_open"]} open / no cap set</span></div>
    <div style="font-size:.78rem; color:var(--muted); margin-top:10px;">{cross_exposure["total_open"]} total open positions across all engines. {cross_exposure["note"]}</div>''' if cross_exposure else "<p class='muted-note'>Unavailable.</p>"}
  </div>

  <!-- Per-signal P&L attribution -->
  <div class="card">
    <div class="card-title">Which Signals Are Actually Working</div>
    {signal_html}
  </div>

  <!-- Recent trades -->
  <div class="card">
    <div class="card-title">Recent Closed Trades</div>
    {recent_html if recent_html else "<p class='muted-note'>No closed trades yet. Come back after a few market sessions.</p>"}
  </div>

  <!-- Runner status -->
  <div class="card">
    <div class="card-title">Automatic Scanner</div>
    <div class="runner-card">
      <span style="font-size:2rem">{'🟢' if runner_active else '🔴'}</span>
      <div class="runner-info">
        <div class="runner-status">{runner_lbl}</div>
        <div class="runner-detail">
          Scans AAPL, MSFT, NVDA, AMD, AMZN, META, GOOGL, TSLA every morning at 9:35 AM ET, and suggests candidates below.<br>
          It only ever analyzes and suggests — it never buys or sells anything on its own. You decide, using the buttons above.<br>
          Once you place a real order, it checks that position every 30 minutes and force-closes it by 3:50 PM ET, same as the day-trading plan you approved when you clicked Buy.
        </div>
        <button class="trade-btn" style="margin-top:12px;" onclick="predictaScanNow()">Scan Now</button>
      </div>
    </div>
  </div>

  <!-- Readiness verdict -->
  <div class="verdict-card">
    <div class="verdict-icon">{verdict_icon}</div>
    <div class="verdict-title">{verdict_title}</div>
    <div class="verdict-body">{verdict_msg}</div>
  </div>

</main>
<footer>Auto-refreshes every 5 minutes &nbsp;·&nbsp; <a href="/trade/paper-data" style="color:var(--muted)">Raw data</a> &nbsp;·&nbsp; <a href="/trade/calibration" style="color:var(--muted)">Calibration</a></footer>
<script>
function predictaErrorText(data) {{
  // FastAPI's own validation errors put a LIST of objects in data.detail
  // (not a string), and a raw Alpaca error dict could too before it's been
  // through friendly_order_error() server-side — either shape used to
  // render as a literal "[object Object]" once handed to string
  // concatenation. Always produce real, readable text instead.
  if (typeof data.detail === 'string') return data.detail;
  if (data.detail) return JSON.stringify(data.detail);
  return JSON.stringify(data);
}}
async function predictaAction(url, label) {{
  if (!confirm('Confirm: ' + label + '?\\n\\nThis places (or closes) a real Alpaca paper order.')) return;
  const passcode = prompt('Trade passcode (leave blank if none is set):') || '';
  try {{
    const res = await fetch(url, {{ method: 'POST', headers: {{ 'X-Trade-Passcode': passcode }} }});
    const data = await res.json();
    if (!res.ok) {{ alert('Failed: ' + predictaErrorText(data)); return; }}
    alert('Done.\\n' + JSON.stringify(data, null, 2));
    location.reload();
  }} catch (e) {{ alert('Request failed: ' + e); }}
}}
async function predictaScanNow() {{
  try {{
    const res = await fetch('/trade/paper-runner/scan-now', {{ method: 'POST' }});
    const data = await res.json();
    alert('Scan complete — ' + data.logged + ' candidate(s) found. Reloading.');
    location.reload();
  }} catch (e) {{ alert('Scan failed: ' + e); }}
}}
</script>
</body>
</html>"""
    return html


# ── Low Value engine endpoints (Kimi review, round 6 follow-up) ──────────────
# Independent from the High Value paper-runner endpoints above — separate
# engine, separate runner, separate dashboard. "Two tabs, two engines."

@app.get("/trade/low-value/universe")
def low_value_universe(force_refresh: bool = False):
    """
    Today's Low Value scanner universe (cached in-memory for the day unless
    force_refresh). A cache hit returns immediately. On a miss/force_refresh,
    this NEVER blocks the request — building the universe is a multi-minute,
    hundreds-of-API-calls operation (fixed 2026-07-07: it used to block here
    directly, and a long enough wait would get silently killed by Railway's
    proxy timeout with a blank error). Instead it kicks off
    trigger_universe_refresh_async() and returns a "started" status; poll
    this same endpoint again (without force_refresh) once it's done.
    """
    from fetchers.low_value_runner import get_daily_universe, trigger_universe_refresh_async
    from fetchers.low_value_runner import _universe_cache
    from fetchers.high_value_runner import _et_now
    # Must match the ET-based date key get_daily_universe() actually caches
    # under (fetchers/low_value_runner.py: _et_now().strftime(...)) — this
    # used to compute today_str from UTC instead. From 8:00 PM ET to
    # midnight ET, the UTC calendar date is already the next day, so this
    # endpoint's cache lookup silently missed an already-completed universe
    # every single poll and re-triggered a brand new build each time,
    # forever — indistinguishable from a hung build from the outside (a
    # live 2026-07-11 report: triggered ~7:55 PM ET, polled repeatedly past
    # 8:00 PM ET, dashboard stayed empty with no error the whole time).
    today_str = _et_now().strftime("%Y-%m-%d")
    if not force_refresh and today_str in _universe_cache:
        universe = get_daily_universe(force_refresh=False)
        return {"date": today_str, "count": len(universe), "symbols": universe, "status": "cached"}
    result = trigger_universe_refresh_async()
    result["date"] = today_str
    result["note"] = "Universe build running in the background — re-request this endpoint without force_refresh in a few minutes for the result."
    return result


@app.post("/trade/low-value/scan-now")
def low_value_scan_now():
    """
    Manually trigger a Low Value universe + signal scan outside the 8 AM ET
    window. Fire-and-forget (fixed 2026-07-07 — a scan can take several
    minutes and must never block the HTTP request; see
    fetchers.low_value_runner.trigger_scan_async). Poll
    GET /trade/low-value/runner/status for scan_in_progress /
    last_scan_completed_at / last_scan_trade_ids.
    """
    from fetchers.low_value_runner import trigger_scan_async
    return trigger_scan_async()


@app.get("/trade/low-value/runner/status")
def low_value_runner_status():
    """Status of the Low Value background runner."""
    from fetchers.low_value_runner import get_runner_status
    return get_runner_status()


@app.post("/trade/low-value/execute/{trade_id}", dependencies=[Depends(require_trade_passcode)])
def execute_low_value(trade_id: int):
    """
    Human-triggered only. Places a real Alpaca paper order for a Low Value
    candidate the automated scan already suggested — the scan itself never
    places an order on its own. See fetchers.low_value_runner.execute_low_value_trade.
    """
    from fetchers.low_value_runner import execute_low_value_trade
    result = execute_low_value_trade(trade_id)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.post("/trade/low-value/close/{trade_id}", dependencies=[Depends(require_trade_passcode)])
def close_low_value(trade_id: int):
    """Human-triggered only. Closes a real open Low Value position early, before its target/stop/hold-window exit fires."""
    from fetchers.low_value_runner import close_low_value_trade
    result = close_low_value_trade(trade_id)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.post("/trade/low-value/runner/start")
def low_value_runner_start():
    """Start the Low Value background runner if not already running."""
    from fetchers.low_value_runner import start_runner
    return {"started": start_runner()}


@app.post("/trade/low-value/runner/stop")
def low_value_runner_stop():
    """Signal the Low Value background runner to stop on its next tick."""
    from fetchers.low_value_runner import stop_runner
    stop_runner()
    return {"ok": True}


@app.get("/trade/low-value/calibration")
def low_value_calibration():
    """
    Per-thesis-type win rate, overall Low Value calibration readiness
    (20/50/100 trade tiers), and per-signal missing-vs-present win-rate
    comparison (Kimi review, round-2 follow-up — answers whether the
    missing-signal reweighting is hiding a real risk).
    """
    from models.trading.shared.signal_calibration import (
        thesis_type_calibration_report, low_value_calibration_readiness, missing_signal_impact_report,
    )
    return {
        "readiness": low_value_calibration_readiness(),
        "by_thesis_type": thesis_type_calibration_report(),
        "missing_signal_impact": missing_signal_impact_report(),
    }


@app.get("/trade/low-value/dashboard", response_class=HTMLResponse)
def low_value_dashboard():
    """Plain-English Low Value engine monitor — separate page from the High Value /trade/dashboard."""
    from fetchers.low_value_dashboard import render_low_value_dashboard
    return HTMLResponse(content=render_low_value_dashboard())


@app.post("/trade/low-value/analyze-image")
async def analyze_low_value_image(file: UploadFile = File(...)):
    """
    Upload a photo (e.g. a screenshot of a brokerage watchlist) — extracts
    ticker symbols via Claude vision (fetchers.ticker_vision, fast, one AI
    call, done synchronously here), then starts the real per-ticker Low
    Value analysis (fetchers.low_value_runner.analyze_low_value_tickers)
    in the BACKGROUND and returns immediately (fixed 2026-07-27 — a
    20-50-ticker photo means 40-100+ sequential Alpaca/Finnhub calls, which
    can genuinely take minutes; blocking the HTTP request on that is
    exactly the bug trigger_scan_async was already fixed for on
    2026-07-07, reintroduced fresh here and now fixed the same way — a
    user reported the Analyze button's loading message never resolving).
    Poll GET /trade/low-value/analyze-status for progress/results. Never
    logs a hypothetical trade — this is read-only analysis.
    """
    image_bytes = await file.read()
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 10MB)")
    if not image_bytes:
        raise HTTPException(400, "Empty file")
    media_type = file.content_type or "image/png"

    from fetchers.ticker_vision import extract_tickers_from_image
    tickers = extract_tickers_from_image(image_bytes, media_type)
    if not tickers:
        return {"status": "no_tickers", "tickers_found": [], "note": "No ticker symbols recognized in this image."}

    from fetchers.low_value_runner import trigger_ticker_analysis_async
    result = trigger_ticker_analysis_async(tickers)
    return {**result, "tickers_found": tickers}


class AnalyzeTickersRequest(BaseModel):
    symbols: list[str]


@app.post("/trade/low-value/analyze-tickers")
def analyze_low_value_tickers_endpoint(body: AnalyzeTickersRequest):
    """Same real Low Value analysis as analyze-image, for a plain typed/pasted ticker list — no photo required. Also fire-and-forget; poll GET /trade/low-value/analyze-status."""
    if not body.symbols:
        raise HTTPException(400, "Provide a non-empty 'symbols' list")
    from fetchers.low_value_runner import trigger_ticker_analysis_async
    result = trigger_ticker_analysis_async(body.symbols)
    return {**result, "tickers_found": body.symbols}


@app.get("/trade/low-value/analyze-status")
def low_value_analyze_status():
    """Poll this after analyze-image / analyze-tickers returns {"status": "started"} — in_progress flips false once results (or an error) are ready."""
    from fetchers.low_value_runner import get_analysis_status
    return get_analysis_status()


class LowValueQueryRequest(BaseModel):
    query: str


# Multi-word phrases that mean "run a live scan now" — checked before the
# brief triggers so "any signals" doesn't get swallowed by a broader match.
_LV_SCAN_TRIGGERS = (
    "scan the market", "scan market", "market scan", "any signals",
    "today's picks", "todays picks", "check the market", "scan today",
    "scan now", "run the scanner", "run a scan",
)


@app.post("/trade/low-value/query")
def low_value_query(body: LowValueQueryRequest):
    """
    Natural-language query box for the Low Value page. "Scan the market" /
    "any signals" starts a live scan in the BACKGROUND (fixed 2026-07-07 —
    this used to block the request on run_low_value_scan() directly, which
    can genuinely take many minutes against the real universe size and was
    getting silently killed by Railway's proxy timeout, returning a blank
    error after a long wait with no way to tell if anything happened).
    Returns immediately with a "started" status; poll
    GET /trade/low-value/runner/status for scan_in_progress / results.
    Everything else (brief / this week / yesterday / anything unrecognized)
    returns a read-only recap — never triggers a scan, always fast.
    """
    q = body.query.strip().lower()
    if not q:
        raise HTTPException(400, "Query cannot be empty")

    if any(trig in q for trig in _LV_SCAN_TRIGGERS):
        from fetchers.low_value_runner import trigger_scan_async
        result = trigger_scan_async()
        return {"mode": "scan", **result}

    days = 1 if "yesterday" in q else 7
    from fetchers.low_value_dashboard import get_low_value_brief
    return get_low_value_brief(days=days)


class PortfolioWatchRequest(BaseModel):
    symbols: list[str]


@app.post("/trade/portfolio-watch/analyze")
def portfolio_watch_analyze(body: PortfolioWatchRequest):
    """
    Portfolio Watch (added 2026-07-19) — up to 10 symbols, real 7-day news per
    symbol (Finnhub) synthesized into a qualitative HOLD/WATCH_CLOSELY/
    TRIM_CANDIDATE read with plain-English reasoning. Never returns a numeric
    sell percentage — see models/trading/portfolio_watch.py's module docstring.
    """
    if not body.symbols:
        raise HTTPException(400, "Provide at least one symbol")
    from models.trading.portfolio_watch import review_watchlist
    return review_watchlist(body.symbols)


@app.get("/trade/paper-data")
def paper_data_export(days: int = 60, include_open: bool = False):
    """
    Export all hypothetical paper trade records for analysis.
    Returns closed trades by default; include_open=true adds open positions.
    Schema includes all v4 signal scores for per-signal calibration analysis.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM intraday_trades
            WHERE is_hypothetical = 1
              AND entry_time >= datetime('now', ? || ' days')
              AND (? OR exit_time IS NOT NULL)
            ORDER BY entry_time DESC
            """,
            (f"-{days}", 1 if include_open else 0),
        ).fetchall()

    trades = [dict(r) for r in rows]
    closed = [t for t in trades if t.get("exit_time")]
    open_t = [t for t in trades if not t.get("exit_time")]

    win_rate = None
    avg_pnl  = None
    avg_r    = None
    if closed:
        wins     = sum(1 for t in closed if (t.get("pnl_dollars") or 0) > 0)
        win_rate = round(wins / len(closed), 3)
        avg_pnl  = round(sum(t.get("pnl_dollars") or 0 for t in closed) / len(closed), 2)
        r_vals   = [t["pnl_r"] for t in closed if t.get("pnl_r") is not None]
        if r_vals:
            avg_r = round(sum(r_vals) / len(r_vals), 3)

    return {
        "period_days":  days,
        "total":        len(trades),
        "closed":       len(closed),
        "open":         len(open_t),
        "win_rate":     win_rate,
        "avg_pnl":      avg_pnl,
        "avg_r":        avg_r,
        "trades":       trades,
    }
