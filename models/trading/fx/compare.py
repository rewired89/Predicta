"""Side-by-side: does adding interest-rate gaps (carry) or macro/risk data beat price history alone?"""
from fetchers import fx_macro
from models.trading.fx import forecast as F

KEEP = ("accuracy", "accuracy_95ci", "always_pick_the_commoner_side_accuracy", "mean_net_return_pct_per_trade",
        "net_return_95ci_pct", "test_periods", "reached_80_percent", "verdict")


def _short(r: dict) -> dict:
    return {k: r.get(k) for k in KEEP}


def compare(bars, pair: str, h: int = 5, loader=fx_macro.load_series) -> dict:
    base = F.build(bars, h)
    out = {"pair": pair, "horizon_days": h, "price_only": _short(F.run_direction(bars, h, pair, frame=base))}
    missing = []
    for level, key in (("carry", "price_plus_carry"), ("macro", "price_plus_carry_and_macro")):
        frame, feats, miss = fx_macro.attach(base, pair, level, loader)
        missing = sorted(set(missing) | set(miss))
        if not any(f in feats for f in ("carry",)):
            out[key] = {"verdict": "NO DATA (run POST /trade/fx/macro-backfill)"}
            continue
        out[key] = _short(F.run_direction(bars, h, pair, frame=frame, feats=F.FEATURES + feats))
        out[key]["extra_features"] = feats
        if level == "carry":
            out["carry_trade_no_model"] = F.carry_trade_baseline(frame, h)
    out["missing_series"] = missing
    best = max((out[k].get("accuracy") or 0) for k in ("price_only", "price_plus_carry", "price_plus_carry_and_macro") if isinstance(out.get(k), dict))
    out["best_accuracy_any_version"] = round(best, 3)
    out["reached_80_percent"] = best >= 0.80
    return F.clean(out)
