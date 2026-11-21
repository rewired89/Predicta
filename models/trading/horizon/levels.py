"""
"Prime number levels" test (research only, changes no engine).

Idea from the user: some traders use prime numbers as support/resistance levels. We test it
the honest way: add "distance from the price to the nearest prime-number level" as a feature
to the same walk-forward stock test, and compare it with TWO controls:
  * a RANDOM control with exactly the same density of levels as the primes (primes thin out
    like 1/ln(n)), so any gain must come from primes being special, not from "having levels";
  * the classic ROUND-number levels (multiples of 5), the one version with a real,
    published (weak) market story: orders cluster at round prices.
Prices are scaled to whole units first: dollars for prices of $10 and up, dimes below $10.
"""
import math

import numpy as np
import pandas as pd

from models.trading.horizon.backtest import MIN_TRAIN_ROWS, walk_forward
from models.trading.horizon.features import build_panel, feature_cols, load_bars

LEVEL_FEATURES = ["lvl_prime", "lvl_random", "lvl_round5"]


def _sieve(n: int) -> np.ndarray:
    s = np.ones(n + 1, dtype=bool)
    s[:2] = False
    for i in range(2, int(n ** 0.5) + 1):
        if s[i]:
            s[i * i::i] = False
    return np.nonzero(s)[0]


def _random_levels(n: int) -> np.ndarray:
    """Pseudo-random level set with the same thinning as primes: keep m with probability 1/ln(m)."""
    m = np.arange(3, n + 1, dtype=np.uint64)
    h = ((m * np.uint64(2654435761)) % np.uint64(2 ** 32)).astype(np.float64) / 2 ** 32
    return m[h < 1.0 / np.log(m.astype(np.float64))].astype(np.int64)


def _nearest_gap(vals: np.ndarray, levels: np.ndarray) -> np.ndarray:
    i = np.searchsorted(levels, vals)
    lo = levels[np.clip(i - 1, 0, len(levels) - 1)]
    hi = levels[np.clip(i, 0, len(levels) - 1)]
    return np.minimum(np.abs(vals - lo), np.abs(hi - vals))


def level_features_from_prices(close: np.ndarray) -> dict:
    """Fractions of price (0.01 = one percent away from the nearest level). Pure function, easy to hand-check."""
    close = np.asarray(close, dtype=float)
    scale = np.where(close >= 10, 1.0, 10.0)
    n = np.maximum(np.rint(close * scale), 2).astype(np.int64)
    top = int(n.max()) + 50
    primes, rnd = _sieve(top), _random_levels(top)
    units = close * scale
    r5 = np.abs(units - np.rint(units / 5.0) * 5.0)
    return {"lvl_prime": _nearest_gap(units, primes) / units,
            "lvl_random": _nearest_gap(units, rnd) / units,
            "lvl_round5": r5 / units}


def add_level_features(panel: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    px = bars.set_index(["bar_date", "symbol"])["close"]
    px = px[~px.index.duplicated(keep="last")]
    close = px.reindex(panel.index).to_numpy()
    out = panel.copy()
    ok = np.isfinite(close) & (close > 0)
    out = out[ok]
    for k, v in level_features_from_prices(close[ok]).items():
        out[k] = v
    return out


def _boot(xs, n=2000, seed=1):
    rnd = np.random.default_rng(seed)
    xs = np.asarray(xs)
    m = np.sort([rnd.choice(xs, len(xs)).mean() for _ in range(n)])
    return float(m[int(0.025 * n)]), float(m[int(0.975 * n)])


def _monthly_brier(res: pd.DataFrame) -> pd.Series:
    return ((res["p"] - res["label"]) ** 2).groupby(res["month"]).mean()


def run_levels_test(horizon: int = 5, bars=None, panel=None) -> dict:
    bars = load_bars() if bars is None else bars
    base_panel = build_panel(bars, horizon) if panel is None else panel
    if base_panel.empty:
        return {"status": "insufficient_data"}
    pan = add_level_features(base_panel, bars)
    base = feature_cols(base_panel)
    variants = {"base": base, "plus_primes": base + ["lvl_prime"], "plus_random_levels": base + ["lvl_random"],
                "plus_round5": base + ["lvl_round5"]}
    runs = {k: walk_forward(pan, horizon, cols) for k, cols in variants.items()}
    if any(r.empty for r in runs.values()):
        return {"status": "insufficient_data", "detail": f"need at least {MIN_TRAIN_ROWS} training rows and 120 days"}
    mb = {k: _monthly_brier(r) for k, r in runs.items()}
    months = sorted(set.intersection(*[set(s.index) for s in mb.values()]))
    mb = {k: s.loc[months] for k, s in mb.items()}
    out = {"status": "ok", "horizon_days": horizon, "months": len(months), "variants": {}}
    for k, r in runs.items():
        out["variants"][k] = {"brier": round(float(mb[k].mean()), 5),
                              "accuracy": round(float(((r["p"] > 0.5) == (r["label"] > 0)).mean()), 4)}

    def paired(a, b):          # positive = a has the LOWER (better) Brier than b
        d = (mb[b] - mb[a]).to_numpy()
        lo, hi = _boot(d)
        return {"mean_gain": round(float(d.mean()), 6), "ci95": [round(lo, 6), round(hi, 6)]}
    out["primes_vs_nothing"] = paired("plus_primes", "base")
    out["primes_vs_random_levels"] = paired("plus_primes", "plus_random_levels")
    out["round5_vs_nothing"] = paired("plus_round5", "base")
    pn, pr = out["primes_vs_nothing"], out["primes_vs_random_levels"]
    if pn["ci95"][0] > 0 and pr["ci95"][0] > 0:
        out["verdict_primes"] = "SOMETHING TO LOOK AT: primes helped and beat random levels (still needs new data to confirm)"
    elif pn["ci95"][0] > 0:
        out["verdict_primes"] = "NOT PRIMES: levels helped, but random levels helped just as much"
    else:
        out["verdict_primes"] = "NO EVIDENCE: prime levels did not improve the forecast"
    rn = out["round5_vs_nothing"]
    out["verdict_round_numbers"] = ("SOMETHING TO LOOK AT" if rn["ci95"][0] > 0 else
                                    "NO EVIDENCE" if rn["ci95"][1] >= 0 else "WORSE THAN NOTHING")
    out["plain"] = (f"Tested on {len(months)} months, walk-forward. Prime-number levels: {out['verdict_primes']}. "
                    f"Round-number levels: {out['verdict_round_numbers']}. 'Random levels' is the fair control: if primes cannot beat "
                    f"levels placed at random with the same spacing, primes are not special.")
    return out
