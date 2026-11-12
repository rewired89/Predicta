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


def forecast_fx(horizon: int = HORIZON) -> dict:
    """Same daily forecast for the three forex pairs (needs the OANDA price download)."""
    from fetchers.oanda import load_bars as fx_bars
    from models.trading.fx.forecast import predict_vol_now
    from models.trading.shared.plain import PAIRS
    n = 0
    for pair in PAIRS:
        b = fx_bars(pair)
        if b.empty:
            continue
        f = predict_vol_now(b, horizon, pair)
        if f.get("status") == "ok":
            log_forecast(pair, f["as_of"], horizon, f["forecast_daily_vol"], f["trailing_daily_vol"], f["last_price"], "auto")
            n += 1
    return {"logged": n}


def _fx_refresh_if_due() -> None:
    from fetchers import oanda
    if not oanda.has_key():
        return
    last = _last_email("fx_refresh")
    if last is None or last.date() < datetime.now(timezone.utc).date():
        try:
            oanda.backfill()
            _mark_email("fx_refresh")
        except Exception as e:
            log.error(f"[VOL] fx refresh failed: {type(e).__name__}")


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
    from models.trading.shared.plain import PAIRS
    fx_syms = [x for x in syms if x in PAIRS]
    if fx_syms:
        with get_db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS fx_daily_bars (pair TEXT NOT NULL, date TEXT NOT NULL, o REAL, h REAL, l REAL, c REAL, spread REAL, PRIMARY KEY (pair, date))")
            for pr in fx_syms:
                closes[pr] = [(r["date"], r["c"]) for r in conn.execute("SELECT date, c FROM fx_daily_bars WHERE pair=? ORDER BY date", (pr,))]
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


def _latest_forecast(symbol: str):
    with get_db() as conn:
        _ensure(conn)
        r = conn.execute("SELECT forecast_vol FROM vol_forecast_log WHERE symbol=? ORDER BY as_of DESC LIMIT 1", (symbol,)).fetchone()
    return float(r["forecast_vol"]) if r else None


_hist_lock = [0.0]


