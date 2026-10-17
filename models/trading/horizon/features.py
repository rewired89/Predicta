"""
Horizon Forecast (2026-10-14): point-in-time features + labels from daily bars.

Every feature at date t uses only bars dated <= t. The label is the forward
h-day return minus SPY's forward h-day return (did it beat the market), and
exists only where t+h bars exist. Nothing here touches other engines.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

BENCH = "SPY"
MIN_PRICE = 1.0
MIN_HISTORY = 70
FEATURES = ["ret_5", "ret_20", "ret_60", "vol_20", "dist_high_60", "dist_low_60",
            "rsi_14", "vol_ratio", "rel_5", "rel_20", "mkt_ret_20", "mkt_vol_20"]
# Candle-shape features (borrowed idea from Kronos, which reads full candles, not just closes).
# All are ratios, so no price level leaks in. Only used when open/high/low bars exist.
CANDLE_FEATURES = ["body_5", "upper_wick_5", "lower_wick_5", "range_5", "gap_5"]


def feature_cols(panel: pd.DataFrame) -> list[str]:
    return FEATURES + [c for c in CANDLE_FEATURES if c in panel.columns]


def load_bars() -> pd.DataFrame:
    """Long frame (symbol, bar_date, close, volume) from market_daily_bars."""
    from db.database import get_db
    with get_db() as conn:
        rows = conn.execute("SELECT symbol, bar_date, open, high, low, close, volume FROM market_daily_bars "
                            "WHERE close IS NOT NULL ORDER BY bar_date").fetchall()
    return pd.DataFrame([dict(r) for r in rows],
                        columns=["symbol", "bar_date", "open", "high", "low", "close", "volume"])


def _rsi(close: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    d = close.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def build_panel(bars: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """
    Returns a long panel indexed by (bar_date, symbol) with FEATURES and, where
    available, `fwd_excess` (forward h-day return minus SPY) and `label` (1 if > 0).
    Rows with price < MIN_PRICE or < MIN_HISTORY bars of history are dropped.
    """
    if bars.empty or BENCH not in set(bars["symbol"]):
        return pd.DataFrame()
    close = bars.pivot_table(index="bar_date", columns="symbol", values="close").sort_index()
    vol = bars.pivot_table(index="bar_date", columns="symbol", values="volume").sort_index()
    spy = close[BENCH]

    ret1 = close.pct_change(fill_method=None)
    f = {
        "ret_5": close.pct_change(5, fill_method=None),
        "ret_20": close.pct_change(20, fill_method=None),
        "ret_60": close.pct_change(60, fill_method=None),
        "vol_20": ret1.rolling(20).std(),
        "dist_high_60": close / close.rolling(60).max() - 1,
        "dist_low_60": close / close.rolling(60).min() - 1,
        "rsi_14": _rsi(close),
        "vol_ratio": vol / vol.rolling(20).mean(),
    }
    has_candles = all(c in bars.columns for c in ("open", "high", "low")) and bars[["open", "high", "low"]].notna().any().all()
    if has_candles:
        op = bars.pivot_table(index="bar_date", columns="symbol", values="open").sort_index()
        hi = bars.pivot_table(index="bar_date", columns="symbol", values="high").sort_index()
        lo = bars.pivot_table(index="bar_date", columns="symbol", values="low").sort_index()
        rng = (hi - lo).replace(0, np.nan)
        body_top = np.maximum(op, close)
        body_bot = np.minimum(op, close)
        f["body_5"] = ((close - op) / rng).rolling(5).mean()
        f["upper_wick_5"] = ((hi - body_top) / rng).rolling(5).mean()
        f["lower_wick_5"] = ((body_bot - lo) / rng).rolling(5).mean()
        f["range_5"] = (rng / close).rolling(5).mean()
        f["gap_5"] = (op / close.shift(1) - 1).rolling(5).mean()
    spy5, spy20 = spy.pct_change(5), spy.pct_change(20)
    f["rel_5"] = f["ret_5"].sub(spy5, axis=0)
    f["rel_20"] = f["ret_20"].sub(spy20, axis=0)
    mkt20 = spy20
    mktvol = spy.pct_change().rolling(20).std()

    fwd_vol = ret1.rolling(horizon).std().shift(-horizon)
    fwd = close.shift(-horizon) / close - 1
    spy_fwd = spy.shift(-horizon) / spy - 1
    excess = fwd.sub(spy_fwd, axis=0)

    parts = {k: v.stack(future_stack=True) for k, v in f.items()}
    panel = pd.DataFrame(parts)
    panel["mkt_ret_20"] = panel.index.get_level_values(0).map(mkt20)
    panel["mkt_vol_20"] = panel.index.get_level_values(0).map(mktvol)
    panel["fwd_excess"] = excess.stack(future_stack=True)
    panel["fwd_vol"] = fwd_vol.stack(future_stack=True)
    panel.index.names = ["bar_date", "symbol"]

    hist = close.notna().cumsum().stack(future_stack=True)
    px = close.stack(future_stack=True)
    keep = (px >= MIN_PRICE) & (hist >= MIN_HISTORY)
    panel = panel[keep.reindex(panel.index).fillna(False)]
    panel = panel.drop(index=BENCH, level="symbol", errors="ignore")
    cols = feature_cols(panel)
    panel = panel.replace([np.inf, -np.inf], np.nan).dropna(subset=cols)
    # clip extreme tails so one bad print can't dominate a linear model
    panel[cols] = panel[cols].clip(panel[cols].quantile(0.01), panel[cols].quantile(0.99), axis=1)
    panel["label"] = np.where(panel["fwd_excess"].isna(), np.nan, (panel["fwd_excess"] > 0).astype(float))
    return panel
