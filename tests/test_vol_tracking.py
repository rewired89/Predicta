import os, sys, tempfile
os.environ["PREDICTA_DB_PATH"] = tempfile.mkdtemp() + "/t.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import db.database as d; d.init_db()
from db.database import get_db
import models.trading.shared.vol_tracking as vt

rnd = np.random.default_rng(2)
dates = [str(x.date()) for x in pd.bdate_range("2021-01-04", periods=900)]
sig = {"SPY": 0.008}; [sig.update({f"S{i}": float(rnd.uniform(0.006, 0.03))}) for i in range(60)]
rows = []
for sym, sg in sig.items():
    c = 20 * np.exp(np.cumsum(rnd.normal(0, sg, len(dates))))
    rows += [(sym, dt, px, px * 1.004, px * 0.996, px, 1e6) for dt, px in zip(dates, c)]
full = pd.DataFrame(rows, columns=["symbol", "bar_date", "open", "high", "low", "close", "volume"])

def store(df):
    with get_db() as conn:
        conn.execute("DELETE FROM market_daily_bars")
        conn.executemany("INSERT INTO market_daily_bars (symbol,bar_date,open,high,low,close,volume,vwap) VALUES (?,?,?,?,?,?,?,?)",
                         [(r.symbol, r.bar_date, r.open, r.high, r.low, r.close, r.volume, r.close) for r in df.itertuples()])

# forecast made when only the first 880 days exist; 20 days arrive later
cut = dates[879]
store(full[full.bar_date <= cut])
r = vt.forecast_universe(5, vt.load_bars()); print(r)
assert r["status"] == "ok" and r["logged"] >= 55 and r["as_of"] == cut
assert vt.forecast_universe(5, vt.load_bars())["logged"] >= 55   # rerun does not duplicate
with get_db() as conn:
    assert conn.execute("SELECT COUNT(*) c FROM vol_forecast_log").fetchone()["c"] == r["logged"]
assert vt.resolve_forecasts() == 0                                # nothing has happened yet
vt.log_forecast("S1", cut, 5, 0.02, 0.03, 20.0, "manual")         # her manual check marks the row hers
# future arrives
store(full[full.bar_date <= dates[884]])                          # exactly 5 more days
n = vt.resolve_forecasts(); print("resolved", n)
assert n >= 55
# hand check one symbol
with get_db() as conn:
    row = conn.execute("SELECT * FROM vol_forecast_log WHERE symbol='S1'").fetchone()
cl = full[full.symbol == "S1"].set_index("bar_date")["close"]
w = cl.loc[cut:dates[884]].values; hand = float(np.std(np.diff(w) / w[:-1], ddof=1))
assert abs(row["actual_vol"] - hand) < 1e-12 and abs(row["model_err"] - abs(row["forecast_vol"] - hand)) < 1e-12
sc = vt.scorecard(); print(sc)
assert sc["scored"] == n and "TOO EARLY" in sc["verdict"]          # 1 forecast day only
# emails: manual one goes out once, weekly one once
sent = []
fake = lambda s, b: (sent.append((s, b)) or {"sent": True})
a = vt.send_alerts(fake); print(a)
assert a["sent"] == ["manual", "weekly"] and len(sent) == 2 and "S1" in sent[0][1]
assert vt.send_alerts(fake)["sent"] == [] and len(sent) == 2       # no repeat
# a failing email does not mark things as sent
with get_db() as conn: conn.execute("UPDATE vol_forecast_log SET notified=0 WHERE symbol='S1'")
bad = vt.send_alerts(lambda s, b: {"sent": False, "reason": "no password"})
assert bad["sent"] == [] and bad["problem"] == "no password"
# job end to end (throttle + not configured)
os.environ.pop("SMTP_USER", None)
j = vt.daily_job(force=True); print(j); assert "forecast" in j and j["alerts"]["note"] == "email not set up"
assert vt.daily_job()["status"] == "throttled"
print("vol tracking OK")
