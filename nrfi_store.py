"""
nrfi_store.py

Read + aggregate helpers over the committed daily NRFI prediction files
(data/nrfi_predictions/YYYY-MM-DD.json), which are the persistent, auditable
record produced by scripts/daily_nrfi.py and committed to git by the
nrfi_daily GitHub Actions workflow.

These files — not the ephemeral CI database — are the source of truth for the
public /v1 API, because they live in git history (timestamped, append-only)
and survive redeploys.
"""
from __future__ import annotations
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

_REPO    = Path(__file__).resolve().parent
PRED_DIR = _REPO / "data" / "nrfi_predictions"

_BET_VERDICTS = ("BET", "LEAN")


def list_dates() -> list[str]:
    """All dates with a committed prediction file, oldest → newest."""
    if not PRED_DIR.exists():
        return []
    dates = []
    for f in PRED_DIR.glob("*.json"):
        stem = f.stem
        try:
            datetime.strptime(stem, "%Y-%m-%d")
            dates.append(stem)
        except ValueError:
            continue
    return sorted(dates)


def latest_date() -> Optional[str]:
    d = list_dates()
    return d[-1] if d else None


def load_date(game_date: str) -> list[dict]:
    """Load one date's prediction records; [] if the file is missing."""
    f = PRED_DIR / f"{game_date}.json"
    if not f.exists():
        return []
    try:
        with open(f) as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return []


def _recent_dates(days: int) -> list[str]:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    return [d for d in list_dates() if d >= cutoff]


def _plays(records: list[dict]) -> list[dict]:
    return [r for r in records
            if r.get("verdict") in _BET_VERDICTS and not r.get("error")]


def aggregate_performance(days: int = 3650) -> dict:
    """
    Win/loss + ROI + CLV across resolved BET/LEAN plays in the recent window.
    ROI assumes -110 juice (win = +0.909u, loss = -1.0u).
    """
    import math

    all_plays: list[dict] = []
    for d in _recent_dates(days):
        all_plays += _plays(load_date(d))

    resolved = [p for p in all_plays if p.get("won") in (0, 1)]
    wins   = sum(1 for p in resolved if p.get("won") == 1)
    losses = len(resolved) - wins
    pnl    = sum(p.get("pnl_units") or 0 for p in resolved)
    wr     = (wins / len(resolved)) if resolved else None
    roi    = (pnl / len(resolved) * 100) if resolved else None

    p_value = None
    ci_95 = None
    if resolved and len(resolved) > 1 and wr is not None:
        se    = math.sqrt(wr * (1 - wr) / len(resolved))
        ci_95 = [round(max(0.0, wr - 1.96 * se) * 100, 1),
                 round(min(1.0, wr + 1.96 * se) * 100, 1)]
        z     = (wr - 0.524) / math.sqrt(0.524 * 0.476 / len(resolved))
        try:
            import scipy.stats as _st
            p_value = round(float(1 - _st.norm.cdf(z)), 4)
        except Exception:
            p_value = None

    clv = aggregate_clv(days)

    if p_value is not None and p_value < 0.05 and (roi or 0) > 3:
        verdict = "EDGE PROVEN"
    elif clv.get("avg_clv_pp") is not None and clv["avg_clv_pp"] > 0 and clv.get("n", 0) >= 20:
        verdict = "BEATING THE CLOSE"
    elif wr is not None and wr > 0.524:
        verdict = "EDGE EXISTS"
    elif len(resolved) < 50:
        verdict = "TOO EARLY"
    else:
        verdict = "NO EDGE DETECTED"

    # Honesty guard: an explicit, state-aware disclaimer travels with every
    # performance response so the API can never be quoted as a profit claim
    # before the live edge is actually proven (Kimi's "don't oversell" point).
    if len(resolved) == 0:
        disclaimer = ("No resolved live predictions yet. Historical walk-forward "
                      "validation only (2022–2025: 54.9% at the 55% threshold, "
                      "p=0.0049). The live edge is NOT yet proven — this scorecard "
                      "is being built in public, starting now.")
    elif verdict in ("TOO EARLY", "EDGE EXISTS"):
        disclaimer = (f"{len(resolved)} live predictions resolved — too few to "
                      "prove an edge. Treat these as an accumulating track record, "
                      "not a profit claim.")
    else:
        disclaimer = ("Live results shown. Past performance does not guarantee "
                      "future results; variance is real over small samples.")

    return {
        "verdict":      verdict,
        "n_plays":      len(all_plays),
        "n_resolved":   len(resolved),
        "wins":         wins,
        "losses":       losses,
        "win_rate_pct": round(wr * 100, 1) if wr is not None else None,
        "roi_pct":      round(roi, 2) if roi is not None else None,
        "pnl_units":    round(pnl, 3),
        "ci_95":        ci_95,
        "p_value":      p_value,
        "clv":          clv,
        "window_days":  days,
        "disclaimer":   disclaimer,
    }


def aggregate_clv(days: int = 3650) -> dict:
    """
    Closing Line Value summary across plays that have both entry + closing lines.
    CLV accumulates every game (independent of outcome), so it's the fastest
    signal of edge and the number sharp buyers ask for.

    Validity guard (Kimi's caveat): CLV only means "we led the market" if the
    entry line was captured well before first pitch. Entries flagged
    entry_stale=True (captured < MIN_ENTRY_LEAD_HOURS before first pitch) are
    excluded from the headline number and reported separately, so a late/stale
    entry can't inflate the metric.
    """
    clv_plays: list[dict] = []
    for d in _recent_dates(days):
        for p in _plays(load_date(d)):
            if p.get("clv_pp") is not None:
                clv_plays.append(p)

    if not clv_plays:
        return {"n": 0, "avg_clv_pp": None, "beat_close_pct": None,
                "message": "No closing-line data yet. Set ODDS_API_KEY and run the "
                           "capture-odds step so CLV can accumulate."}

    # Headline CLV uses only entries captured early enough to be valid.
    valid  = [p for p in clv_plays if not p.get("entry_stale")]
    stale  = [p for p in clv_plays if p.get("entry_stale")]
    scored = valid or clv_plays  # fall back if no lead-time info recorded

    beat = sum(1 for p in scored if p.get("beat_close") == 1)
    avg  = sum(p["clv_pp"] for p in scored) / len(scored)

    leads = [p["entry_hours_to_fp"] for p in scored
             if p.get("entry_hours_to_fp") is not None]
    avg_lead = round(sum(leads) / len(leads), 1) if leads else None

    return {
        "n":                  len(scored),
        "avg_clv_pp":         round(avg, 3),
        "beat_close":         beat,
        "beat_close_pct":     round(beat / len(scored) * 100, 1),
        "avg_entry_lead_hrs": avg_lead,
        "n_stale_excluded":   len(stale),
        "note": ("Headline CLV uses only entry lines captured >= 3h before first "
                 "pitch, so it measures leading the market, not moving with it."),
    }
