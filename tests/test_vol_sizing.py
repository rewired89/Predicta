import os, sys, tempfile
os.environ["PREDICTA_DB_PATH"] = tempfile.mkdtemp() + "/t.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd, json
import db.database as _d; _d.init_db()
from models.trading.shared.vol_sizing import size_from_vol, readout

# pure math, by hand: daily vol 2%, 5 days -> 1 sigma = 4.47%; stop = 1.5x = 6.71%; $5 risk on $10 stock -> 7.45 sh = $74.5 -> capped at $25
s = size_from_vol(10.0, 0.02, 5, 5.0, 25.0)
assert abs(s["stop_distance_pct"] - 6.71) < 0.01 and s["capped_by_max_position"] and s["position_dollars"] == 25.0
assert abs(s["dollars_lost_if_stopped"] - 25 * 0.0671) < 0.02
# wilder stock gets a smaller position for the same risk
a = size_from_vol(10.0, 0.01, 5, 5.0, 1000.0); b = size_from_vol(10.0, 0.04, 5, 5.0, 1000.0)
assert b["shares"] < a["shares"] and abs(a["dollars_lost_if_stopped"] - 5) < 0.05 and abs(b["dollars_lost_if_stopped"] - 5) < 0.05
# clamps
assert size_from_vol(10, 0.0001, 5, 5, 1e9)["stop_distance_pct"] == 2.0 and size_from_vol(10, 0.5, 5, 5, 1e9)["stop_distance_pct"] == 50.0

# end to end on synthetic bars (60 symbols + SPY, 900 days): volatile stock must forecast higher swing than a calm one
rnd = np.random.default_rng(1); dates = [str(d.date()) for d in pd.bdate_range("2021-01-04", periods=900)]
rows = []
sig = {"SPY": 0.008, "CALM": 0.006, "WILD": 0.03}
for i in range(60): sig[f"S{i}"] = float(rnd.uniform(0.006, 0.035))
for sym, sg in sig.items():
    c = 20 * np.exp(np.cumsum(rnd.normal(0, sg, len(dates))))
    for d, px in zip(dates, c):
        rows.append({"symbol": sym, "bar_date": d, "open": px, "high": px * 1.005, "low": px * 0.995, "close": px, "volume": 1e6})
bars = pd.DataFrame(rows)
w = readout("WILD", 5, 5, 25, bars=bars); c = readout("CALM", 5, 5, 25, bars=bars)
print(w["status"], w.get("forecast_daily_swing_pct"), c.get("forecast_daily_swing_pct"))
assert w["status"] == "ok" and w["forecast_daily_swing_pct"] > 2 * c["forecast_daily_swing_pct"]
assert w["sizing"]["stop_distance_pct"] > c["sizing"]["stop_distance_pct"]
json.dumps(w, allow_nan=False)
assert readout("NOPE", 5, 5, 25, bars=bars)["status"] == "insufficient_data"
print("vol sizing OK")

# endpoint: missing symbol triggers one on-demand fetch, then retries (fetch mocked)
import fetchers.horizon_backfill as hb
calls = []
def fake_backfill(years=3, symbols=None):
    calls.append(symbols); return {"symbols_with_data": 0}
hb.backfill = fake_backfill
import models.trading.shared.vol_sizing as vs
vs.load_bars = lambda: bars
import app as _app
from fastapi.testclient import TestClient
r = TestClient(_app.app).get("/trade/volatility/data?symbol=ZZZZ").json()
assert calls == [["ZZZZ", "SPY"]] and r["status"] == "insufficient_data" and "find a stock called" in r["detail"], (calls, r)
print("on-demand fetch OK")
