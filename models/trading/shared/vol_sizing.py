"""
Volatility readout for stock position sizing (read-only, changes no engine).

Idea, in plain words: a stock that is expected to swing a lot should get a wider
stop and a smaller position, so a normal bad day costs about the same dollars no
matter which stock you hold. The swing forecast comes from the Horizon model
(same data, same walk-forward discipline), which beat "same as the last 20 days".
"""
import math

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from models.trading.horizon.features import apply_clip, build_panel, feature_cols, fit_clip, load_bars

STOP_K = 1.5          # stop = 1.5 x one-sigma move over the hold (same rule Low Value's exits use)
MIN_STOP, MAX_STOP = 0.02, 0.50
MIN_TRAIN_ROWS = 3000


def size_from_vol(price: float, daily_vol: float, horizon: int, risk_dollars: float, max_position_dollars: float) -> dict:
    """Pure math, no data access. Stop distance from volatility, then shares so a stop-out loses ~risk_dollars."""
    one_sigma = daily_vol * math.sqrt(horizon)
    stop_pct = min(max(STOP_K * one_sigma, MIN_STOP), MAX_STOP)
    qty = risk_dollars / (price * stop_pct)
    dollars = qty * price
    capped = dollars > max_position_dollars
    if capped:
        dollars = max_position_dollars
        qty = dollars / price
    return {
        "expected_move_pct_1sigma": round(one_sigma * 100, 2),
        "stop_distance_pct": round(stop_pct * 100, 2),
        "stop_price_long": round(price * (1 - stop_pct), 4),
        "shares": round(qty, 4),
        "position_dollars": round(dollars, 2),
        "dollars_lost_if_stopped": round(qty * price * stop_pct, 2),
        "capped_by_max_position": bool(capped),
    }


def readout(symbol: str, horizon: int = 5, risk_dollars: float = 5.0, max_position_dollars: float = 25.0, bars=None) -> dict:
    symbol = symbol.upper()
    bars = load_bars() if bars is None else bars
    panel = build_panel(bars, horizon)
    if panel.empty or symbol not in set(panel.index.get_level_values(1)):
        return {"status": "insufficient_data", "symbol": symbol,
                "detail": "symbol not in recorded price history (or under 70 days). Use the Health Check page to load history."}
    cols = feature_cols(panel)
    train = panel.dropna(subset=["fwd_vol"])
    train = train[train["fwd_vol"] > 0]
    if len(train) < MIN_TRAIN_ROWS:
        return {"status": "insufficient_data", "symbol": symbol, "detail": f"only {len(train)} usable history rows, need {MIN_TRAIN_ROWS}"}
    bounds = fit_clip(train, cols)
    mdl = make_pipeline(StandardScaler(), Ridge(alpha=10.0)).fit(
        apply_clip(train, cols, bounds)[cols].values, np.log(train["fwd_vol"].values))
    last_raw = panel.xs(symbol, level=1).sort_index().iloc[-1:]
    last = apply_clip(last_raw, cols, bounds)
    pred = float(np.exp(mdl.predict(last[cols].values)[0]))
    trailing = float(last_raw["vol_20"].iloc[0])
    price = float(bars[bars["symbol"] == symbol].sort_values("bar_date")["close"].iloc[-1])
    out = {"status": "ok", "symbol": symbol, "as_of": str(last_raw.index[0]), "horizon_days": horizon,
           "last_price": round(price, 4),
           "forecast_daily_swing_pct": round(pred * 100, 3),
           "last_20_days_daily_swing_pct": round(trailing * 100, 3),
           "sizing": size_from_vol(price, pred, horizon, risk_dollars, max_position_dollars),
           "plain": None,
           "note": "Read-only guide. It changes nothing in Low Value, High Value or Automaton. "
                   "Check GET /trade/horizon/volatility to see whether the forecast still beats the simple guess."}
    s = out["sizing"]
    out["plain"] = (f"{symbol} is expected to swing about {s['expected_move_pct_1sigma']}% over {horizon} days. "
                    f"A stop {s['stop_distance_pct']}% below the entry (about ${s['stop_price_long']}) keeps a bad outcome near "
                    f"${s['dollars_lost_if_stopped']} on a ${s['position_dollars']} position ({s['shares']} shares)."
                    + (" The position was capped by your max-position limit, so the real risk is smaller." if s["capped_by_max_position"] else ""))
    return out
