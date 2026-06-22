"""
FastAPI local API layer for Predicta.
Run with: uvicorn app:app --reload
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Load .env file automatically if present (pip install python-dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
    if body.sport not in ("soccer", "table_tennis", "tennis"):
        raise HTTPException(400, "sport must be soccer, table_tennis, or tennis")
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


@app.post("/matches/{match_id}/outcome", status_code=201)
def add_outcome(match_id: int, body: OutcomeCreate):
    result = record_outcome(
        match_id, body.result, body.score_a, body.score_b,
        body.update_ratings, body.importance, body.surface
    )
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


# ── Calibration ───────────────────────────────────────────────────────────────

@app.get("/calibration")
def calibration(method: Optional[str] = None):
    return compute_metrics_from_db(method)


# ── Report ────────────────────────────────────────────────────────────────────

@app.get("/report")
def html_report():
    from report import generate_html_report
    return HTMLResponse(content=generate_html_report())


# ── Main UI ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(content=(TEMPLATES_DIR / "index.html").read_text(encoding="utf-8"))


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
