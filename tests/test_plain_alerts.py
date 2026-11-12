import os, sys, tempfile
os.environ["PREDICTA_DB_PATH"] = tempfile.mkdtemp() + "/t.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import db.database as d; d.init_db()
from db.database import get_db
import models.trading.shared.vol_tracking as vt
from models.trading.shared.plain import risk_label, finished_check_text
from models.trading.shared.scenario import simulate

# risk wording
assert risk_label(0.01)[0] == "LOW" and risk_label(0.02)[0] == "MEDIUM" and risk_label(0.05)[0] == "HIGH"
assert risk_label(0.002, "fx")[0] == "LOW" and risk_label(0.009, "fx")[0] == "HIGH"
t = finished_check_text("IPO", "2026-11-10", 5, 0.0119, 0.0105, 0.0275)
assert "Predicta's guess was closer" in t and "1.19%" in t

# scenario sanity: wider stop is hit less often; target and stop symmetric -> roughly equal chances; zero vol never hits
a = simulate(0.02, 20, "long", 0.05, 0.05); b = simulate(0.02, 20, "long", 0.15, 0.15)
assert a["chance_stop_hit_first_pct"] > b["chance_stop_hit_first_pct"]
assert abs(a["chance_stop_hit_first_pct"] - a["chance_target_hit_first_pct"]) < 6
assert simulate(1e-9, 20, "long", 0.05, 0.05)["chance_neither_pct"] == 100.0
sh = simulate(0.02, 20, "short", 0.05, 0.05); assert abs(sh["chance_stop_hit_first_pct"] - a["chance_stop_hit_first_pct"]) < 6

# forecasts exist for the stocks so risk lines appear
vt.log_forecast("ABC", "2026-11-10", 5, 0.025, 0.03, 10.0, "auto")
vt.log_forecast("XYZ", "2026-11-10", 5, 0.012, 0.02, 5.0, "auto")
with get_db() as c:
    ins = ("INSERT INTO intraday_trades (symbol, side, engine, is_hypothetical, entry_time, entry_price, stop_price, target1_price, entry_score, logged_at) "
           "VALUES (?,?,?,1,'2026-11-10',?,?,?,?, '2026-11-10')")
    c.execute(ins, ("ABC", "long", "low_value", 10.0, 9.0, 11.0, 55))
    c.execute(ins, ("XYZ", "short", "automaton", 5.0, 7.5, 2.5, 48))
sent = []
fake = lambda s, b: (sent.append((s, b)) or {"sent": True})
r = vt.send_alerts(fake); print(r)
assert r["sent"][0] == "ideas" and "2 new ideas" in sent[0][0]
body = sent[0][1]; print(body)
assert "BUY ABC around $10.00" in body and "SELL SHORT XYZ" in body
assert "Stop: if it falls to $9.00" in body and "Sell for profit: when it reaches $11.00" in body
assert "Risk is MEDIUM" in body and "What could happen" in body and "NOT yet proven" in body
assert "Nothing was bought or sold for you" in body
assert not any(w in body for w in ("MAE", "sigma", "Brier", "bootstrap"))
assert vt.send_alerts(fake)["sent"] == []                 # same ideas are not emailed twice
with get_db() as c:
    c.execute(ins, ("NEW", "long", "low_value", 3.0, 2.7, 3.3, 60))
r2 = vt.send_alerts(fake); assert r2["sent"] == []        # throttled to one a day
with get_db() as c:
    c.execute("UPDATE vol_email_log SET sent_at='2020-01-01T00:00:00+00:00' WHERE kind LIKE 'digest_upto:%'")
r3 = vt.send_alerts(fake); assert r3["sent"] == ["ideas"] and "1 new idea" in sent[-1][0] and "NEW" in sent[-1][1] and "ABC" not in sent[-1][1]

# forex forecast + automatic scoring from fx_daily_bars
rnd = np.random.default_rng(4)
dates = [str(x.date()) for x in pd.bdate_range("2012-01-02", periods=3200)]
def mk(seed):
    r = np.random.default_rng(seed).normal(0, 0.005, len(dates)); c = 1.1 * np.exp(np.cumsum(r))
    return c
cut = dates[3190]
with get_db() as c:
    c.execute("CREATE TABLE IF NOT EXISTS fx_daily_bars (pair TEXT NOT NULL, date TEXT NOT NULL, o REAL, h REAL, l REAL, c REAL, spread REAL, PRIMARY KEY (pair, date))")
    for pair, seed in (("EUR_USD", 1), ("GBP_USD", 2), ("USD_JPY", 3)):
        cl = mk(seed) * (100 if pair == "USD_JPY" else 1)
        for dt, px in zip(dates[:3191], cl[:3191]):
            c.execute("INSERT INTO fx_daily_bars VALUES (?,?,?,?,?,?,?)", (pair, dt, px, px * 1.002, px * 0.998, px, px * 0.0001))
        globals()[pair] = cl
res = vt.forecast_fx(5); print(res); assert res["logged"] == 3
with get_db() as c:
    assert c.execute("SELECT COUNT(*) n FROM vol_forecast_log WHERE symbol IN ('EUR_USD','GBP_USD','USD_JPY')").fetchone()["n"] == 3
    for pair in ("EUR_USD", "GBP_USD", "USD_JPY"):
        cl = globals()[pair]
        for dt, px in zip(dates[3191:3196], cl[3191:3196]):
            c.execute("INSERT INTO fx_daily_bars VALUES (?,?,?,?,?,?,?)", (pair, dt, px, px, px, px, 0))
n = vt.resolve_forecasts(); print("resolved", n); assert n >= 3
with get_db() as c:
    row = c.execute("SELECT * FROM vol_forecast_log WHERE symbol='EUR_USD'").fetchone()
cl = EUR_USD[3190:3196]; hand = float(np.std(np.diff(cl) / cl[:-1], ddof=1))
assert abs(row["actual_vol"] - hand) < 1e-12

# forex readout text
from models.trading.shared.vol_sizing import fx_readout
fr = fx_readout("EUR_USD", 5, 5, 25); print(fr.get("plain"))
assert fr["status"] == "ok" and "no reliable call" in fr["direction_note"] and "pips" in fr["plain"]
print("plain alerts OK")