def missing_history_symbols() -> list:
    """Symbols the engines hold or suggest that have no (or too little) price history loaded."""
    with get_db() as conn:
        open_syms = [r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM intraday_trades WHERE exit_time IS NULL AND engine IN ('low_value','automaton','high_value')")]
        if not open_syms:
            return []
        q = ",".join("?" * len(open_syms))
        have = {r["symbol"] for r in conn.execute(
            f"SELECT symbol FROM market_daily_bars WHERE symbol IN ({q}) GROUP BY symbol HAVING COUNT(*) >= 70", open_syms)}
    return sorted(set(open_syms) - have)


def ensure_history(force: bool = False) -> dict:
    """Download 3 years of daily prices for every stock an engine holds or suggests (once per 10 minutes at most)."""
    import time
    if not force and time.time() - _hist_lock[0] < 600:
        return {"status": "throttled"}
    miss = missing_history_symbols()
    if not miss:
        return {"status": "nothing_missing"}
    _hist_lock[0] = time.time()
    from fetchers.horizon_backfill import backfill
    return {"status": "ok", "symbols": miss, **backfill(years=3, symbols=miss + ["SPY"])}


def _idea_lines(t: dict) -> str:
    """One trade idea in plain words: what to do, where the stop and the sell price are, how risky, and what could happen."""
    from models.trading.shared.plain import risk_label, days_word
    from models.trading.shared.scenario import simulate, plain as sim_plain
    sym, side, px = t["symbol"], t["side"], t["entry_price"]
    engine = {"low_value": "Low Value", "automaton": "Automaton", "high_value": "High Value"}.get(t["engine"], t["engine"])
    hold = {"low_value": 5, "automaton": 126, "high_value": 1}.get(t["engine"], 5)
    action = f"BUY {sym} around ${px:,.2f}" if side == "long" else f"SELL SHORT {sym} around ${px:,.2f} (you profit if it falls)"
    lines = [f"{action}   [{engine} idea]"]
    if px and px < 1.0:
        lines.append("  - WARNING: this costs under $1 a share. Very cheap stocks (and warrants, whose tickers often end in W) can jump around a lot and be hard to sell.")
    stop, tgt = t.get("stop_price"), t.get("target1_price")
    if stop and tgt:
        if side == "long":
            lines.append(f"  - Stop: if it falls to ${stop:,.2f}, sell to limit the loss.")
            lines.append(f"  - Sell for profit: when it reaches ${tgt:,.2f}.")
        else:
            lines.append(f"  - Stop: if it rises to ${stop:,.2f}, buy it back to limit the loss.")
            lines.append(f"  - Take profit: when it falls to ${tgt:,.2f}.")
    lines.append(f"  - Time limit: close it within about {days_word(hold)}" + (" (about 6 months)" if hold > 60 else "") + " if neither price is hit.")
    v = _latest_forecast(sym)
    if v:
        lines.append("  - " + risk_label(v, "stock")[1])
        if t["engine"] == "automaton" and stop and tgt and px:
            try:
                sp = abs(px - stop) / px
                tp = abs(tgt - px) / px
                sim = simulate(v, hold, side, sp, tp, 25.0)
                lines.append("  - What could happen: " + sim_plain(sim, 25.0))
            except Exception:
                pass
    else:
        lines.append("  - Risk: not rated yet (no price history loaded for this stock).")
    return "\n".join(lines)


_NOT_PROVEN = ("Please remember: these are research ideas from a program that has NOT yet proven it can pick winners. "
               "Nothing was bought or sold for you. Only put in money you can afford to lose, and you decide what to do.")


def _last_digest():
    """(sent_at, highest trade id already emailed) of the latest ideas email."""
    with get_db() as conn:
        _ensure(conn)
        r = conn.execute("SELECT sent_at, kind FROM vol_email_log WHERE kind LIKE 'digest_upto:%' ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None, 0
    return datetime.fromisoformat(r["sent_at"]), int(r["kind"].split(":")[1])


def _new_ideas_since(last_id: int):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, symbol, side, engine, entry_price, stop_price, target1_price, entry_score FROM intraday_trades "
            "WHERE id > ? AND is_hypothetical=1 AND exit_time IS NULL AND COALESCE(is_copy_trade,0)=0 "
            "AND engine IN ('low_value','automaton','high_value') ORDER BY id", (last_id,)).fetchall()
    return [dict(r) for r in rows]


def send_alerts(sender=notify.send_email) -> dict:
    """Plain-English emails: (1) new ideas, (2) your own checks when finished, (3) a weekly report card."""
    from models.trading.shared.plain import finished_check_text
    sent = []
    # 1) new trade ideas (at most one email a day)
    last_d, last_id = _last_digest()
    if last_d is None or (datetime.now(timezone.utc) - last_d).total_seconds() > 20 * 3600:
        everything = _new_ideas_since(last_id)
        ideas = sorted(everything, key=lambda t: -abs(t.get("entry_score") or 0))[:8]
        if ideas:
            body = (f"Predicta found {len(ideas)} new idea" + ("" if len(ideas) == 1 else "s") + " to look at:\n\n"
                    + "\n\n".join(_idea_lines(t) for t in ideas) + "\n\n" + _NOT_PROVEN)
            r = sender(f"Predicta: {len(ideas)} new idea" + ("" if len(ideas) == 1 else "s") + " to look at", body)
            if r.get("sent"):
                _mark_email(f"digest_upto:{max(t['id'] for t in everything)}"); sent.append("ideas")
            else:
                return {"sent": sent, "problem": r.get("reason")}
    # 2) her own checks that finished
    manual = _manual_resolved_unsent()
    if manual:
        lines = [finished_check_text(m["symbol"], m["as_of"], m["horizon"], m["forecast_vol"], m["actual_vol"], m["trailing_vol"] or 0) for m in manual]
        body = ("Your volatility checks have finished. This only measures HOW MUCH a price moved, not whether it went up or down.\n\n"
                + "\n\n".join(lines) + "\n\nSee the overall report card at http://127.0.0.1:8000/trade/volatility")
        r = sender("Predicta: your checks finished", body)
        if r.get("sent"):
            with get_db() as conn:
                conn.execute(f"UPDATE vol_forecast_log SET notified=1 WHERE id IN ({','.join(str(m['id']) for m in manual)})")
            sent.append("manual")
        else:
            return {"sent": sent, "problem": r.get("reason")}
    # 3) weekly report card
    sc = scorecard()
    last = _last_email("weekly")
    if sc["scored"] > 0 and (last is None or (datetime.now(timezone.utc) - last).days >= WEEKLY_DAYS):
        v = sc["verdict"]
        if v.startswith("TOO EARLY"):
            headline = "Not enough results yet to judge. I need about two weeks of results before I can say if the forecasts are any good."
        elif v.startswith("MODEL BEATS"):
            headline = "Good news so far: the forecasts are better than just assuming 'same as lately'."
        elif v.startswith("MODEL IS WORSE"):
            headline = "Bad news so far: the forecasts are WORSE than just assuming 'same as lately'. Don't rely on them."
        else:
            headline = "Too close to call: the forecasts are about as good as just assuming 'same as lately'."
        body = ("Weekly report card for Predicta's swing forecasts (stocks and forex).\n\n" + headline + "\n\n"
                f"Checked so far: {sc['scored']} forecasts. Still waiting on {sc['waiting_for_days_to_pass']} more.\n")
        if "model_avg_error" in sc:
            body += (f"Predicta's guesses missed by about {sc['model_avg_error']:.2f}% a day on average. "
                     f"The simple 'same as lately' guess missed by {sc['last_20_days_guess_avg_error']:.2f}%. "
                     f"Predicta's guess was the closer one {sc['model_closer_share']*100:.0f}% of the time.\n")
        body += "\nThis is about how big price moves are, not which way they go.\nDetails: http://127.0.0.1:8000/trade/volatility"
        r = sender("Predicta: weekly report card", body)
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
    try:
        res["history"] = ensure_history(force=True)
    except Exception as e:
        res["history"] = {"error": type(e).__name__}
    bars = load_bars()
    if not bars.empty:
        latest = bars["bar_date"].max()
        with get_db() as conn:
            _ensure(conn)
            have = conn.execute("SELECT COUNT(*) c FROM vol_forecast_log WHERE source IN ('auto','manual') AND as_of=? AND horizon=?", (latest, HORIZON)).fetchone()["c"]
        res["forecast"] = {"status": "already_done_for_" + latest} if have > 5 else forecast_universe(HORIZON, bars)
    try:
        _fx_refresh_if_due()
        res["fx"] = forecast_fx(HORIZON)
    except Exception as e:
        res["fx"] = {"error": type(e).__name__}
    res["resolved"] = resolve_forecasts()
    res["alerts"] = send_alerts() if notify.email_configured() else {"sent": [], "note": "email not set up"}
    return res


def current_ideas_text(symbol: str) -> str:
    """What Predicta's engines currently say about this symbol, in plain words (read-only)."""
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, symbol, side, engine, entry_price, stop_price, target1_price FROM intraday_trades "
            "WHERE symbol=? AND exit_time IS NULL AND engine IN ('low_value','automaton','high_value') ORDER BY id DESC LIMIT 3", (symbol,))]
    if not rows:
        return ("Predicta has no buy or sell idea on this stock right now. This page only tells you how risky it is "
                "and how much to buy if you decide to buy it yourself.")
    return "Predicta's engines currently have these ideas on it:\n\n" + "\n\n".join(_idea_lines(t) for t in rows)


