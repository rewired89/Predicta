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
        return {"status": "insufficient_data", "symbol": symbol, "reason": "symbol_missing",
                "detail": "This symbol (or SPY, the market benchmark) is not in the recorded price history, or has under 70 days."}
    cols = feature_cols(panel)
    train = panel.dropna(subset=["fwd_vol"])
    train = train[train["fwd_vol"] > 0]
    if len(train) < MIN_TRAIN_ROWS:
        return {"status": "insufficient_data", "symbol": symbol, "reason": "too_little_history",
                "detail": f"The forecast needs a bigger price history than is loaded now ({len(train)} rows, needs {MIN_TRAIN_ROWS}). "
                          f"Load the 3-year history for the whole list first (Health Check page, or POST /trade/horizon/backfill?years=3) and let it finish, then try again."}
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


def fx_size(pair: str, price: float, daily_vol: float, horizon: int, risk_dollars: float, max_position_dollars: float) -> dict:
    pip = 0.01 if pair.endswith("JPY") else 0.0001
    one_sigma = daily_vol * math.sqrt(horizon)
    stop_pct = min(max(STOP_K * one_sigma, 0.002), 0.05)
    if pair.startswith("USD_"):                       # units are US dollars (USD/JPY)
        units = risk_dollars / stop_pct
        dollars = units
    else:                                              # units of EUR or GBP priced in dollars
        units = risk_dollars / (price * stop_pct)
        dollars = units * price
    capped = dollars > max_position_dollars
    if capped:
        units *= max_position_dollars / dollars
        dollars = max_position_dollars
    dist = price * stop_pct
    return {"expected_move_pct_1sigma": round(one_sigma * 100, 3), "stop_distance_pct": round(stop_pct * 100, 3),
            "stop_distance_pips": round(dist / pip, 1), "stop_price_if_buying": round(price - dist, 5),
            "stop_price_if_selling": round(price + dist, 5), "units": round(units, 2), "position_dollars": round(dollars, 2),
            "dollars_lost_if_stopped": round(dollars * stop_pct, 2), "capped_by_max_position": bool(capped)}


def fx_readout(pair: str, horizon: int = 5, risk_dollars: float = 5.0, max_position_dollars: float = 25.0) -> dict:
    from fetchers.oanda import load_bars
    from models.trading.fx.forecast import predict_vol_now
    from models.trading.shared.plain import risk_label
    bars = load_bars(pair)
    if bars.empty:
        return {"status": "insufficient_data", "symbol": pair, "detail": "No forex prices loaded yet. Run the OANDA download first (POST /trade/fx/backfill)."}
    f = predict_vol_now(bars, horizon, pair)
    if f.get("status") != "ok":
        return {"status": "insufficient_data", "symbol": pair, "detail": "Not enough forex history to forecast."}
    sz = fx_size(pair, f["last_price"], f["forecast_daily_vol"], f["horizon_days"], risk_dollars, max_position_dollars)
    label, why = risk_label(f["forecast_daily_vol"], "fx")
    return {"status": "ok", "kind": "fx", "symbol": pair, "as_of": f["as_of"], "horizon_days": f["horizon_days"], "last_price": round(f["last_price"], 5),
            "forecast_daily_swing_pct": round(f["forecast_daily_vol"] * 100, 3), "last_20_days_daily_swing_pct": round(f["trailing_daily_vol"] * 100, 3),
            "sizing": sz, "risk_label": label, "risk_text": why,
            "direction_note": "Predicta has no reliable call on whether this pair goes up or down. Tests on 15 years of prices found none. This only tells you how big the moves are likely to be.",
            "plain": (f"{why} A stop {sz['stop_distance_pips']} pips away ({sz['stop_distance_pct']}%) keeps a bad outcome near ${sz['dollars_lost_if_stopped']} "
                      f"on a ${sz['position_dollars']} position. Buying: stop at {sz['stop_price_if_buying']}. Selling: stop at {sz['stop_price_if_selling']}."
                      + (" The size was capped by your max-position limit." if sz["capped_by_max_position"] else ""))}
