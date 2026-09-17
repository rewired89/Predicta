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
trades have closed, it reweights toward whatever combination of the Low
Value signals (9 as of 2026-09-11 — see thesis_tracker.SIGNAL_WEIGHTS) has
actually been winning — the "keep finding more of the
stock it needs" behavior the user asked for — using machinery that already
existed for Low Value (compute_low_value_dynamic_weights) and was "not yet
wired to SIGNAL_WEIGHTS" per LOW_VALUE_README.md. This is that wiring,
scoped to Automaton's own trade history via signal_calibration.py's engine
parameter (2026-08-28), never Low Value's.
"""
from __future__ import annotations
import time

from models.trading.low_value.thesis_tracker import SIGNAL_WEIGHTS, ENTRY_THRESHOLD

ENGINE: str = "automaton"

_WEIGHTS_CACHE: dict = {"weights": None, "computed_at": 0.0}
_WEIGHTS_CACHE_TTL_SEC: float = 300.0

# Reset lever (added 2026-08-29, per external AI review — a real gap
# DeepSeek's feedback flagged: since weights are computed live from trade
# history rather than stored, there was no way to say "go back to the
# static starting weights" if the first batch of learned weights looks bad,
# short of a code change. This is NOT a kill switch on the strategy — it
# has no effect on scanning, entries, or exits, which keep running exactly
# as before, honoring the user's explicit "it shouldn't be killed" design.
# It only controls whether effective_weights() is allowed to deviate from
# the static table. Flip to False and the very next cache refresh (within
# _WEIGHTS_CACHE_TTL_SEC) reverts every future scan to the untouched
# starting weights — instant, no DB cleanup needed, because nothing about
# a "learned" state was ever persisted in the first place.
AUTOMATON_LEARNING_ENABLED: bool = True

# Sprint-mode training-data exclusion (added 2026-08-29, per external AI
# review — DeepSeek's P0 finding): Automaton's sprint mode logs/executes
# trades down to |score|=15 to accelerate data collection (see
# fetchers/automaton_runner.py's AUTOMATON_SPRINT_MIN_SCORE), but the real
# entry bar is ENTRY_THRESHOLD (40). Those sub-threshold trades are real,
# honest data — they still count toward readiness/progress tiers below and
# the per-thesis-type/per-signal diagnostic reads — but weight CALIBRATION
# specifically excludes them: fitting weights on trades the live strategy
# would never actually take risks converging on a set that looks good on
# weak signals without ever being tested against the real bar. See
# compute_low_value_dynamic_weights's min_entry_score param.
_WEIGHT_CALIBRATION_MIN_ENTRY_SCORE: float = ENTRY_THRESHOLD


def effective_weights() -> dict[str, float]:
    """
    thesis_tracker.SIGNAL_WEIGHTS (the same static starting guess every
    paper-trading engine in this codebase uses) unless AUTOMATON_LEARNING_
    ENABLED is True AND enough of Automaton's own REAL-ENTRY-BAR trades
    (|score| >= ENTRY_THRESHOLD, sprint-mode trades excluded — see
    _WEIGHT_CALIBRATION_MIN_ENTRY_SCORE above) have closed to unlock
    compute_low_value_dynamic_weights AND it actually found a real edge
    (status == "dynamic") — mirrors Low Value's own _effective_signal_
    weights() (thesis_tracker.py) exactly, scoped to engine="automaton"
    instead. Cached for _WEIGHTS_CACHE_TTL_SEC — this runs once per
    candidate per scan; recalibration doesn't change moment-to-moment.
    """
    now = time.monotonic()
    cached = _WEIGHTS_CACHE
    if cached["weights"] is not None and (now - cached["computed_at"]) < _WEIGHTS_CACHE_TTL_SEC:
        return cached["weights"]

    weights = dict(SIGNAL_WEIGHTS)
    if AUTOMATON_LEARNING_ENABLED:
        try:
            from models.trading.shared.signal_calibration import compute_low_value_dynamic_weights
            result = compute_low_value_dynamic_weights(
                min_trades=30, engine=ENGINE, min_entry_score=_WEIGHT_CALIBRATION_MIN_ENTRY_SCORE,
            )
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
        low_value_per_signal_accuracy_report, _load_closed_low_value_trades,
    )

    readiness = low_value_calibration_readiness(engine=ENGINE)
    weights = effective_weights()
    is_learned = weights != SIGNAL_WEIGHTS

    n_total = readiness["total_closed_trades"]
    # Real-entry-bar count specifically (sprint-mode trades excluded) — the
    # number that actually gates weight calibration, distinct from n_total
    # above which counts every closed trade including sprint ones. See
    # _WEIGHT_CALIBRATION_MIN_ENTRY_SCORE's docstring.
    n_qualifying = len(_load_closed_low_value_trades(engine=ENGINE, min_entry_score=_WEIGHT_CALIBRATION_MIN_ENTRY_SCORE))

    if not AUTOMATON_LEARNING_ENABLED:
        note = (
            f"Learning is manually disabled (AUTOMATON_LEARNING_ENABLED=False) — running on the static "
            f"starting weights regardless of trade history. Trading itself is unaffected; this only resets "
            f"what the composite has learned. {n_total} closed trade(s) on record ({n_qualifying} at the real entry bar)."
        )
    elif n_total == 0:
        note = "No closed Automaton trades yet — running on the same static starting weights every engine in this codebase begins with."
    elif n_qualifying < 30:
        remaining = max(0, 30 - n_qualifying)
        note = (
            f"{n_total} closed trade(s) so far, but only {n_qualifying} at the real entry bar "
            f"(|score| >= {ENTRY_THRESHOLD:.0f} — sprint-mode trades below that don't count toward weight "
            f"calibration). {remaining} more real-bar trades needed before it can start reweighting."
        )
    elif is_learned:
        note = f"Learning is active — weights below are recomputed from {n_qualifying} real-entry-bar trades' actual win rates, not the static starting guess."
    else:
        note = f"{n_qualifying} real-entry-bar trades closed — enough to attempt reweighting, but no signal showed a real edge yet, so it's still on the static starting weights."

    return {
        "engine": ENGINE,
        "learning_enabled": AUTOMATON_LEARNING_ENABLED,
        "readiness": readiness,
        "n_closed_total": n_total,
        "n_closed_qualifying_real_bar": n_qualifying,
        "weight_calibration_min_entry_score": _WEIGHT_CALIBRATION_MIN_ENTRY_SCORE,
        "current_weights": weights,
        "static_starting_weights": dict(SIGNAL_WEIGHTS),
        "is_learned": is_learned,
        "note": note,
        "by_thesis_type": thesis_type_calibration_report(engine=ENGINE),
        "by_signal": low_value_per_signal_accuracy_report(engine=ENGINE),
    }
