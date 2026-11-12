"""
FX Forecast (research only, places no orders).

Question it answers honestly: can daily price history predict whether a
currency pair is higher h days from now, and does that survive the spread?
Walk-forward: train only on the past, with an h-day gap (embargo) before each
test block, then score on the future. Reports accuracy WITH a 95% range, the
profit after spreads, and an accuracy-at-confidence table so a claim like "80%"
can be checked against what is actually achievable.
"""
import math

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.preprocessing import StandardScaler

MIN_TRAIN = 750      # ~3 years of daily bars before the first test
RETRAIN_EVERY = 21   # ~monthly
FEATURES = ["ret1", "ret5", "ret20", "ret60", "vol20", "vol60", "vol_ratio",
            "dist50", "dist200", "rsi14", "range_pct", "body"]


def build(bars: pd.DataFrame, h: int) -> pd.DataFrame:
    d = bars.sort_values("date").reset_index(drop=True).copy()
    lc = np.log(d["c"])
    r = lc.diff()
    d["ret1"] = r
    for n in (5, 20, 60):
        d[f"ret{n}"] = lc - lc.shift(n)
    d["vol20"] = r.rolling(20).std()
    d["vol60"] = r.rolling(60).std()
    d["vol_ratio"] = d["vol20"] / d["vol60"]
    d["dist50"] = (d["c"] / d["c"].rolling(50).mean() - 1) / d["vol20"]
    d["dist200"] = (d["c"] / d["c"].rolling(200).mean() - 1) / d["vol20"]
    up = r.clip(lower=0).rolling(14).mean()
    dn = (-r.clip(upper=0)).rolling(14).mean()
    d["rsi14"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    d["range_pct"] = (d["h"] - d["l"]) / d["c"]
    d["body"] = (d["c"] - d["o"]) / (d["h"] - d["l"]).replace(0, np.nan)
    d["fwd_ret"] = lc.shift(-h) - lc                      # log return over next h days
    d["y"] = (d["fwd_ret"] > 0).astype(float).where(d["fwd_ret"].notna())   # unknown future stays unknown
    d["fwd_vol"] = r.rolling(h).std().shift(-h) if h >= 2 else np.nan
    d["cost"] = d["spread"] / d["c"]                      # round trip ~ one full spread
    return d


def _walk(d: pd.DataFrame, h: int, kind: str, feats=None) -> pd.DataFrame:
    feats = feats or FEATURES
    tgt = "y" if kind == "dir" else "fwd_vol"
    ok = d.replace([np.inf, -np.inf], np.nan).dropna(subset=feats + [tgt, "fwd_ret", "cost"]).reset_index(drop=True)
    out = []
    i = MIN_TRAIN
    while i < len(ok):
        tr = ok.iloc[: max(i - h, 0)]                     # embargo: labels must be fully in the past
        te = ok.iloc[i: i + RETRAIN_EVERY]
        if len(tr) < 300 or te.empty:
            break
        lo, hi = tr[feats].quantile(0.01), tr[feats].quantile(0.99)
        Xtr = tr[feats].clip(lo, hi, axis=1)
        Xte = te[feats].clip(lo, hi, axis=1)
        sc = StandardScaler().fit(Xtr)
        if kind == "dir":
            m = LogisticRegression(C=0.05, max_iter=500).fit(sc.transform(Xtr), tr["y"])
            p = m.predict_proba(sc.transform(Xte))[:, 1]
            base = float(tr["y"].mean())
            out.append(te.assign(p=p, base=base))
        else:
            m = Ridge(alpha=50.0).fit(sc.transform(Xtr), np.log(tr["fwd_vol"]))
            out.append(te.assign(pred_logvol=m.predict(sc.transform(Xte))))
        i += RETRAIN_EVERY
    return pd.concat(out) if out else pd.DataFrame()


def clean(x):
    """Make results JSON-safe: NaN/inf become None."""
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, (float, np.floating)):
        return None if not math.isfinite(x) else float(x)
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


def _boot(xs, n=2000, seed=1):
    rnd = np.random.default_rng(seed)
    xs = np.asarray(xs)
    m = np.sort([rnd.choice(xs, len(xs)).mean() for _ in range(n)])
    return float(m[int(0.025 * n)]), float(m[int(0.975 * n)])


def _acc_ci(hits: np.ndarray):
    n = len(hits)
    if n == 0:
        return None
    a = hits.mean()
    se = math.sqrt(max(a * (1 - a), 1e-9) / n)
    return float(a), float(a - 1.96 * se), float(a + 1.96 * se)


def run_direction(bars: pd.DataFrame, h: int = 5, pair: str = "", frame=None, feats=None) -> dict:
    return clean(_run_direction(bars, h, pair, frame, feats))


