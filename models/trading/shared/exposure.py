"""
Cross-engine portfolio exposure — visibility only (Tier 2 of the
2026-07-16/17 trading-model audit; see CODEMAP.md).

Each engine caps its OWN concurrent positions independently — High Value at
MAX_CONCURRENT_POSITIONS (3), Low Value at LOW_VALUE_MAX_CONCURRENT_POSITIONS
(3), Pairs at no cap at all — but nothing anywhere adds them up. A user could
be in High Value's 3 + Low Value's 3 + N pair trades simultaneously, all with
correlated exposure (e.g. the same sector selling off across every engine at
once), and no part of the system would treat that as one combined risk.

This is deliberately a READ-ONLY aggregator, not a gate. Unlike the per-
engine caps (which are also just guesses, but at least long-standing ones),
there is no validated "safe combined position count" for this codebase —
inventing one now would be exactly the kind of unvalidated-threshold guess
the rest of this audit has been trying to get away from. Report first,
consider a real gate only once there's a reason (a real correlated drawdown)
to believe a specific number is right — same diagnose-before-gate sequencing
already used for the Dalio macro overlay when it was first added.
"""
from __future__ import annotations

from db.database import get_db


def get_cross_engine_exposure() -> dict:
    """
    Open positions for EVERY engine (High Value, Low Value, Automaton, Pairs),
    counting both suggested (hypothetical) and real paper positions, with
    dollar amounts and same-stock overlaps.

    Updated 2026-11-01 (Kimi review): the old version skipped Automaton and
    real positions and reported counts only. Dollars = qty x entry price, only
    where qty is recorded (suggestions logged without a size show 0 dollars,
    reported separately as `unsized`). Pairs still has no dollar column.
    """
    from fetchers.high_value_runner import MAX_CONCURRENT_POSITIONS
    from models.trading.shared.kelly import LOW_VALUE_MAX_CONCURRENT_POSITIONS

    engines = ("high_value", "low_value", "automaton")
    out: dict = {}
    by_symbol: dict[str, dict] = {}
    with get_db() as conn:
        rows = conn.execute(
            "SELECT engine, symbol, side, qty, entry_price, is_hypothetical "
            "FROM intraday_trades WHERE exit_time IS NULL"
        ).fetchall()
        pairs = conn.execute(
            "SELECT COUNT(*) FROM pair_signals WHERE exit_time IS NULL AND action != 'NONE'"
        ).fetchone()[0]

    for e in engines:
        out[e] = {"open": 0, "real": 0, "suggested": 0, "long_dollars": 0.0,
                  "short_dollars": 0.0, "unsized": 0}
    for r in rows:
        e = r["engine"]
        if e not in out:
            continue
        o = out[e]
        o["open"] += 1
        o["suggested" if r["is_hypothetical"] else "real"] += 1
        qty, px = r["qty"] or 0, r["entry_price"] or 0
        dollars = qty * px
        if dollars <= 0:
            o["unsized"] += 1
        elif r["side"] == "short":
            o["short_dollars"] += dollars
        else:
            o["long_dollars"] += dollars
        sym = by_symbol.setdefault(r["symbol"], {"engines": set(), "dollars": 0.0})
        sym["engines"].add(e)
        sym["dollars"] += dollars

    for o in out.values():
        o["long_dollars"] = round(o["long_dollars"], 2)
        o["short_dollars"] = round(o["short_dollars"], 2)
        o["gross_dollars"] = round(o["long_dollars"] + o["short_dollars"], 2)

    overlaps = [
        {"symbol": s, "engines": sorted(v["engines"]), "dollars": round(v["dollars"], 2)}
        for s, v in by_symbol.items() if len(v["engines"]) > 1
    ]
    gross = round(sum(o["gross_dollars"] for o in out.values()), 2)
    total = sum(o["open"] for o in out.values()) + pairs
    biggest = max(by_symbol.items(), key=lambda kv: kv[1]["dollars"], default=None)
    return {
        # legacy keys (still read by the dashboards)
        "high_value_open": out["high_value"]["open"],
        "high_value_cap": MAX_CONCURRENT_POSITIONS,
        "low_value_open": out["low_value"]["open"],
        "low_value_cap": LOW_VALUE_MAX_CONCURRENT_POSITIONS,
        "pairs_open": pairs,
        "pairs_cap": None,
        "total_open": total,
        # new
        "automaton_open": out["automaton"]["open"],
        "engines": out,
        "gross_dollars": gross,
        "same_stock_overlaps": overlaps,
        "largest_symbol": (
            {"symbol": biggest[0], "dollars": round(biggest[1]["dollars"], 2),
             "pct_of_gross": round(biggest[1]["dollars"] / gross * 100, 1) if gross else None}
            if biggest else None
        ),
        "note": (
            "Visibility only, no combined limit is enforced. Counts include both "
            "suggested and real paper positions. Dollars = shares x entry price where "
            "a size was recorded. Pairs has no dollar size. Two engines holding the "
            "same stock is one bet, see same_stock_overlaps."
        ),
    }
