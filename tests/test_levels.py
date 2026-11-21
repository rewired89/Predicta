import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import models.trading.horizon.levels as L

# hand checks (dollars for price >= 10): 24 -> nearest prime 23 (gap 1); 100 -> 101 (gap 1); 90 -> 89 (gap 1); 114 -> 113 (gap 1); 25 -> 23 (gap 2); 119 -> 113 and 127? 119 is 7*17: primes 113,127 -> gap 6
f = L.level_features_from_prices(np.array([24.0, 100.0, 25.0, 119.0, 47.0]))
gaps = f["lvl_prime"] * np.array([24.0, 100.0, 25.0, 119.0, 47.0])
assert np.allclose(gaps, [1, 1, 2, 6, 0]), gaps            # 47 is itself prime
# round-5: 24 -> 1 away from 25; 100 -> 0; 47 -> 2 away from 45
r = f["lvl_round5"] * np.array([24.0, 100.0, 25.0, 119.0, 47.0])
assert np.allclose(r, [1, 0, 0, 1, 2]), r
# under $10 uses dimes: 4.7 -> 47 dimes (prime, gap 0); 4.6 -> 46 -> nearest prime 47 -> gap 1 dime = 0.1
g = L.level_features_from_prices(np.array([4.7, 4.6]))["lvl_prime"] * np.array([4.7, 4.6])
assert np.allclose(g, [0.0, 0.1]), g
# the random control has the same density as primes (within 5%)
P, R = L._sieve(200000), L._random_levels(200000)
assert abs(len(R) - len(P)) / len(P) < 0.05, (len(P), len(R))

# synthetic stock panel: 60 random-walk stocks + SPY, 900 days
from models.trading.horizon.features import build_panel
rnd = np.random.default_rng(5)
dates = [str(x.date()) for x in pd.bdate_range("2021-01-04", periods=900)]
rows = []
for sym in ["SPY"] + [f"S{i}" for i in range(60)]:
    sg = 0.008 if sym == "SPY" else float(rnd.uniform(0.01, 0.03))
    c = rnd.uniform(8, 120) * np.exp(np.cumsum(rnd.normal(0, sg, len(dates))))
    rows += [(sym, d, p, p * 1.004, p * 0.996, p, 1e6) for d, p in zip(dates, c)]
bars = pd.DataFrame(rows, columns=["symbol", "bar_date", "open", "high", "low", "close", "volume"])

res = L.run_levels_test(5, bars=bars)
print(res["plain"]); print(res["primes_vs_nothing"], res["primes_vs_random_levels"])
assert res["status"] == "ok" and res["months"] >= 10
assert not res["verdict_primes"].startswith("SOMETHING"), "random walk must not show a prime effect"

# planted effect: if the next-5-day label really depends on distance to a prime, the harness must see it, and must see it for primes only (not the random control)
orig = L.add_level_features
def planted(panel, b):
    pan = orig(panel, b)
    z = (pan["lvl_prime"] - pan["lvl_prime"].mean()) / pan["lvl_prime"].std()
    pr = 1 / (1 + np.exp(-2.5 * z))
    pan = pan.copy(); pan["label"] = (np.random.default_rng(1).random(len(pan)) < pr).astype(float)
    return pan
L.add_level_features = planted
res2 = L.run_levels_test(5, bars=bars)
print(res2["verdict_primes"], res2["primes_vs_nothing"], res2["primes_vs_random_levels"])
assert res2["verdict_primes"].startswith("SOMETHING"), res2
print("levels tests OK")
