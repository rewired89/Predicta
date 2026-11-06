import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
from models.trading.fx.forecast import build, run_direction, run_volatility
from fetchers.oanda import parse_candles

def make(n, momentum=0.0, seed=0):
    rnd = np.random.default_rng(seed)
    vol = 0.006 * np.exp(np.cumsum(rnd.normal(0, 0.05, n)) * 0.2)      # clustering volatility
    r = np.zeros(n)
    for t in range(1, n):
        r[t] = momentum * np.sign(r[t-1:t].sum()) * vol[t] * 0.0 + rnd.normal(0, vol[t])
    if momentum:   # planted: next 5-day move follows trailing 20-day trend
        for t in range(25, n):
            r[t] += momentum * np.sign(r[t-20:t].sum()) * vol[t]
    c = 1.1 * np.exp(np.cumsum(r))
    o = np.r_[c[0], c[:-1]]
    hi = np.maximum(o, c) * (1 + abs(rnd.normal(0, .002, n)))
    lo = np.minimum(o, c) * (1 - abs(rnd.normal(0, .002, n)))
    return pd.DataFrame({"date": pd.bdate_range("2005-01-03", periods=n), "o": o, "h": hi, "l": lo, "c": c, "spread": c * 0.00012})

# 1) pure random walk: must NOT claim an edge, and must not hit 80%
rw = run_direction(make(4000, 0.0, 1), 5, "RW")
print("random walk:", rw["accuracy"], rw["accuracy_95ci"], rw["verdict"], rw["reached_80_percent"])
assert not rw["verdict"].startswith("POSSIBLE") and not rw["reached_80_percent"]

# 2) planted trend pattern: model should find real skill
mo = run_direction(make(4000, 0.25, 2), 5, "MOM")
print("planted:", mo["accuracy"], mo["accuracy_95ci"], mo["verdict"])
assert mo["accuracy"] > rw["accuracy"] and mo["accuracy"] > 0.53

# 3) volatility is learnable (clustering)
v = run_volatility(make(4000, 0.0, 3), 5, "VOL")
print("vol:", v["improvement_pct"], v["verdict"])

# 4) no look-ahead: features at t must not change when future bars change
a = make(1500, 0.0, 4); b = a.copy(); b.loc[1400:, "c"] *= 1.5
fa, fb = build(a, 5), build(b, 5)
cols = ["ret1", "ret5", "ret20", "vol20", "dist50", "rsi14"]
assert np.allclose(fa.loc[:1399, cols].fillna(0), fb.loc[:1399, cols].fillna(0))

# 5) OANDA parser
pl = {"candles": [{"complete": True, "time": "2026-01-02T22:00:00Z", "mid": {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.15"},
      "bid": {"c": "1.1499"}, "ask": {"c": "1.1501"}}, {"complete": False, "time": "2026-01-03T22:00:00Z"}]}
rows = parse_candles(pl); assert len(rows) == 1 and abs(rows[0]["spread"] - 0.0002) < 1e-9
print("FX tests OK")

# 6) regression: the last h days have no known future; results must be JSON-safe (real-data crash 2026-11-06)
import json
for sd in (7, 8):
    json.dumps(run_direction(make(4999, 0.0, sd), 5, "J"), allow_nan=False)
    json.dumps(run_volatility(make(4999, 0.0, sd), 5, "J"), allow_nan=False)
d2 = build(make(1500, 0.0, 9), 5)
assert d2["y"].iloc[-5:].isna().all() and d2["y"].iloc[:-5].notna().all()
print("FX json-safety OK")
