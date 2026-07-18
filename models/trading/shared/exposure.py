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
    Open-position counts for every engine, from their respective tables —
    High Value and Low Value share intraday_trades (tagged by `engine`);
    Pairs logs to its own pair_signals table with no engine tag and no
    dollar-size column, so this reports position COUNTS, not dollar
    exposure — the three engines size positions too differently (ATR-based
    for High Value, fixed $25 for Low Value, untracked for Pairs) for a
    dollar total to mean anything correct yet.
    """
    from fetchers.high_value_runner import MAX_CONCURRENT_POSITIONS
    from models.trading.shared.kelly import LOW_VALUE_MAX_CONCURRENT_POSITIONS

    with get_db() as conn:
        hv = conn.execute(
            "SELECT COUNT(*) FROM intraday_trades "
            "WHERE engine='high_value' AND is_hypothetical=1 AND exit_time IS NULL"
        ).fetchone()[0]
        lv = conn.execute(
            "SELECT COUNT(*) FROM intraday_trades "
            "WHERE engine='low_value' AND is_hypothetical=1 AND exit_time IS NULL"
        ).fetchone()[0]
        pairs = conn.execute(
            "SELECT COUNT(*) FROM pair_signals "
            "WHERE exit_time IS NULL AND action != 'NONE'"
        ).fetchone()[0]

    total = hv + lv + pairs
    return {
        "high_value_open": hv,
        "high_value_cap": MAX_CONCURRENT_POSITIONS,
        "low_value_open": lv,
        "low_value_cap": LOW_VALUE_MAX_CONCURRENT_POSITIONS,
        "pairs_open": pairs,
        "pairs_cap": None,
        "total_open": total,
        "note": (
            "Visibility only — no combined limit is enforced; each engine still "
            "gates its own concurrent positions independently. Reports position "
            "counts, not dollar exposure, since High Value (ATR-sized), Low Value "
            "(fixed $25), and Pairs (untracked size) aren't comparable in dollars yet."
        ),
    }