def automaton_scenarios() -> list:
    """What-if ranges for every open Automaton position/idea over its remaining days (no effect on trading)."""
    from models.trading.shared.scenario import simulate, plain as sim_plain
    out = []
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, symbol, side, entry_price, stop_price, target1_price, entry_time, is_hypothetical FROM intraday_trades "
            "WHERE engine='automaton' AND exit_time IS NULL ORDER BY id")]
    for t in rows:
        v = _latest_forecast(t["symbol"])
        if not v or not t["entry_price"] or not t["stop_price"] or not t["target1_price"]:
            out.append({"symbol": t["symbol"], "text": "No price history loaded for this stock yet, so I can't run the what-if."})
            continue
        try:
            held = max((datetime.now(timezone.utc).date() - datetime.fromisoformat(str(t["entry_time"]).replace("Z", "+00:00")).date()).days * 5 // 7, 0)
        except Exception:
            held = 0
        left = max(126 - held, 1)
        sp, tp = abs(t["entry_price"] - t["stop_price"]) / t["entry_price"], abs(t["target1_price"] - t["entry_price"]) / t["entry_price"]
        sim = simulate(v, left, t["side"], sp, tp, 25.0)
        out.append({"symbol": t["symbol"], "side": t["side"], "days_left": left, "text": sim_plain(sim, 25.0), "sim": sim})
    return out


def open_ideas(limit: int = 40) -> list:
    """All open ideas/positions across the three engines with plain text, strongest first."""
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, symbol, side, engine, entry_price, stop_price, target1_price, entry_score, is_hypothetical FROM intraday_trades "
            "WHERE exit_time IS NULL AND COALESCE(is_copy_trade,0)=0 AND engine IN ('low_value','automaton','high_value') "
            "ORDER BY ABS(COALESCE(entry_score,0)) DESC LIMIT ?", (limit,))]
    out = []
    for t in rows:
        out.append({"symbol": t["symbol"], "engine": t["engine"], "side": t["side"], "you_hold_it": not t["is_hypothetical"],
                    "text": _idea_lines(t)})
    return out
