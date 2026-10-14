"""
Horizon Forecast (2026-10-14): walk-forward test of "will this stock beat SPY
over the next h trading days".

Train on the past only, with an embargo of h days so no training label overlaps
the test block, test on the next calendar month, roll forward. Reports whether
the model beats the dumb baseline (always predict the base rate). A model that
does not beat it has no edge, whatever its accuracy looks like.

Known limits (reported in the output, not hidden): survivorship bias when the
symbol universe is today's tradable list; overlapping h-day windows make
per-row statistics look more significant than they are, so the headline edge
number is computed per month.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline

from models.trading.horizon.features import FEATURES, build_panel, load_bars

MIN_TRAIN_ROWS = 3000
COST_BPS_PER_SIDE = 10.0
TOP_FRAC = 0.1


def _model():
    return make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=300))


def walk_forward(panel: pd.DataFrame, horizon: int) -> pd.DataFrame:
    data = panel.dropna(subset=["label"])
    dates = sorted(data.index.get_level_values(0).unique())
    if len(dates) < 120:
        return pd.DataFrame()
    months = sorted({d[:7] for d in dates})
    out = []
    for m in months:
        test_dates = [d for d in dates if d[:7] == m]
        first = dates.index(test_dates[0])
        cutoff = first - horizon  # embargo: last train label must resolve before test starts
        if cutoff <= 0:
            continue
        train_set = set(dates[:cutoff])
        idx = data.index.get_level_values(0)
        train = data[idx.isin(train_set)]
        if len(train) < MIN_TRAIN_ROWS or train["label"].nunique() < 2:
            continue
        test = data[idx.isin(set(test_dates))]
        if test.empty:
            continue
        mdl = _model().fit(train[FEATURES].values, train["label"].values)
        res = test[["label", "fwd_excess"]].copy()
        res["p"] = mdl.predict_proba(test[FEATURES].values)[:, 1]
        res["base"] = float(train["label"].mean())
        res["month"] = m
        out.append(res)
    return pd.concat(out) if out else pd.DataFrame()


def summarize(res: pd.DataFrame, horizon: int) -> dict:
    if res.empty:
        return {"status": "insufficient_data", "horizon_days": horizon}
    y, p, b = res["label"].values, res["p"].values, res["base"].values
    brier = float(np.mean((p - y) ** 2))
    brier_base = float(np.mean((b - y) ** 2))
    acc = float(np.mean((p > 0.5) == (y == 1)))
    acc_base = float(np.mean((b > 0.5) == (y == 1)))

    cal = []
    for lo in np.arange(0.0, 1.0, 0.1):
        m = (p >= lo) & (p < lo + 0.1)
        if m.sum() >= 50:
            cal.append({"bucket": f"{lo:.1f}-{lo + 0.1:.1f}", "n": int(m.sum()),
                        "predicted": round(float(p[m].mean()), 3), "actual": round(float(y[m].mean()), 3)})

    # per signal date: top-decile by p, mean excess return minus round-trip cost
    cost = 2 * COST_BPS_PER_SIDE / 1e4
    monthly = {}
    for d, g in res.groupby(level=0):
        k = max(1, int(len(g) * TOP_FRAC))
        top = g.nlargest(k, "p")["fwd_excess"].mean() - cost
        monthly.setdefault(g["month"].iloc[0], []).append(top)
    mvals = np.array([np.mean(v) for v in monthly.values()])
    return {
        "status": "ok", "horizon_days": horizon, "test_rows": int(len(res)),
        "months_tested": int(len(mvals)), "first_month": min(monthly), "last_month": max(monthly),
        "accuracy": round(acc, 4), "accuracy_baseline": round(acc_base, 4),
        "brier": round(brier, 5), "brier_baseline": round(brier_base, 5),
        "brier_skill": round(1 - brier / brier_base, 5),
        "calibration": cal,
        "top_decile_excess_after_cost_pct": round(float(mvals.mean()) * 100, 3),
        "months_positive_pct": round(float((mvals > 0).mean()) * 100, 1),
        "monthly_excess_std_pct": round(float(mvals.std()) * 100, 3),
        "cost_bps_per_side": COST_BPS_PER_SIDE,
        "verdict": _verdict(1 - brier / brier_base, float(mvals.mean()), len(mvals), float((mvals > 0).mean())),
        "caveats": ["survivorship bias if universe = today's tradable list",
                    "overlapping windows: per-row stats overstate significance; use the monthly numbers",
                    "no result here is a trading instruction"],
    }


def _verdict(skill: float, mean_excess: float, n_months: int, pos_frac: float) -> str:
    if n_months < 24:
        return "NOT ENOUGH HISTORY: fewer than 24 test months, no conclusion"
    if mean_excess <= 0 or pos_frac < 0.55:
        return "NO EDGE: top-ranked picks do not reliably beat SPY after costs"
    if skill <= 0.001:
        return "RANKING SIGNAL ONLY: top picks beat SPY but probabilities are not calibrated, do not read p as a real chance"
    return "POSSIBLE EDGE: beats baseline, still needs out-of-sample confirmation before any use"


def run_backtest(horizon: int = 5, bars: pd.DataFrame | None = None) -> dict:
    bars = load_bars() if bars is None else bars
    panel = build_panel(bars, horizon)
    if panel.empty:
        return {"status": "insufficient_data", "horizon_days": horizon, "detail": "no bars or SPY missing"}
    out = summarize(walk_forward(panel, horizon), horizon)
    out["symbols"] = int(panel.index.get_level_values(1).nunique())
    return out


def predict_symbol(symbol: str, horizon: int = 5, bars: pd.DataFrame | None = None) -> dict:
    """P(symbol beats SPY over the next `horizon` days), trained only on resolved labels."""
    symbol = symbol.upper()
    bars = load_bars() if bars is None else bars
    panel = build_panel(bars, horizon)
    if panel.empty or symbol not in set(panel.index.get_level_values(1)):
        return {"status": "insufficient_data", "symbol": symbol,
                "detail": "symbol not in recorded bars or under 70 days of history"}
    train = panel.dropna(subset=["label"])
    if len(train) < MIN_TRAIN_ROWS or train["label"].nunique() < 2:
        return {"status": "insufficient_data", "symbol": symbol, "detail": f"only {len(train)} labelled rows"}
    mdl = _model().fit(train[FEATURES].values, train["label"].values)
    last = panel.xs(symbol, level=1).sort_index().iloc[-1:]
    p = float(mdl.predict_proba(last[FEATURES].values)[0, 1])
    return {"status": "ok", "symbol": symbol, "as_of": str(last.index[0]), "horizon_days": horizon,
            "p_beats_spy": round(p, 3), "base_rate": round(float(train["label"].mean()), 3),
            "note": "Only meaningful if /trade/horizon/backtest shows a POSSIBLE EDGE for this horizon."}
