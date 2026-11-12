"""
Automatic scorecard for the volatility forecast (no manual checking).

Every day: forecast the swing for every tracked stock (one model fit for all),
log it; once the days have passed, compare with what really happened, and
compare against the dumb guess "same as the last 20 days". Her own manual
checks on the How Much To Buy page are logged too and emailed when they resolve.
Read-only: changes nothing in any trading engine.
"""
import logging
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from db.database import get_db
from fetchers import notify
from models.trading.horizon.features import apply_clip, build_panel, feature_cols, fit_clip, load_bars

log = logging.getLogger("vol_tracking")
HORIZON = 5
MIN_TRAIN_ROWS = 3000
WEEKLY_DAYS = 7

_DDL = """CREATE TABLE IF NOT EXISTS vol_forecast_log (
    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, as_of TEXT NOT NULL, horizon INTEGER NOT NULL,
    forecast_vol REAL NOT NULL, trailing_vol REAL, price REAL, source TEXT NOT NULL DEFAULT 'auto',
    made_at TEXT NOT NULL, resolved_at TEXT, actual_vol REAL, model_err REAL, base_err REAL,
    notified INTEGER NOT NULL DEFAULT 0, UNIQUE(symbol, as_of, horizon));
CREATE TABLE IF NOT EXISTS vol_email_log (id INTEGER PRIMARY KEY, sent_at TEXT NOT NULL, kind TEXT NOT NULL)"""


def _ensure(conn):
    for stmt in _DDL.split(";"):
        conn.execute(stmt)


