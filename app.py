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
    # Kick off a background auto-resolve pass on startup so any results
    # that came in while the server was down get picked up immediately.
    import threading
    def _bg_resolve():
        try:
            from tasks.auto_resolve import run_auto_resolve
            run_auto_resolve()
        except Exception:
            pass
    threading.Thread(target=_bg_resolve, daemon=True).start()


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
    result = compute_intraday_signals(intraday, daily, snap, avg_vol, symbol=body.symbol)
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
    from fetchers.alpaca import get_snapshot, get_bars, get_daily_bars, place_bracket_order
    from models.trading.intraday import compute_intraday_signals
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
    if tod_label == "LUNCH_CHOP" and abs(score_val) < 60:
        return {
            "status":    "REJECTED",
            "reason":    "Lunch chop — score suppressed below 60 conviction threshold",
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
        raise HTTPException(400, order_result.get("detail") or order_result["error"])

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
    from models.trading.intraday import compute_intraday_signals
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
    elif tod_label == "LUNCH_CHOP" and abs(score_val) < 60:
        would_reject = "Lunch chop — score below 60 conviction threshold"
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
    from models.trading.ngram import build_ngram_from_alpaca

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
