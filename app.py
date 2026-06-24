"""
FastAPI local API layer for Predicta.
Run with: uvicorn app:app --reload
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import env_loader  # noqa: F401 — loads .env on import, handles CRLF/BOM/quotes

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
    if body.sport not in ("soccer", "table_tennis", "tennis", "baseball"):
        raise HTTPException(400, "sport must be soccer, table_tennis, tennis, or baseball")
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


@app.get("/trading", response_class=HTMLResponse)
def trading():
    return HTMLResponse(content=(TEMPLATES_DIR / "trading.html").read_text(encoding="utf-8"))


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


class BaseballRequest(BaseModel):
    query: str
    bankroll: float = 1000.0


@app.post("/analyze-baseball")
def analyze_baseball(body: BaseballRequest):
    if not body.query.strip():
        raise HTTPException(400, "Query cannot be empty")
    from analyze_baseball import run_baseball_analysis
    result = run_baseball_analysis(body.query, body.bankroll)
    if "error" in result and not result.get("team_a"):
        raise HTTPException(500, detail=result["error"])
    return result


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


@app.post("/analyze-table-tennis")
def analyze_table_tennis(body: TableTennisRequest):
    if not body.query.strip():
        raise HTTPException(400, "Query cannot be empty")
    from analyze_table_tennis import run_table_tennis_analysis
    result = run_table_tennis_analysis(body.query, body.bankroll)
    if "error" in result and not result.get("player_a"):
        raise HTTPException(500, detail=result["error"])
    return result


class TradeRequest(BaseModel):
    query: str
    bankroll: float = 10000.0


@app.post("/analyze-trade")
def analyze_trade(body: TradeRequest):
    if not body.query.strip():
        raise HTTPException(400, "Query cannot be empty")
    from analyze_trading import run_trade_analysis
    result = run_trade_analysis(body.query, body.bankroll)
    if "error" in result:
        raise HTTPException(500, detail=result["error"])
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
    from models.trading.intraday import compute_intraday_signals, _avg_daily_volume

    snap = get_snapshot(body.symbol)
    if "error" in snap:
        raise HTTPException(400, snap["error"])
    intraday = get_bars(body.symbol, "5Min", 78)
    daily = get_daily_bars(body.symbol, 60)
    avg_vol = sum(b.get("v", 0) for b in daily[-20:]) / 20 if daily else 1_000_000
    result = compute_intraday_signals(intraday, daily, snap, avg_vol)
    if "error" in result:
        raise HTTPException(400, result["error"])

    # Kelly position sizing
    from models.trading.kelly import kelly_from_signals
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
    if "error" in result:
        raise HTTPException(400, result.get("detail") or result["error"])
    return result


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
