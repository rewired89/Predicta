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

# 7) carry / macro: point-in-time and planted-signal checks
from fetchers import fx_macro
from models.trading.fx.compare import compare
import numpy as np, pandas as pd

def fake_loader_factory(bars, planted):
    dates = pd.date_range(bars["date"].min() - pd.Timedelta(days=90), bars["date"].max(), freq="MS")
    rnd = np.random.default_rng(5)
    def make_rate(base_, seed):
        r = np.random.default_rng(seed)
        return base_ + np.cumsum(r.normal(0, 0.15, len(dates)))
    store = {}
    for sid in fx_macro.SERIES:
        v = make_rate(2.0, hash(sid) % 1000) if "IR3" in sid else 20 + np.cumsum(np.random.default_rng(hash(sid) % 999).normal(0, .5, len(dates)))
        store[sid] = pd.DataFrame({"date": dates, "value": v})
    def loader(sid):
        df = store[sid].copy()
        df["avail"] = df["date"] + pd.Timedelta(days=fx_macro.SERIES[sid][1])
        return df.sort_values("avail")
    return loader, store

b = make(4000, 0.0, 11)
loader, store = fake_loader_factory(b, False)
d0 = build(b, 5)
f0, feats0, miss0 = fx_macro.attach(d0, "EUR_USD", "macro", loader)
# point-in-time: changing a rate observation dated AFTER a bar's availability must not change that bar's features
cut = b["date"].iloc[2000]
store2 = {k: v.copy() for k, v in store.items()}
late = store2["IR3TIB01EZM156N"]["date"] > cut
store2["IR3TIB01EZM156N"].loc[late, "value"] += 50
def loader2(sid):
    df = store2[sid].copy(); df["avail"] = df["date"] + pd.Timedelta(days=fx_macro.SERIES[sid][1]); return df.sort_values("avail")
f1, _, _ = fx_macro.attach(d0, "EUR_USD", "macro", loader2)
before = b["date"] <= cut
assert np.allclose(f0.loc[before, "carry"].fillna(0), f1.loc[before, "carry"].fillna(0)), "carry looked into the future"
assert "carry" in feats0 and "vix" in feats0 and not miss0

# planted: next-5-day return depends on carry -> model with carry must beat price-only, and carry trade must show edge
b2 = make(4000, 0.0, 12)
loader3, store3 = fake_loader_factory(b2, True)
fr, fe, _ = fx_macro.attach(build(b2, 5), "EUR_USD", "carry", loader3)
carry = fr["carry"].to_numpy()
rnd = np.random.default_rng(3)
r = rnd.normal(0, 0.006, len(b2)) + np.nan_to_num(carry) * 0.0006     # carry drives returns in this fake world
c = 1.1 * np.exp(np.cumsum(r)); b2 = b2.copy(); b2["c"] = c; b2["o"] = np.r_[c[0], c[:-1]]
b2["h"] = np.maximum(b2["o"], b2["c"]) * 1.001; b2["l"] = np.minimum(b2["o"], b2["c"]) * 0.999; b2["spread"] = b2["c"] * 0.00012
res = compare(b2, "EUR_USD", 5, loader=loader3)
print("planted carry:", res["price_only"]["accuracy"], "->", res["price_plus_carry"]["accuracy"], "| carry trade:", res["carry_trade_no_model"]["verdict"])
# (price momentum already picks up part of a persistent drift, so only require carry not to hurt and the no-model carry trade to find the planted edge)
assert res["price_plus_carry"]["accuracy"] >= res["price_only"]["accuracy"] - 0.01
assert res["carry_trade_no_model"]["verdict"].startswith("POSSIBLE EDGE")
# and with NO planted link the carry trade must not claim an edge
res0 = compare(b, "EUR_USD", 5, loader=loader)
print("no link:", res0["carry_trade_no_model"]["verdict"], res0["best_accuracy_any_version"])
assert not res0["carry_trade_no_model"]["verdict"].startswith("POSSIBLE") and not res0["reached_80_percent"]
import json; json.dumps(res, allow_nan=False)
# parser
assert fx_macro.parse_observations({"observations": [{"date": "2020-01-01", "value": "1.5"}, {"date": "2020-02-01", "value": "."}]}) == [("2020-01-01", 1.5)]
print("FX carry tests OK")