def log_forecast(symbol, as_of, horizon, forecast_vol, trailing_vol, price, source="auto"):
    with get_db() as conn:
        _ensure(conn)
        conn.execute("INSERT OR IGNORE INTO vol_forecast_log (symbol,as_of,horizon,forecast_vol,trailing_vol,price,source,made_at) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (symbol, as_of, horizon, forecast_vol, trailing_vol, price, source, datetime.now(timezone.utc).isoformat()))
        if source == "manual":   # a manual check on an existing auto row marks it as hers, so she gets the email
            conn.execute("UPDATE vol_forecast_log SET source='manual' WHERE symbol=? AND as_of=? AND horizon=?", (symbol, as_of, horizon))


def forecast_universe(horizon: int = HORIZON, bars=None) -> dict:
    """One model fit, one forecast per tracked stock, all logged. Safe to run repeatedly (same as_of = no duplicates)."""
    bars = load_bars() if bars is None else bars
    panel = build_panel(bars, horizon)
    if panel.empty:
        return {"status": "insufficient_data"}
    cols = feature_cols(panel)
    train = panel.dropna(subset=["fwd_vol"])
    train = train[train["fwd_vol"] > 0]
    if len(train) < MIN_TRAIN_ROWS:
        return {"status": "insufficient_data", "rows": len(train)}
    bounds = fit_clip(train, cols)
    mdl = make_pipeline(StandardScaler(), Ridge(alpha=10.0)).fit(apply_clip(train, cols, bounds)[cols].values, np.log(train["fwd_vol"].values))
    last = panel.reset_index().sort_values(panel.index.names[0]).groupby(panel.index.names[1]).tail(1)
    last = last.set_index(panel.index.names)
    clipped = apply_clip(last, cols, bounds)
    preds = np.exp(mdl.predict(clipped[cols].values))
    px = bars.sort_values("bar_date").groupby("symbol")["close"].last()
    n = 0
    for (d, sym), p, tv in zip(last.index, preds, last["vol_20"].values):
        if np.isfinite(p) and np.isfinite(tv) and tv > 0 and sym in px.index:
            log_forecast(sym, str(d), horizon, float(p), float(tv), float(px[sym]), "auto")
            n += 1
    return {"status": "ok", "logged": n, "as_of": str(max(i[0] for i in last.index))}


def resolve_forecasts() -> int:
    """Score every forecast whose horizon has fully passed, using recorded daily bars."""
    with get_db() as conn:
        _ensure(conn)
        pend = conn.execute("SELECT id, symbol, as_of, horizon, forecast_vol, trailing_vol FROM vol_forecast_log WHERE resolved_at IS NULL").fetchall()
    if not pend:
        return 0
    syms = sorted({r["symbol"] for r in pend})
    with get_db() as conn:
        q = ",".join("?" * len(syms))
        rows = conn.execute(f"SELECT symbol, bar_date, close FROM market_daily_bars WHERE symbol IN ({q}) AND close IS NOT NULL ORDER BY bar_date", syms).fetchall()
    closes = {}
    for r in rows:
        closes.setdefault(r["symbol"], []).append((r["bar_date"], r["close"]))
    done = 0
    for r in pend:
        series = closes.get(r["symbol"], [])
        idx = next((i for i, (d, _) in enumerate(series) if d == r["as_of"]), None)
        if idx is None or idx + r["horizon"] > len(series) - 1:
            continue
        window = [c for _, c in series[idx: idx + r["horizon"] + 1]]
        if len(window) < r["horizon"] + 1:
            continue
        rets = np.diff(window) / np.array(window[:-1])
        actual = float(np.std(rets, ddof=1))
        with get_db() as conn:
            conn.execute("UPDATE vol_forecast_log SET resolved_at=?, actual_vol=?, model_err=?, base_err=? WHERE id=?",
                         (datetime.now(timezone.utc).isoformat(), actual, abs(r["forecast_vol"] - actual),
                          abs((r["trailing_vol"] or 0) - actual), r["id"]))
        done += 1
    return done


def _boot(xs, n=2000, seed=1):
    rnd = np.random.default_rng(seed)
    xs = np.asarray(xs)
    m = np.sort([rnd.choice(xs, len(xs)).mean() for _ in range(n)])
    return float(m[int(0.025 * n)]), float(m[int(0.975 * n)])


def scorecard() -> dict:
    with get_db() as conn:
        _ensure(conn)
        rows = conn.execute("SELECT as_of, forecast_vol, trailing_vol, actual_vol, model_err, base_err FROM vol_forecast_log WHERE resolved_at IS NOT NULL").fetchall()
        pending = conn.execute("SELECT COUNT(*) c FROM vol_forecast_log WHERE resolved_at IS NULL").fetchone()["c"]
    out = {"scored": len(rows), "waiting_for_days_to_pass": pending}
    if not rows:
        out["verdict"] = "TOO EARLY: nothing has finished yet. A 5-day forecast needs 5 trading days."
        return out
    df = pd.DataFrame([dict(r) for r in rows])
    out["model_avg_error"] = round(float(df["model_err"].mean()) * 100, 4)
    out["last_20_days_guess_avg_error"] = round(float(df["base_err"].mean()) * 100, 4)
    out["model_closer_share"] = round(float((df["model_err"] < df["base_err"]).mean()), 3)
    dates = sorted(df["as_of"].unique())
    if len(dates) < 6:
        out["verdict"] = f"TOO EARLY: {len(dates)} forecast days scored, need 6+ for a fair read."
        return out
    # one point per forecast date (all stocks that day averaged), bootstrap over dates since stocks move together
    per_day = (df.assign(diff=df["base_err"] - df["model_err"]).groupby("as_of")["diff"].mean().values)
    lo, hi = _boot(per_day)
    out["improvement_pct"] = round(float((1 - df["model_err"].mean() / df["base_err"].mean()) * 100), 2)
    out["improvement_95ci_error_pts"] = [round(lo * 100, 4), round(hi * 100, 4)]
    out["verdict"] = ("MODEL BEATS the last-20-days guess" if lo > 0 else
                      "MODEL IS WORSE than the last-20-days guess" if hi < 0 else
                      "UNPROVEN: too close to call so far")
    out["note"] = "Daily forecasts overlap and stocks move together, so the range uses one point per forecast day."
    return out


def _manual_resolved_unsent():
    with get_db() as conn:
        _ensure(conn)
        return [dict(r) for r in conn.execute("SELECT id, symbol, as_of, horizon, forecast_vol, trailing_vol, actual_vol FROM vol_forecast_log "
                                              "WHERE source='manual' AND resolved_at IS NOT NULL AND notified=0")]


def _last_email(kind):
    with get_db() as conn:
        _ensure(conn)
        r = conn.execute("SELECT sent_at FROM vol_email_log WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
    return datetime.fromisoformat(r["sent_at"]) if r else None


def _mark_email(kind):
    with get_db() as conn:
        _ensure(conn)
        conn.execute("INSERT INTO vol_email_log (sent_at, kind) VALUES (?,?)", (datetime.now(timezone.utc).isoformat(), kind))


def send_alerts(sender=notify.send_email) -> dict:
    """Email her (1) her own manual checks once they have finished, (2) a weekly overall scorecard."""
    sent = []
    manual = _manual_resolved_unsent()
    if manual:
        lines = []
        for m in manual:
            a = m["actual_vol"] * 100
            f = m["forecast_vol"] * 100
            g = (m["trailing_vol"] or 0) * 100
            closer = "Predicta's forecast" if abs(f - a) < abs(g - a) else "the simple guess (same as the last 20 days)"
            lines.append(f"- {m['symbol']} (forecast made on {m['as_of']}, {m['horizon']} days): forecast daily swing {f:.2f}%, "
                         f"actually swung {a:.2f}%, simple guess was {g:.2f}%. Closer: {closer}.")
        body = ("Here is how your volatility forecasts turned out:\n\n" + "\n".join(lines) +
                "\n\nThis only measures how much the stock swung, not which way it went.\nOpen http://127.0.0.1:8000/trade/volatility to see the overall scorecard.")
        r = sender("Predicta: your volatility forecasts finished", body)
        if r.get("sent"):
            with get_db() as conn:
                conn.execute(f"UPDATE vol_forecast_log SET notified=1 WHERE id IN ({','.join(str(m['id']) for m in manual)})")
            sent.append("manual")
        else:
            return {"sent": sent, "problem": r.get("reason")}
    sc = scorecard()
    last = _last_email("weekly")
    if sc["scored"] > 0 and (last is None or (datetime.now(timezone.utc) - last).days >= WEEKLY_DAYS):
        body = (f"Weekly volatility forecast scorecard\n\nForecasts scored: {sc['scored']} (still waiting: {sc['waiting_for_days_to_pass']})\n"
                f"Verdict: {sc['verdict']}\n")
        if "model_avg_error" in sc:
            body += (f"Predicta's average miss: {sc['model_avg_error']}% per day\nSimple guess's average miss: {sc['last_20_days_guess_avg_error']}% per day\n"
                     f"Share of forecasts where Predicta was closer: {sc['model_closer_share']*100:.0f}%\n")
        body += "\nIt measures size of price swings only, not direction. Details: http://127.0.0.1:8000/trade/volatility"
        r = sender("Predicta: weekly volatility scorecard", body)
        if r.get("sent"):
            _mark_email("weekly"); sent.append("weekly")
        else:
            return {"sent": sent, "problem": r.get("reason")}
    return {"sent": sent}


_last_run = [0.0]


def daily_job(force: bool = False) -> dict:
    """Called from the background loop (throttled to once an hour) and by POST /trade/volatility/run-now."""
    import time
    if not force and time.time() - _last_run[0] < 3600:
        return {"status": "throttled"}
    _last_run[0] = time.time()
    res = {}
    bars = load_bars()
    if not bars.empty:
        latest = bars["bar_date"].max()
        with get_db() as conn:
            _ensure(conn)
            have = conn.execute("SELECT COUNT(*) c FROM vol_forecast_log WHERE source IN ('auto','manual') AND as_of=? AND horizon=?", (latest, HORIZON)).fetchone()["c"]
        res["forecast"] = {"status": "already_done_for_" + latest} if have > 5 else forecast_universe(HORIZON, bars)
    res["resolved"] = resolve_forecasts()
    res["alerts"] = send_alerts() if notify.email_configured() else {"sent": [], "note": "email not set up"}
    return res
