"""
Automaton engine — the learning loop (2026-08-28).

User-directed design, explicit and deliberate: no kill switch, no cloning.
"If it can't make it work, kill itself; if the opposite, clone itself" is
the well-known "Automaton" AI-agent pattern (github.com/Conway-Research/
automaton — a continuously-running agent that pays for its own compute and
dies if it can't, or spins up funded child agents if it can). Asked
directly whether Predicta's version should work the same way, the user was
explicit: no kill switch ("it shouldn't be killed, it should learn") and no
cloning ("No clone"). What they actually want ported from that project is
its CONTINUOUS, UNSUPERVISED evaluate-and-adapt loop, not its
survival-or-death mechanic — a strategy that keeps running and gets better
at finding winners, never one that shuts itself off or spawns copies.

This module is that adaptation for Predicta: instead of a credit-balance
survival tier deciding whether the agent lives, a resolved-trade count
decides how much the SAME strategy trusts its own history yet. Below the
calibration floor, it runs on the same static SIGNAL_WEIGHTS every other
paper-trading engine in this codebase starts on. Once enough of its own
trades have closed, it reweights toward whatever combination of the 8 Low
Value signals has actually been winning — the "keep finding more of the
stock it needs" behavior the user asked for — using machinery that already
existed for Low Value (compute_low_value_dynamic_weights) and was "not yet
wired to SIGNAL_WEIGHTS" per LOW_VALUE_README.md. This is that wiring,
scoped to Automaton's own trade history via signal_calibration.py's engine
parameter (2026-08-28), never Low Value's.
"""
from __future__ import annotations
import time

from models.trading.low_value.thesis_tracker import SIGNAL_WEIGHTS

ENGINE: str = "automaton"

_WEIGHTS_CACHE: dict = {"weights": None, "computed_at": 0.0}
_WEIGHTS_CACHE_TTL_SEC: float = 300.0


def effective_weights() -> dict[str, float]:
    """
    thesis_tracker.SIGNAL_WEIGHTS (the same static starting guess every
    paper-trading engine in this codebase uses) unless enough Automaton
    trades have closed to unlock compute_low_value_dynamic_weights AND it
    actually found a real edge (status == "dynamic") — mirrors Low Value's
    own _effective_signal_weights() (thesis_tracker.py) exactly, scoped to
    engine="automaton" instead. Cached for _WEIGHTS_CACHE_TTL_SEC — this
    runs once per candidate per scan; recalibration doesn't change
    moment-to-moment.
    """
    now = time.monotonic()
    cached = _WEIGHTS_CACHE
    if cached["weights"] is not None and (now - cached["computed_at"]) < _WEIGHTS_CACHE_TTL_SEC:
        return cached["weights"]

    weights = dict(SIGNAL_WEIGHTS)
    try:
        from models.trading.shared.signal_calibration import (
            low_value_calibration_readiness, compute_low_value_dynamic_weights,
        )
        if low_value_calibration_readiness(engine=ENGINE).get("dynamic_weights_ready"):
            result = compute_low_value_dynamic_weights(min_trades=30, engine=ENGINE)
            if result and result.get("status") == "dynamic":
                weights = dict(result["weights"])
    except Exception:
        weights = dict(SIGNAL_WEIGHTS)

    cached["weights"] = weights
    cached["computed_at"] = now
    return weights


def learning_summary() -> dict:
    """
    Everything the dashboard/API needs to answer "has it actually learned
    anything yet, and from what": calibration readiness tiers (20/50/100
    closed trades, same discipline as every other engine in this codebase),
    per-thesis-type win rates (which KIND of underrated-stock story is
    actually winning — directly answers "keep finding more of the stock it
    needs"), and the currently-active weights vs. the static starting guess.
    """
    from models.trading.shared.signal_calibration import (
        low_value_calibration_readiness, thesis_type_calibration_report,
        low_value_per_signal_accuracy_report,
    )

    readiness = low_value_calibration_readiness(engine=ENGINE)
    weights = effective_weights()
    is_learned = weights != SIGNAL_WEIGHTS

    n_total = readiness["total_closed_trades"]
    if n_total == 0:
        note = "No closed Automaton trades yet — running on the same static starting weights every engine in this codebase begins with."
    elif not readiness["dynamic_weights_ready"]:
        remaining = max(0, 50 - n_total)
        note = (
            f"{n_total} closed trade(s) so far — still running on static starting weights. "
            f"{remaining} more needed before it can start reweighting toward what's actually winning."
        )
    elif is_learned:
        note = f"Learning is active — weights below are recomputed from {n_total} closed trades' real win rates, not the static starting guess."
    else:
        note = f"{n_total} closed trades — enough to attempt reweighting, but no signal showed a real edge yet, so it's still on the static starting weights."

    return {
        "engine": ENGINE,
        "readiness": readiness,
        "current_weights": weights,
        "static_starting_weights": dict(SIGNAL_WEIGHTS),
        "is_learned": is_learned,
        "note": note,
        "by_thesis_type": thesis_type_calibration_report(engine=ENGINE),
        "by_signal": low_value_per_signal_accuracy_report(engine=ENGINE),
    }