def _run_direction(bars: pd.DataFrame, h: int, pair: str, frame=None, feats=None) -> dict:
    d = frame if frame is not None else build(bars, h)
    feats = feats or FEATURES
    res = _walk(d, h, "dir", feats)
    if res.empty:
        return {"pair": pair, "horizon_days": h, "verdict": "TOO LITTLE DATA",
                "detail": f"need more than {MIN_TRAIN + 300} daily bars; have {len(d)}"}
    ns = res.iloc[::h]                                    # non-overlapping periods (honest sample size)
    pred_up = ns["p"] > 0.5
    hits = (pred_up == (ns["y"] > 0)).to_numpy().astype(float)
    acc, alo, ahi = _acc_ci(hits)
    base_acc = float(max(ns["y"].mean(), 1 - ns["y"].mean()))

    # trading result after the spread, only when confident enough; one trade per h days
    side = np.where(ns["p"] > 0.5, 1.0, -1.0)
    net = side * ns["fwd_ret"].to_numpy() - ns["cost"].to_numpy()
    lo, hi = _boot(net)

    table = []
    for thr in (0.0, 0.02, 0.05, 0.08, 0.12):
        sel = (ns["p"] - 0.5).abs() >= thr
        if int(sel.sum()) < 1:
            continue
        hh = hits[sel.to_numpy()]
        row = {"min_confidence_gap": thr, "trades": int(sel.sum()), "accuracy": round(float(hh.mean()), 3)}
        table.append(row)
    best = max((t for t in table if t["trades"] >= 30), key=lambda t: t["accuracy"], default=None)

    if len(ns) < 30:
        verdict = "TOO LITTLE DATA"
    elif lo > 0 and alo > 0.5:
        verdict = "POSSIBLE EDGE (needs forward confirmation on new data)"
    elif hi < 0:
        verdict = "NO EDGE (loses money after spreads)"
    else:
        verdict = "UNPROVEN (profit range includes zero)"
    return {
        "pair": pair, "horizon_days": h, "test_periods": int(len(ns)),
        "accuracy": round(acc, 3), "accuracy_95ci": [round(alo, 3), round(ahi, 3)],
        "always_pick_the_commoner_side_accuracy": round(base_acc, 3),
        "mean_net_return_pct_per_trade": round(float(net.mean()) * 100, 4),
        "net_return_95ci_pct": [round(lo * 100, 4), round(hi * 100, 4)],
        "avg_spread_pct": round(float(ns["cost"].mean()) * 100, 4),
        "accuracy_by_confidence": table,
        "best_accuracy_with_30plus_trades": best,
        "reached_80_percent": bool(best and best["accuracy"] >= 0.80),
        "verdict": verdict,
        "note": "Accuracy is scored on non-overlapping periods only. Picking the best cutoff after looking at results overstates real skill.",
    }


def run_volatility(bars: pd.DataFrame, h: int = 5, pair: str = "") -> dict:
    return clean(_run_volatility(bars, h, pair))


def _run_volatility(bars: pd.DataFrame, h: int, pair: str) -> dict:
    d = build(bars, max(h, 2))
    res = _walk(d, max(h, 2), "vol")
    if res.empty:
        return {"pair": pair, "verdict": "TOO LITTLE DATA"}
    ns = res.iloc[::max(h, 2)]
    actual = np.log(ns["fwd_vol"].to_numpy())
    model_mae = float(np.abs(actual - ns["pred_logvol"].to_numpy()).mean())
    base_mae = float(np.abs(actual - np.log(ns["vol20"].to_numpy())).mean())
    imp = (base_mae - model_mae) / base_mae * 100
    return {"pair": pair, "horizon_days": h, "test_periods": int(len(ns)),
            "model_error": round(model_mae, 4), "same_as_last_20_days_error": round(base_mae, 4),
            "improvement_pct": round(imp, 2),
            "verdict": "BEATS the simple guess" if imp > 2 else "NO BETTER than the simple guess"}


def predict_now(bars: pd.DataFrame, h: int = 5, pair: str = "") -> dict:
    return clean(_predict_now(bars, h, pair))


def _predict_now(bars: pd.DataFrame, h: int, pair: str) -> dict:
    d = build(bars, h)
    tr = d.dropna(subset=FEATURES + ["y"])
    tr = tr.iloc[: max(len(tr) - h, 0)]
    last = d.dropna(subset=FEATURES).iloc[-1:]
    if len(tr) < 300 or last.empty:
        return {"pair": pair, "status": "insufficient_data"}
    lo, hi = tr[FEATURES].quantile(0.01), tr[FEATURES].quantile(0.99)
    sc = StandardScaler().fit(tr[FEATURES].clip(lo, hi, axis=1))
    m = LogisticRegression(C=0.05, max_iter=500).fit(sc.transform(tr[FEATURES].clip(lo, hi, axis=1)), tr["y"])
    p = float(m.predict_proba(sc.transform(last[FEATURES].clip(lo, hi, axis=1)))[:, 1][0])
    return {"pair": pair, "horizon_days": h, "as_of": str(last["date"].iloc[0].date()),
            "prob_higher": round(p, 3),
            "lean": "UP" if p > 0.5 else "DOWN",
            "warning": "Research output only. Check /trade/fx/backtest first: if its verdict is not POSSIBLE EDGE, ignore this number."}


def carry_trade_baseline(frame: pd.DataFrame, h: int = 5) -> dict:
    """
    The classic carry trade with NO model: hold the higher-yielding currency for h days,
    earn the rate gap, pay the spread. Interbank rates are optimistic vs real broker
    financing, so treat this as the best case.
    """
    d = frame.replace([np.inf, -np.inf], np.nan).dropna(subset=["carry", "fwd_ret", "cost"]).iloc[::h]
    if len(d) < 30:
        return {"verdict": "TOO LITTLE DATA", "periods": int(len(d))}
    side = np.where(d["carry"] > 0, 1.0, -1.0)
    net = side * d["fwd_ret"].to_numpy() + side * d["carry"].to_numpy() / 100.0 * h / 365.0 - d["cost"].to_numpy()
    lo, hi = _boot(net)
    return clean({
        "periods": int(len(d)),
        "mean_net_return_pct_per_trade": round(float(net.mean()) * 100, 4),
        "net_return_95ci_pct": [round(lo * 100, 4), round(hi * 100, 4)],
        "win_rate": round(float((net > 0).mean()), 3),
        "verdict": ("POSSIBLE EDGE (needs forward confirmation)" if lo > 0
                    else "NO EDGE (loses after spreads)" if hi < 0 else "UNPROVEN (profit range includes zero)"),
    })
