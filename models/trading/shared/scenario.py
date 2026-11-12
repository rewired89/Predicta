"""
"What could happen" simulator (read-only, a what-if, NOT a prediction of direction).

It replays the next N days thousands of times using the stock's expected daily swing, with
no built-in lean up or down (we have no proven way to know direction), and counts how often a
stop or a profit target would have been hit. Fat tails are used on purpose: real markets have
more big days than a bell curve says.
"""
import math

import numpy as np


def simulate(daily_vol: float, days: int, side: str = "long", stop_pct: float = 0.1, target_pct: float = 0.1,
             position_dollars: float = 25.0, n: int = 4000, seed: int = 7) -> dict:
    days = max(1, int(days))
    rnd = np.random.default_rng(seed)
    df = 4.0
    z = rnd.standard_t(df, size=(n, days)) * math.sqrt((df - 2) / df)      # fat tails, unit variance
    log_r = z * daily_vol - 0.5 * daily_vol ** 2                           # zero expected drift
    path = np.exp(np.cumsum(log_r, axis=1)) - 1.0                          # return vs entry at each day
    if side == "short":
        path = -path                                                       # a short gains when the price falls
    hit_stop = (path <= -stop_pct).any(axis=1)
    hit_tgt = (path >= target_pct).any(axis=1)
    first_stop = np.where(hit_stop, (path <= -stop_pct).argmax(axis=1), days + 1)
    first_tgt = np.where(hit_tgt, (path >= target_pct).argmax(axis=1), days + 1)
    stop_first = hit_stop & (first_stop <= first_tgt)
    tgt_first = hit_tgt & (first_tgt < first_stop)
    end = path[:, -1]
    q = np.percentile(end, [5, 25, 50, 75, 95]) * 100
    return {
        "days": days, "side": side,
        "chance_stop_hit_first_pct": round(float(stop_first.mean()) * 100, 1),
        "chance_target_hit_first_pct": round(float(tgt_first.mean()) * 100, 1),
        "chance_neither_pct": round(float((~stop_first & ~tgt_first).mean()) * 100, 1),
        "ending_return_pct": {"worst_5pct": round(float(q[0]), 1), "bad_25pct": round(float(q[1]), 1), "middle": round(float(q[2]), 1),
                              "good_25pct": round(float(q[3]), 1), "best_5pct": round(float(q[4]), 1)},
        "typical_range_dollars": [round(float(q[1]) / 100 * position_dollars, 2), round(float(q[3]) / 100 * position_dollars, 2)],
        "note": "A what-if from price swings only. It has no opinion on direction (zero average return). Over long periods the middle case sits a little below zero because a loss hurts more than an equal gain helps.",
    }


def _m(x: float) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


def plain(sim: dict, position_dollars: float) -> str:
    e = sim["ending_return_pct"]
    return (f"I replayed the next {sim['days']} days thousands of times. In about half of them, a ${position_dollars:,.0f} position ends "
            f"between {e['bad_25pct']:+.1f}% and {e['good_25pct']:+.1f}% (roughly {_m(sim['typical_range_dollars'][0])} to {_m(sim['typical_range_dollars'][1])}). "
            f"In a bad case (1 in 20) it is down {abs(e['worst_5pct']):.1f}% or worse. "
            f"The stop gets hit first about {sim['chance_stop_hit_first_pct']:.0f}% of the time and the profit target about {sim['chance_target_hit_first_pct']:.0f}%. "
            f"This shows the range of what could happen, not which way it will go.")
