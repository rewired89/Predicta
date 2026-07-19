"""
End-to-end baseball analysis pipeline.
query → MLB Stats API → Poisson run model → Elo blend → markets → narrative → result dict
"""
from __future__ import annotations
import env_loader  # noqa: F401 — loads .env on import
import math
import traceback
from datetime import datetime, timezone
from typing import Optional

from db.database import get_db, init_db
from fetchers.baseball import fetch_baseball_context, LEAGUE_AVG_FIP
from fetchers.savant import enrich_starter, enrich_team_hitting
from fetchers.schedule import fetch_team_fatigue
from fetchers.signals import log_signal
from fetchers.weather import fetch_game_weather, weather_to_signals, team_to_stadium_code
from models.baseball_market import (
    expected_runs_split, compute_baseball_markets,
    platoon_wrc_adjust, LEAGUE_BULLPEN_FIP,
)
from models.elo import EloModel
from models.kelly import kelly_stake, american_to_decimal, market_edge_summary
from models.nrfi_model import predict_nrfi, model_available as nrfi_model_available


def _log_prediction(result: dict, game_date: str, home_team: str,
                     away_team: str, home_starter: dict,
                     away_starter: dict, bankroll: float) -> None:
    """
    Persist every prediction to the nrfi_bets table so live performance can be
    tracked and compared to real outcomes later — this fires for BOTH the
    automated daily pipeline AND ad-hoc manual website queries, so a one-off
    "Yankees vs Red Sox tonight" query is graded the same as a scheduled game.

    Captures NRFI (required — only logs if the NRFI market is available) plus
    moneyline/F5/O-U picks when present, and game_pk so resolve_pending_bets()
    (scripts/daily_nrfi.py) can fetch the real linescore later and grade all
    four markets, not just NRFI. Before this, moneyline/F5/O-U picks from
    manual queries were computed, shown once, and never persisted anywhere.
    """
    try:
        bet_recs = result.get("bet_recommendations", [])
        nrfi_rec = next((r for r in bet_recs if r.get("market") == "NRFI"), None)
        if nrfi_rec is None:
            return
        nrfi_market = result.get("markets", {}).get("nrfi", {})
        opts = nrfi_market.get("options", [])
        if not opts:
            return
        p_nrfi = opts[0].get("prob", 50.0)
        kelly_d = nrfi_rec.get("kelly") or {}

        ml_rec = next((r for r in bet_recs if r.get("market") == "Full Game"), {})
        f5_rec = next((r for r in bet_recs if r.get("market") == "F5"), {})
        ou_rec = next((r for r in bet_recs if r.get("market") == "Game Total"), {})

        with get_db() as db:
            db.execute(
                """
                INSERT INTO nrfi_bets
                  (game_date, home_team, away_team, home_starter, away_starter,
                   p_nrfi, verdict, confidence,
                   kelly_full_pct, kelly_half_pct, recommended_stake, bankroll,
                   game_pk, ml_pick, ml_prob, ml_verdict,
                   f5_pick, f5_prob, f5_verdict,
                   ou_pick, ou_prob, ou_verdict, ou_line)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    game_date, home_team, away_team,
                    home_starter.get("name"), away_starter.get("name"),
                    round(p_nrfi, 2),
                    nrfi_rec.get("verdict", "SKIP"),
                    nrfi_rec.get("confidence"),
                    kelly_d.get("full_kelly_pct"),
                    kelly_d.get("half_kelly_pct"),
                    kelly_d.get("recommended_stake"),
                    bankroll,
                    result.get("game_pk"),
                    ml_rec.get("bet"), ml_rec.get("model_prob"), ml_rec.get("verdict"),
                    f5_rec.get("bet"), f5_rec.get("model_prob"), f5_rec.get("verdict"),
                    ou_rec.get("bet"), ou_rec.get("model_prob"), ou_rec.get("verdict"),
                    ou_rec.get("line"),
                ),
            )
    except Exception:
        pass   # never let logging break the main prediction flow


def _elo_from_winpct(win_pct: float) -> float:
    """Seed Elo from current-season win% so the blend uses real team strength."""
    wp = max(0.01, min(0.99, win_pct))
    return 1500.0 - 400.0 * math.log10((1 - wp) / wp)


ELO_WINPCT_SHRINKAGE_K = 20.0  # games needed for raw win% to earn half-weight vs .500 (mirrors models/rugby_model.py's SHRINKAGE_K convention)


def _shrink_win_pct(win_pct: float, games_played: float) -> float:
    """
    Regress win% toward league-average .500 by sample size before it feeds
    _elo_from_winpct. The Elo leg gets 40% weight in the blend (see
    run_baseball_analysis) but was previously gated only on games_played >= 10 —
    still thin enough that an early hot/cold streak (e.g. 8-2) swung 40% of the
    final probability on essentially small-sample noise.
    """
    if games_played <= 0:
        return 0.5
    w = games_played / (games_played + ELO_WINPCT_SHRINKAGE_K)
    return w * win_pct + (1 - w) * 0.5


def _derive_bullpen_fip(team_era: float, starter_fip: float,
                         starter_avg_ip: Optional[float] = None,
                         starter_era: Optional[float] = None) -> float:
    """
    Estimate bullpen quality from team ERA and starter stats.

    ERA decomposition (consistent units throughout):
      bullpen_ERA = (team_ERA × 9 - starter_ERA × avg_ip) / (9 - avg_ip)

    starter_ERA is used instead of starter_FIP because team_ERA measures actual
    runs allowed (including defense), and decomposition requires both sides to
    be in ERA units. starter_FIP is defense-independent and mixes unit systems.
    Falls back to starter_FIP when starter_ERA is unavailable.

    Fallback when avg_ip unknown:
      (team_ERA × 9 - starter_rate × 5) / 4  (classic 5/4 split)

    Clamped to [3.0, 7.5]; returns LEAGUE_BULLPEN_FIP when team_era is missing.
    """
    if not team_era or team_era <= 0:
        return LEAGUE_BULLPEN_FIP
    # Use ERA for decomposition (consistent units); fall back to FIP if ERA unavailable
    decomp_rate = starter_era if (starter_era and starter_era > 0) else starter_fip
    if starter_avg_ip and 0 < starter_avg_ip < 8.5:
        bullpen_innings = 9.0 - starter_avg_ip
        derived = (team_era * 9.0 - decomp_rate * starter_avg_ip) / bullpen_innings
    else:
        derived = (team_era * 9.0 - decomp_rate * 5.0) / 4.0
    return max(3.0, min(derived, 7.5))


def _baseball_data_confidence(
    ai_fallback: bool,
    starter_a: dict, starter_b: dict,
    hitting_a: dict, hitting_b: dict,
    record_a: dict, record_b: dict,
) -> str:
    """
    Data confidence for a baseball prediction, based on how much of the model's
    input is real live data vs defaults. Mirrors the other sports' pipelines.

      low     — ESPN unreachable (AI-estimated signals), OR both probable
                starters unknown/TBD (common for next-day games before lineups
                post). The model is running largely on league averages.
      medium  — one starter unknown/TBD, or missing team offense (wRC+), or a
                thin season sample (< 10 games played).
      high    — both starters named with FIP, real wRC+, and ≥ 10 games played
                for both teams.
    """
    if ai_fallback:
        return "low"

    def _starter_missing(s: dict) -> bool:
        name = (s.get("name") or "").strip().lower()
        return name in ("", "tbd", "unknown") or not s.get("fip")

    missing_starters = sum(_starter_missing(s) for s in (starter_a, starter_b))
    if missing_starters == 2:
        return "low"

    weak = missing_starters  # 0 or 1 at this point
    if not hitting_a.get("wrc_plus") or not hitting_b.get("wrc_plus"):
        weak += 1
    if (record_a.get("games_played") or 0) < 10 or (record_b.get("games_played") or 0) < 10:
        weak += 1

    return "high" if weak == 0 else "medium"


def _format_baseball_markets(markets: dict, team_home: str, team_away: str) -> dict:
    def pct(v):
        return round(float(v) * 100, 1)

    result: dict = {}

    ml = markets.get("moneyline", {})
    if ml:
        hp, ap = pct(ml["p_home_win"]), pct(ml["p_away_win"])
        result["moneyline"] = {
            "label": "Moneyline",
            "options": [
                {"label": team_home, "prob": hp, "best": hp >= ap},
                {"label": team_away, "prob": ap, "best": ap > hp},
            ],
        }

    rl = markets.get("run_line", [])
    if rl:
        result["run_line"] = {
            "label": "Run Line",
            "lines": [
                {
                    "label":         r["label"],
                    "p_home_covers": pct(r["p_home_covers"]),
                    "p_away_covers": pct(r["p_away_covers"]),
                    "p_push":        pct(r["p_push"]),
                }
                for r in rl
            ],
        }

    totals = markets.get("totals", [])
    if totals:
        result["totals"] = {
            "label": "Game Total (O/U)",
            "lines": [
                {
                    "line":      t["line"],
                    "label":     t["label"],
                    "p_over":    pct(t["p_over"]),
                    "p_under":   pct(t["p_under"]),
                    "best_side": "over" if t["p_over"] >= t["p_under"] else "under",
                }
                for t in totals
            ],
        }

    f5 = markets.get("first_five", {})
    if f5:
        hp, ap = pct(f5["p_home_win"]), pct(f5["p_away_win"])
        result["first_five"] = {
            "label": "First 5 Innings (Starter)",
            "options": [
                {"label": team_home, "prob": hp, "best": hp >= ap},
                {"label": team_away, "prob": ap, "best": ap > hp},
            ],
            "mu_home_f5": f5.get("mu_home_f5"),
            "mu_away_f5": f5.get("mu_away_f5"),
            "note": f5.get("note", ""),
        }

    l4 = markets.get("last_four", {})
    if l4:
        hp, ap = pct(l4["p_home_win"]), pct(l4["p_away_win"])
        result["last_four"] = {
            "label": "Innings 6–9 (Bullpen)",
            "options": [
                {"label": team_home, "prob": hp, "best": hp >= ap},
                {"label": team_away, "prob": ap, "best": ap > hp},
            ],
            "mu_home_l4": l4.get("mu_home_l4"),
            "mu_away_l4": l4.get("mu_away_l4"),
            "note": l4.get("note", ""),
        }

    nrfi = markets.get("nrfi", {})
    if nrfi:
        np_, yp = pct(nrfi["p_nrfi"]), pct(nrfi["p_yrfi"])
        result["nrfi"] = {
            "label": "NRFI / YRFI",
            "options": [
                {"label": "NRFI (No Run First Inning)", "prob": np_, "best": np_ >= yp},
                {"label": "YRFI (Yes Run First Inning)", "prob": yp, "best": yp > np_},
            ],
            "note": nrfi.get("note", ""),
        }

    tt_home = markets.get("team_total_home", [])
    tt_away = markets.get("team_total_away", [])
    if tt_home or tt_away:
        result["team_totals"] = {
            "label": "Team Totals",
            "home": {
                "team":  team_home,
                "mu":    markets.get("mu_home"),
                "lines": [{"line": t["line"], "p_over": pct(t["p_over"]),
                            "p_under": pct(t["p_under"])} for t in tt_home],
            },
            "away": {
                "team":  team_away,
                "mu":    markets.get("mu_away"),
                "lines": [{"line": t["line"], "p_over": pct(t["p_over"]),
                            "p_under": pct(t["p_under"])} for t in tt_away],
            },
        }

    return result


def _nrfi_kelly(p_nrfi_pct: float, bankroll: float = 1000.0,
                american_odds: float = -110.0) -> dict:
    """
    Kelly staking for an NRFI bet.

    Returns full_kelly_pct, half_kelly_pct, recommended_stake (half-Kelly),
    and breakeven probability for the given odds.
    """
    dec   = american_to_decimal(american_odds)
    b     = dec - 1.0              # profit per unit staked
    p     = p_nrfi_pct / 100.0
    q     = 1.0 - p
    full_k = max((b * p - q) / b, 0.0)
    half_k = full_k / 2.0
    breakeven = 1.0 / dec
    return {
        "full_kelly_pct":    round(full_k * 100, 2),
        "half_kelly_pct":    round(half_k * 100, 2),
        "recommended_stake": round(bankroll * half_k, 2),
        "breakeven_pct":     round(breakeven * 100, 1),
        "edge_pct":          round((p - breakeven) * 100, 1),
        "american_odds":     american_odds,
    }


def _bet_recommendations(
    formatted_markets: dict,
    home_starter: dict,
    away_starter: dict,
    team_home: str,
    team_away: str,
    bankroll: float = 1000.0,
    data_confidence: str = "high",
    market_implied_home: float | None = None,
    market_implied_away: float | None = None,
) -> list[dict]:
    """
    Explicit BET / LEAN / SKIP verdicts for NRFI, F5, and full-game moneyline.

    NRFI:      prob >= 55% + at least one starter with CSW% > 30% OR barrel% < 6.5%
    F5:        leading side >= 60% + SIERA/FIP gap between starters >= 1.0
    Full Game: leading side >= 65% + data_confidence != "low"

    Uses formatted_markets (probabilities already in %).
    """
    recs: list[dict] = []

    # ── NRFI ─────────────────────────────────────────────────────────────────
    nrfi = formatted_markets.get("nrfi", {})
    if nrfi:
        opts     = nrfi.get("options", [])
        p_nrfi   = opts[0]["prob"] if opts else 50.0
        p_yrfi   = opts[1]["prob"] if len(opts) > 1 else 50.0
        main_side = "NRFI" if p_nrfi >= p_yrfi else "YRFI"
        main_prob = max(p_nrfi, p_yrfi)

        # Starter quality gate — CSW% and barrel% from Savant
        elite: list[str] = []
        for label, s in ((team_home, home_starter), (team_away, away_starter)):
            name = s.get("name", label + " starter")
            csw  = s.get("csw_pct")
            brl  = s.get("barrel_pct_against")
            if csw is not None and csw > 0.300:
                elite.append(f"{name} CSW% {csw*100:.1f}% (elite, league avg 28.5%)")
            if brl is not None and brl < 0.065:
                elite.append(f"{name} barrel% {brl*100:.1f}% against (low, league avg 7.5%)")

        # 55% threshold gives ~100-150 games/season volume to validate edge faster.
        # Elite quality gate still applies for NRFI — requires at least one starter
        # signal (CSW% > 30% or barrel% < 6.5%) as a secondary confirmation.
        if main_side == "NRFI" and p_nrfi >= 55.0 and elite:
            verdict     = "BET"
            confidence  = "HIGH" if (p_nrfi >= 57.0 and len(elite) >= 2) else "MEDIUM"
            reasons     = [f"Model: {p_nrfi:.1f}% NRFI probability (threshold 55%)"] + elite
            skip_reason = None
        elif main_side == "NRFI" and p_nrfi >= 55.0 and not elite:
            verdict     = "LEAN"
            confidence  = "LOW"
            reasons     = [f"Model: {p_nrfi:.1f}% NRFI (threshold 55%) but no elite starter signal"]
            skip_reason = None
        elif main_side == "YRFI" and p_yrfi >= 55.0:
            verdict     = "BET"
            confidence  = "MEDIUM"
            reasons     = [f"Model: {p_yrfi:.1f}% YRFI — both starters expected to give up first-inning runs"]
            skip_reason = None
        else:
            verdict     = "SKIP"
            confidence  = None
            reasons     = []
            parts: list[str] = []
            if main_side == "NRFI" and p_nrfi < 55.0:
                parts.append(f"NRFI {p_nrfi:.1f}% below 55% threshold")
            if main_side == "NRFI" and not elite:
                parts.append("no elite starter signals (need CSW% > 30% or barrel% < 6.5%)")
            skip_reason = "; ".join(parts) or f"edge insufficient ({main_side} {main_prob:.1f}%)"

        nrfi_k = _nrfi_kelly(main_prob, bankroll) if verdict in ("BET", "LEAN") else None
        recs.append({
            "market":      "NRFI",
            "verdict":     verdict,
            "bet":         "NRFI Yes (No Run 1st Inning)" if main_side == "NRFI" else "YRFI Yes (Run 1st Inning)",
            "model_prob":  round(main_prob, 1),
            "threshold":   55.0,
            "confidence":  confidence,
            "reasons":     reasons,
            "skip_reason": skip_reason,
            "kelly":       nrfi_k,
        })

    # ── F5 ───────────────────────────────────────────────────────────────────
    f5 = formatted_markets.get("first_five", {})
    if f5:
        opts         = f5.get("options", [])
        home_f5_prob = opts[0]["prob"] if opts else 50.0
        away_f5_prob = opts[1]["prob"] if len(opts) > 1 else 50.0
        leading_side = "home" if home_f5_prob >= away_f5_prob else "away"
        leading_team = team_home if leading_side == "home" else team_away
        leading_prob = max(home_f5_prob, away_f5_prob)

        # Use SIERA when available; fall back to FIP
        home_q = home_starter.get("siera") or home_starter.get("fip") or 4.00
        away_q = away_starter.get("siera") or away_starter.get("fip") or 4.00
        home_src = "SIERA" if home_starter.get("siera") else "FIP"
        away_src = "SIERA" if away_starter.get("siera") else "FIP"

        if leading_side == "home":
            fav_q, fav_src, fav_name = home_q, home_src, home_starter.get("name", "Home starter")
            dog_q, dog_src, dog_name = away_q, away_src, away_starter.get("name", "Away starter")
        else:
            fav_q, fav_src, fav_name = away_q, away_src, away_starter.get("name", "Away starter")
            dog_q, dog_src, dog_name = home_q, home_src, home_starter.get("name", "Home starter")

        # Positive gap = favored team has better (lower) starter SIERA
        gap = dog_q - fav_q

        if leading_prob >= 62.0 and gap >= 1.0 and data_confidence != "low":
            verdict     = "BET"
            confidence  = "HIGH" if (leading_prob >= 67.0 and gap >= 1.5) else "MEDIUM"
            reasons     = [
                f"Model: {leading_prob:.1f}% F5 for {leading_team}",
                f"Starter gap: {fav_name} {fav_src} {fav_q:.2f} vs {dog_name} {dog_src} {dog_q:.2f} (gap {gap:.2f}, threshold 1.0)",
            ]
            skip_reason = None
        elif leading_prob >= 58.0:
            verdict     = "LEAN"
            confidence  = "LOW"
            reasons     = [
                f"Model: {leading_prob:.1f}% F5 for {leading_team} — starter gap {gap:.2f}",
            ]
            skip_reason = f"Below 62% F5 threshold or starter gap < 1.0 — soft lean only"
        else:
            verdict     = "SKIP"
            confidence  = None
            reasons     = []
            skip_reason = f"F5 probability {leading_prob:.1f}% below 58% threshold"

        recs.append({
            "market":      "F5",
            "verdict":     verdict,
            "bet":         f"{leading_team} to lead after 5 innings",
            "model_prob":  round(leading_prob, 1),
            "threshold":   60.0,
            "confidence":  confidence,
            "reasons":     reasons,
            "skip_reason": skip_reason,
        })

    # ── Full Game Moneyline ───────────────────────────────────────────────────
    ml = formatted_markets.get("moneyline", {})
    if ml:
        opts         = ml.get("options", [])
        home_ml_prob = opts[0]["prob"] if opts else 50.0
        away_ml_prob = opts[1]["prob"] if len(opts) > 1 else 50.0
        leading_side = "home" if home_ml_prob >= away_ml_prob else "away"
        leading_team = team_home if leading_side == "home" else team_away
        leading_prob = max(home_ml_prob, away_ml_prob)

        # Market sanity check: if we have real sportsbook odds, see if the
        # model disagrees with the market by > 20pp. If so, downgrade to
        # LEAN at best — wild disagreement usually means the model is wrong.
        market_disagree = False
        _disagree_note = ""
        if market_implied_home is not None and market_implied_away is not None:
            model_home_pct = home_ml_prob
            market_home_pct = market_implied_home * 100
            gap = abs(model_home_pct - market_home_pct)
            if gap > 20.0:
                market_disagree = True
                _disagree_note = (f"Model disagrees with market by {gap:.0f}pp "
                                  f"(model {model_home_pct:.0f}% vs market {market_home_pct:.0f}%) "
                                  f"— capped at LEAN")

        if leading_prob >= 65.0 and data_confidence != "low" and not market_disagree:
            verdict     = "BET"
            confidence  = "HIGH" if leading_prob >= 70.0 else "MEDIUM"
            reasons     = [f"Model: {leading_prob:.1f}% full-game edge for {leading_team}"]
            skip_reason = None
        elif leading_prob >= 58.0 and data_confidence != "low":
            verdict     = "LEAN"
            confidence  = "LOW"
            reasons     = [f"Model: {leading_prob:.1f}% — soft edge for {leading_team}"]
            if market_disagree:
                reasons.append(_disagree_note)
            skip_reason = "Below 65% full-game threshold — lean only, no bet"
        else:
            verdict     = "SKIP"
            confidence  = None
            reasons     = []
            parts = []
            if leading_prob < 58.0:
                parts.append(f"{leading_prob:.1f}% — coin-flip range, no edge")
            if data_confidence == "low":
                parts.append("data confidence low — insufficient real data for BET")
            skip_reason = "; ".join(parts) or f"{leading_prob:.1f}% — no edge"

        recs.append({
            "market":      "Full Game",
            "verdict":     verdict,
            "bet":         f"{leading_team} moneyline",
            "model_prob":  round(leading_prob, 1),
            "threshold":   65.0,
            "confidence":  confidence,
            "reasons":     reasons,
            "skip_reason": skip_reason,
        })

    # ── Game Total (Over/Under) ─────────────────────────────────────────────
    totals = formatted_markets.get("totals", {})
    if totals:
        total_lines = totals.get("lines", [])
        # Pick the line with the strongest edge (highest probability on either side)
        best_line = None
        best_prob = 0.0
        best_side = "over"
        for tl in total_lines:
            p_over  = tl.get("p_over", 50.0)
            p_under = tl.get("p_under", 50.0)
            top = max(p_over, p_under)
            if top > best_prob:
                best_prob = top
                best_side = "over" if p_over >= p_under else "under"
                best_line = tl

        if best_line:
            line_val = best_line["line"]
            if best_prob >= 62.0:
                verdict     = "BET"
                confidence  = "HIGH" if best_prob >= 68.0 else "MEDIUM"
                reasons     = [f"Model: {best_prob:.1f}% {best_side} {line_val} runs"]
                skip_reason = None
            elif best_prob >= 57.0:
                verdict     = "LEAN"
                confidence  = "LOW"
                reasons     = [f"Model: {best_prob:.1f}% {best_side} {line_val} — soft edge"]
                skip_reason = f"Below 62% O/U threshold — lean only"
            else:
                verdict     = "SKIP"
                confidence  = None
                reasons     = []
                skip_reason = f"O/U best line {best_prob:.1f}% — no strong edge"

            recs.append({
                "market":      "Game Total",
                "verdict":     verdict,
                "bet":         f"{best_side.title()} {line_val}",
                "model_prob":  round(best_prob, 1),
                "threshold":   62.0,
                "line":        line_val,
                "confidence":  confidence,
                "reasons":     reasons,
                "skip_reason": skip_reason,
            })

    return recs


def _build_plain_summary(
    team_home: str, team_away: str,
    prob_home: float, prob_away: float,
    mu_home: float, mu_away: float,
    mu_home_f5: float, mu_away_f5: float,
) -> str:
    """
    Plain-English bet recommendation in the user's preferred phrasing.

    Format:
      "<favourite> is gonna win vs <opponent> (XX% accuracy). Bet on: runs (over/under N.5),
       1st 5 innings <team>, NRFI."

    Markets are gated by Poisson run probabilities so we only list a market
    when the model actually likes it.
    """
    if prob_home >= prob_away:
        winner, loser, win_prob = team_home, team_away, prob_home
    else:
        winner, loser, win_prob = team_away, team_home, prob_away

    total_runs = mu_home + mu_away
    total_runs_f5 = mu_home_f5 + mu_away_f5

    # NRFI = no run in the 1st inning. Per-inning μ ≈ μ_total/9.
    inning_mu_home = mu_home / 9.0
    inning_mu_away = mu_away / 9.0
    p_nrfi = math.exp(-inning_mu_home) * math.exp(-inning_mu_away)

    bets: list[str] = []

    # Runs total — most sportsbooks line at 8.5
    common_line = 8.5
    if total_runs >= common_line + 0.7:
        bets.append(f"runs OVER {common_line} (model {total_runs:.1f})")
    elif total_runs <= common_line - 0.7:
        bets.append(f"runs UNDER {common_line} (model {total_runs:.1f})")

    # 1st 5 innings — who's the F5 favourite
    if mu_home_f5 - mu_away_f5 >= 0.4:
        bets.append(f"1st 5 innings {team_home} (F5 μ {mu_home_f5:.1f} vs {mu_away_f5:.1f})")
    elif mu_away_f5 - mu_home_f5 >= 0.4:
        bets.append(f"1st 5 innings {team_away} (F5 μ {mu_away_f5:.1f} vs {mu_home_f5:.1f})")

    # NRFI / YRFI
    if p_nrfi >= 0.58:
        bets.append(f"NRFI — no run 1st inning ({p_nrfi*100:.0f}%)")
    elif p_nrfi <= 0.42:
        bets.append(f"YRFI — run scored 1st inning ({(1-p_nrfi)*100:.0f}%)")

    accuracy = win_prob * 100
    if accuracy >= 65:
        headline = f"{winner} is gonna win vs {loser} ({accuracy:.0f}% accuracy)."
    elif accuracy >= 55:
        headline = f"{winner} slight favourite vs {loser} ({accuracy:.0f}% accuracy — tight match)."
    else:
        headline = f"{team_home} vs {team_away}: coin-flip ({accuracy:.0f}% lean, no clear winner)."

    if bets:
        return headline + " Bet on: " + ", ".join(bets) + "."
    return headline + " No clear secondary market — moneyline only."


def run_baseball_analysis(user_query: str, bankroll: float = 1000.0,
                          odds_a: float = 1.909, odds_b: float = 1.909) -> dict:
    """
    Full baseball pipeline:
    1. Parse query (Claude)
    2. Fetch MLB Stats API — starters, team stats, park factor
    3. Compute expected runs (Poisson model)
    4. Blend with Elo seeded from win%
    5. Compute all markets
    6. Persist match + signals + prediction to DB
    7. Kelly stake sizing
    8. Generate narrative (Claude)
    """
    init_db()
    steps: list[dict] = []

    # ── 1. Parse ─────────────────────────────────────────────────────────────
    try:
        from ai_agent_baseball import parse_baseball_query
        parsed = parse_baseball_query(user_query)
        steps.append({"step": "parse_query", "status": "ok", "data": parsed})
    except Exception as exc:
        return {"error": f"Could not parse query: {exc}", "steps": steps}

    team_a_raw = parsed.get("team_a", "Team A")
    team_b_raw = parsed.get("team_b", "Team B")
    game_date  = parsed.get("date") or datetime.now(timezone.utc).date().isoformat()

    # Override odds from query text if sportsbook lines were included
    _oa = parsed.get("odds_a_american")
    _ob = parsed.get("odds_b_american")
    if _oa is not None:
        try: odds_a = american_to_decimal(float(_oa))
        except Exception: pass
    if _ob is not None:
        try: odds_b = american_to_decimal(float(_ob))
        except Exception: pass

    # ── 2. Fetch MLB data (with AI fallback when API is unreachable) ────────────
    context: dict = {}
    ai_fallback = False

    try:
        context = fetch_baseball_context(team_a_raw, team_b_raw, game_date)
        if "error" in context:
            raise ValueError(context["error"])
        steps.append({"step": "mlb_fetch", "status": "ok",
                      "sources": context.get("sources", [])})
    except Exception as exc:
        steps.append({"step": "mlb_fetch", "status": "fallback",
                      "error": str(exc),
                      "note": "ESPN API unreachable — using AI signal estimates from training knowledge."})
        ai_fallback = True

    if ai_fallback:
        # Claude fills in wRC+, FIP, park factor from training knowledge
        try:
            from ai_agent_baseball import interpret_baseball_signals
            sigs = interpret_baseball_signals(team_a_raw, team_b_raw,
                                              parsed.get("notes", ""))
            steps.append({"step": "ai_signals", "status": "ok", "data": sigs})
        except Exception as exc2:
            steps.append({"step": "ai_signals", "status": "error", "error": str(exc2)})
            sigs = {}

        # Always treat team_a as home here instead of trusting sigs["home_team"].
        # This path only runs when the real ESPN/MLB schedule fetch failed, so
        # there's no live data to check Claude's guess against — and Claude has
        # gotten it backwards in practice (e.g. an AZ @ SD game logged with the
        # away team recorded as home), which corrupts mu_home/mu_away, the park
        # factor lookup, and ml_correct grading for that game. daily_nrfi.py
        # always builds the query as "{home_abbr} vs {away_abbr}", so team_a is
        # already the real home team for the automated pipeline; for manual
        # queries this is a documented assumption, not a verified fact.
        is_home_a  = True
        pf         = float(sigs.get("park_factor", 1.0))
        sig_a      = sigs.get("team_a", {})
        sig_b      = sigs.get("team_b", {})
        team_a     = team_a_raw
        team_b     = team_b_raw
        team_home  = team_a_raw if is_home_a else team_b_raw
        team_away  = team_b_raw if is_home_a else team_a_raw
        park_factor = pf
        wrc_a      = float(sig_a.get("wrc_plus", 100) or 100)
        wrc_b      = float(sig_b.get("wrc_plus", 100) or 100)
        fip_a      = float(sig_a.get("starter_fip", LEAGUE_AVG_FIP) or LEAGUE_AVG_FIP)
        fip_b      = float(sig_b.get("starter_fip", LEAGUE_AVG_FIP) or LEAGUE_AVG_FIP)
        win_pct_a  = float(sig_a.get("record_win_pct", 0.5) or 0.5)
        win_pct_b  = float(sig_b.get("record_win_pct", 0.5) or 0.5)
        starter_a  = {"name": sig_a.get("starter_name", "Unknown"),
                      "fip": fip_a, "era": float(sig_a.get("starter_era", LEAGUE_AVG_FIP) or LEAGUE_AVG_FIP),
                      "whip": 0.0, "k9": 0.0, "bb9": 0.0,
                      "innings_pitched": 0, "recent_games": [], "throws": "R"}
        starter_b  = {"name": sig_b.get("starter_name", "Unknown"),
                      "fip": fip_b, "era": float(sig_b.get("starter_era", LEAGUE_AVG_FIP) or LEAGUE_AVG_FIP),
                      "whip": 0.0, "k9": 0.0, "bb9": 0.0,
                      "innings_pitched": 0, "recent_games": [], "throws": "R"}
        record_a   = {"win_pct": win_pct_a, "games_played": 0}
        record_b   = {"win_pct": win_pct_b, "games_played": 0}
        hitting_a  = {"wrc_plus": wrc_a, "ops": 0.0, "runs_per_game": 0.0}
        hitting_b  = {"wrc_plus": wrc_b, "ops": 0.0, "runs_per_game": 0.0}
        context = {
            "team_a": {"name": team_a, "is_home": is_home_a, "hitting": hitting_a,
                       "starter": starter_a, "record": record_a,
                       "team_pitching": {"era": LEAGUE_BULLPEN_FIP}},
            "team_b": {"name": team_b, "is_home": not is_home_a, "hitting": hitting_b,
                       "starter": starter_b, "record": record_b,
                       "team_pitching": {"era": LEAGUE_BULLPEN_FIP}},
            "game": {"date": game_date, "venue": "", "park_factor": pf,
                     "home_team": team_home, "away_team": team_away},
            "sources": [{"label": "AI signal estimation",
                         "url": "", "snippet": sigs.get("notes", "")}],
        }
    else:
        team_a      = context["team_a"]["name"]
        team_b      = context["team_b"]["name"]
        is_home_a   = context["team_a"]["is_home"]
        park_factor = context["game"]["park_factor"]
        team_home   = context["game"]["home_team"]
        team_away   = context["game"]["away_team"]
        hitting_a   = context["team_a"].get("hitting", {})
        hitting_b   = context["team_b"].get("hitting", {})
        starter_a   = context["team_a"].get("starter", {})
        starter_b   = context["team_b"].get("starter", {})
        record_a    = context["team_a"].get("record", {})
        record_b    = context["team_b"].get("record", {})
        wrc_a = hitting_a.get("wrc_plus") or 100
        wrc_b = hitting_b.get("wrc_plus") or 100
        fip_a = starter_a.get("fip") or LEAGUE_AVG_FIP
        fip_b = starter_b.get("fip") or LEAGUE_AVG_FIP

    # ── 2.5. Enrich starters with FanGraphs SIERA/xFIP + Statcast barrel% ────
    # Non-destructive: enrich_starter returns a new dict; originals unchanged.
    # Falls back silently — ESPN data remains the guaranteed fallback.
    if not ai_fallback:
        abbr_a = context["team_a"].get("abbreviation", "")
        abbr_b = context["team_b"].get("abbreviation", "")
        try:
            starter_a = enrich_starter(starter_a, abbr_a)
            starter_b = enrich_starter(starter_b, abbr_b)
            hitting_a = enrich_team_hitting(hitting_a, abbr_a)
            hitting_b = enrich_team_hitting(hitting_b, abbr_b)
            # Re-read FIP after enrichment (may now be SIERA or xFIP)
            fip_a = starter_a.get("fip") or LEAGUE_AVG_FIP
            fip_b = starter_b.get("fip") or LEAGUE_AVG_FIP
            wrc_a = hitting_a.get("wrc_plus") or 100
            wrc_b = hitting_b.get("wrc_plus") or 100
            steps.append({
                "step": "savant_enrich", "status": "ok",
                "fip_source_a": starter_a.get("fip_source", "espn"),
                "fip_source_b": starter_b.get("fip_source", "espn"),
                "wrc_source_a": hitting_a.get("wrc_plus_source", "espn"),
                "wrc_source_b": hitting_b.get("wrc_plus_source", "espn"),
            })
        except Exception as exc:
            steps.append({"step": "savant_enrich", "status": "skipped", "error": str(exc)})

    # ── Data confidence (how much is real live data vs defaults) ─────────────
    data_confidence = _baseball_data_confidence(
        ai_fallback, starter_a, starter_b, hitting_a, hitting_b, record_a, record_b,
    )

    # Injury gate (Kimi's rev): a probable starter appearing on the injury
    # report at all — any status — means we're likely pricing a game around a
    # pitcher who won't actually throw. Downgrade rather than guess at a
    # replacement's stats (an unnamed replacement has no real FIP/Savant data
    # anyway, so we'd just be feeding league-average numbers under a wrong
    # name). This is a data-quality gate, not a new predictive signal — it
    # only ever pushes confidence down, never up, and never touches the run
    # model's inputs directly.
    starter_injury_flag = bool(starter_a.get("injury_status") or starter_b.get("injury_status"))
    if starter_injury_flag and data_confidence != "low":
        data_confidence = "low"

    steps.append({"step": "data_confidence", "status": "ok",
                  "level": data_confidence,
                  "starter_a": starter_a.get("name", "TBD"),
                  "starter_b": starter_b.get("name", "TBD"),
                  "starter_a_injury": starter_a.get("injury_status"),
                  "starter_b_injury": starter_b.get("injury_status"),
                  "injury_downgrade": starter_injury_flag})

    # ── 2.6. Weather fetch + fatigue / rest ──────────────────────────────────
    # Weather is now applied to the run model (not just logged).
    weather_data = None
    try:
        stadium_code = team_to_stadium_code(team_home)
        if stadium_code:
            weather_data = fetch_game_weather(stadium_code, game_date)
    except Exception:
        pass
    weather_sigs = weather_to_signals(weather_data)

    id_a = context["team_a"].get("id", "") if not ai_fallback else ""
    id_b = context["team_b"].get("id", "") if not ai_fallback else ""

    fatigue_a = fetch_team_fatigue(str(id_a), game_date)
    fatigue_b = fetch_team_fatigue(str(id_b), game_date)

    # Combined weather multiplier (applied to all expected runs for this park)
    # temp_factor ≈ 1.0 ± 0.05; wind_factor [-1,+1] × wind_mph × 0.004
    is_dome    = bool(weather_sigs.get("is_dome"))
    temp_f     = weather_sigs.get("temp_factor", 1.0)
    wind_align = weather_sigs.get("wind_factor", 0.0)
    wind_mph   = weather_sigs.get("wind_mph", 0.0)
    if is_dome:
        weather_factor = 1.0
    else:
        wind_contrib   = wind_align * wind_mph * 0.004   # ±6% at 15mph aligned wind
        weather_factor = max(0.85, min(1.15, temp_f * (1.0 + wind_contrib)))

    steps.append({
        "step":            "fatigue_weather",
        "status":          "ok",
        "games_last_3_a":  fatigue_a.get("games_last_3"),
        "games_last_3_b":  fatigue_b.get("games_last_3"),
        "rest_days_a":     fatigue_a.get("rest_days"),
        "rest_days_b":     fatigue_b.get("rest_days"),
        "bullpen_fat_a":   fatigue_a["bullpen_fatigue_mult"],
        "bullpen_fat_b":   fatigue_b["bullpen_fatigue_mult"],
        "weather_factor":  round(weather_factor, 4),
        "is_dome":         is_dome,
    })

    # ── 3. Bullpen FIP derivation + platoon adjustment ────────────────────────
    # avg IP needed here (bullpen derivation) AND below (expected_runs_split).
    def _avg_ip(starter: dict) -> Optional[float]:
        ip = starter.get("innings_pitched") or 0
        gs = starter.get("games_started") or 0
        return round(ip / gs, 2) if gs >= 3 else None

    avg_ip_a = _avg_ip(starter_a)
    avg_ip_b = _avg_ip(starter_b)

    team_pit_a    = context["team_a"].get("team_pitching", {})
    team_pit_b    = context["team_b"].get("team_pitching", {})
    era_a = starter_a.get("era") or None   # None → falls back to FIP in decomposition
    era_b = starter_b.get("era") or None
    bullpen_fip_a = _derive_bullpen_fip(team_pit_a.get("era", 0), fip_a, avg_ip_a, era_a)
    bullpen_fip_b = _derive_bullpen_fip(team_pit_b.get("era", 0), fip_b, avg_ip_b, era_b)
    # Apply fatigue multiplier: tired bullpen → effectively worse FIP
    bullpen_fip_a *= fatigue_a["bullpen_fatigue_mult"]
    bullpen_fip_b *= fatigue_b["bullpen_fatigue_mult"]
    bullpen_fip_a  = max(3.0, min(bullpen_fip_a, 7.5))
    bullpen_fip_b  = max(3.0, min(bullpen_fip_b, 7.5))

    throws_a = starter_a.get("throws", "R")
    throws_b = starter_b.get("throws", "R")
    # team_a bats against team_b's starter (throws_b); team_b bats against team_a's starter (throws_a)
    wrc_a_adj = platoon_wrc_adjust(wrc_a, throws_b)
    wrc_b_adj = platoon_wrc_adjust(wrc_b, throws_a)

    steps.append({"step": "bullpen_platoon", "status": "ok",
                  "bullpen_fip_a": round(bullpen_fip_a, 2),
                  "bullpen_fip_b": round(bullpen_fip_b, 2),
                  "throws_a": throws_a, "throws_b": throws_b,
                  "wrc_a_adj": round(wrc_a_adj, 1), "wrc_b_adj": round(wrc_b_adj, 1)})

    # ── 4. Expected runs (split F5 / L4) ─────────────────────────────────────
    # Home bats against away starter (F5) + away bullpen (L4); vice versa for away.
    off_rest_home = fatigue_a["off_rest_mult"] if is_home_a else fatigue_b["off_rest_mult"]
    off_rest_away = fatigue_b["off_rest_mult"] if is_home_a else fatigue_a["off_rest_mult"]

    # Pitch-process signals: home bats against away starter, vice versa
    # so we pass the OPPOSING starter's process metrics with each call.
    if is_home_a:
        mu_home_f5, mu_home_l4 = expected_runs_split(
            wrc_a_adj, fip_b, bullpen_fip_b, park_factor, True,  avg_ip_b,
            weather_factor, off_rest_home,
            opp_starter_csw_pct    = starter_b.get("csw_pct"),
            opp_starter_fb_velo    = starter_b.get("avg_fb_velo"),
            opp_starter_o_swing    = starter_b.get("o_swing_pct"),
            opp_starter_barrel_pct = starter_b.get("barrel_pct_against"),
        )
        mu_away_f5, mu_away_l4 = expected_runs_split(
            wrc_b_adj, fip_a, bullpen_fip_a, park_factor, False, avg_ip_a,
            weather_factor, off_rest_away,
            opp_starter_csw_pct    = starter_a.get("csw_pct"),
            opp_starter_fb_velo    = starter_a.get("avg_fb_velo"),
            opp_starter_o_swing    = starter_a.get("o_swing_pct"),
            opp_starter_barrel_pct = starter_a.get("barrel_pct_against"),
        )
    else:
        mu_home_f5, mu_home_l4 = expected_runs_split(
            wrc_b_adj, fip_a, bullpen_fip_a, park_factor, True,  avg_ip_a,
            weather_factor, off_rest_home,
            opp_starter_csw_pct    = starter_a.get("csw_pct"),
            opp_starter_fb_velo    = starter_a.get("avg_fb_velo"),
            opp_starter_o_swing    = starter_a.get("o_swing_pct"),
            opp_starter_barrel_pct = starter_a.get("barrel_pct_against"),
        )
        mu_away_f5, mu_away_l4 = expected_runs_split(
            wrc_a_adj, fip_b, bullpen_fip_b, park_factor, False, avg_ip_b,
            weather_factor, off_rest_away,
            opp_starter_csw_pct    = starter_b.get("csw_pct"),
            opp_starter_fb_velo    = starter_b.get("avg_fb_velo"),
            opp_starter_o_swing    = starter_b.get("o_swing_pct"),
            opp_starter_barrel_pct = starter_b.get("barrel_pct_against"),
        )

    mu_home = mu_home_f5 + mu_home_l4
    mu_away = mu_away_f5 + mu_away_l4

    from models.baseball_market import pitcher_process_adjustment
    _proc_home_starter = starter_b if is_home_a else starter_a
    _proc_away_starter = starter_a if is_home_a else starter_b
    steps.append({"step": "run_model", "status": "ok",
                  "mu_home": round(mu_home, 2), "mu_away": round(mu_away, 2),
                  "mu_home_f5": round(mu_home_f5, 2), "mu_home_l4": round(mu_home_l4, 2),
                  "mu_away_f5": round(mu_away_f5, 2), "mu_away_l4": round(mu_away_l4, 2),
                  "process_adj_home_starter": round(pitcher_process_adjustment(
                      _proc_home_starter.get("csw_pct"),
                      _proc_home_starter.get("avg_fb_velo"),
                      _proc_home_starter.get("o_swing_pct"),
                  ), 4),
                  "process_adj_away_starter": round(pitcher_process_adjustment(
                      _proc_away_starter.get("csw_pct"),
                      _proc_away_starter.get("avg_fb_velo"),
                      _proc_away_starter.get("o_swing_pct"),
                  ), 4),
                  })

    # ── 5. Compute markets ────────────────────────────────────────────────────
    markets_raw = compute_baseball_markets(
        mu_home, mu_away, mu_home_f5, mu_away_f5, mu_home_l4, mu_away_l4
    )
    ml          = markets_raw["moneyline"]
    prob_home   = ml["p_home_win"]
    prob_away   = ml["p_away_win"]

    # Map back to team_a / team_b perspective
    prob_a = prob_home if is_home_a else prob_away
    prob_b = prob_away if is_home_a else prob_home

    # ── 6. Elo blend (40% weight) ────────────────────────────────────────────
    elo_explanation = ""
    try:
        elo = EloModel()
        if record_a.get("games_played", 0) >= 10:
            gp_a = record_a["games_played"]
            elo.set_rating(f"MLB:{team_a}", _elo_from_winpct(_shrink_win_pct(record_a["win_pct"], gp_a)))
        if record_b.get("games_played", 0) >= 10:
            gp_b = record_b["games_played"]
            elo.set_rating(f"MLB:{team_b}", _elo_from_winpct(_shrink_win_pct(record_b["win_pct"], gp_b)))
        elo_a, elo_b = elo.win_probability(f"MLB:{team_a}", f"MLB:{team_b}")
        prob_a = 0.6 * prob_a + 0.4 * elo_a
        prob_b = 0.6 * prob_b + 0.4 * elo_b
        total  = prob_a + prob_b
        prob_a /= total
        prob_b /= total
        # Cap: no single MLB game should exceed 72% confidence (65% at Coors-type parks).
        MAX_MLB_PROB = 0.65 if park_factor >= 1.20 else 0.72
        if prob_a > MAX_MLB_PROB:
            prob_a = MAX_MLB_PROB
            prob_b = 1.0 - MAX_MLB_PROB
        elif prob_b > MAX_MLB_PROB:
            prob_b = MAX_MLB_PROB
            prob_a = 1.0 - MAX_MLB_PROB
        elo_explanation = (
            f" Elo (from win%): {team_a} {elo_a*100:.1f}% / {team_b} {elo_b*100:.1f}%."
        )
        steps.append({"step": "elo_blend", "status": "ok",
                      "elo_a": round(elo_a, 3), "elo_b": round(elo_b, 3)})
    except Exception as exc:
        steps.append({"step": "elo_blend", "status": "skipped", "error": str(exc)})

    # Final home/away probabilities after the Elo blend + 65/72% cap above —
    # everything downstream (narrative, displayed bars, moneyline market) must
    # use these, not the raw pre-blend prob_home/prob_away from step 5, or the
    # AI narrative ends up quoting a different number than what's on screen
    # (caught 2026-07-11: bars showed 51.5%/48.5% while the narrative said
    # 52.6% because generate_baseball_narrative was still getting the raw
    # Poisson-only prob_home/prob_away).
    prob_home_final = prob_a if is_home_a else prob_b
    prob_away_final = prob_b if is_home_a else prob_a

    home_starter_name = starter_a["name"] if is_home_a else starter_b["name"]
    away_starter_name = starter_b["name"] if is_home_a else starter_a["name"]
    home_starter_fip  = fip_b if is_home_a else fip_a   # home bats vs away starter
    away_starter_fip  = fip_a if is_home_a else fip_b
    home_starter_hand = throws_b if is_home_a else throws_a
    away_starter_hand = throws_a if is_home_a else throws_b
    home_bp_fip = bullpen_fip_b if is_home_a else bullpen_fip_a
    away_bp_fip = bullpen_fip_a if is_home_a else bullpen_fip_b

    # Build fatigue/weather note for explanation
    _fat_note = ""
    if fatigue_a.get("games_last_3") is not None or fatigue_b.get("games_last_3") is not None:
        gl3a = fatigue_a.get("games_last_3", "?")
        gl3b = fatigue_b.get("games_last_3", "?")
        _fat_note = f" Fatigue: {team_a} {gl3a}g/3d, {team_b} {gl3b}g/3d."
    _wx_note = (
        f" Weather factor {weather_factor:.3f}" + (" (dome)" if is_dome else "")
        + "."
    ) if weather_factor != 1.0 or is_dome else ""

    explanation = (
        f"Split Poisson: F5 μ_home={mu_home_f5:.2f}+L4 {mu_home_l4:.2f}={mu_home:.2f} runs; "
        f"F5 μ_away={mu_away_f5:.2f}+L4 {mu_away_l4:.2f}={mu_away:.2f} runs. "
        f"Park factor={park_factor}. "
        f"Home starter: {home_starter_name} [{home_starter_hand}HP] FIP {home_starter_fip:.2f}, "
        f"bullpen FIP {home_bp_fip:.2f}. "
        f"Away starter: {away_starter_name} [{away_starter_hand}HP] FIP {away_starter_fip:.2f}, "
        f"bullpen FIP {away_bp_fip:.2f}."
        + _fat_note + _wx_note + elo_explanation
    )

    # weather_data / weather_sigs already fetched at step 2.6 above

    # ── 7. Persist ───────────────────────────────────────────────────────────
    match_id: Optional[int] = None
    try:
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO matches
                       (sport, league, participant_a, participant_b,
                        scheduled_at, venue, neutral_site)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "baseball", "MLB",
                    team_a, team_b,
                    f"{game_date}T12:00:00",
                    context["game"].get("venue", ""),
                    0,
                ),
            )
            match_id = cur.lastrowid

        signals_to_log = [
            ("wrc_plus",        team_a,    wrc_a),
            ("wrc_plus",        team_b,    wrc_b),
            ("starter_fip",     team_a,    fip_a),
            ("starter_fip",     team_b,    fip_b),
            ("bullpen_fip",     team_a,    bullpen_fip_a),
            ("bullpen_fip",     team_b,    bullpen_fip_b),
            ("park_factor",     None,      park_factor),
            ("mu_runs",         team_home, mu_home),
            ("mu_runs",         team_away, mu_away),
            ("mu_runs_f5",      team_home, mu_home_f5),
            ("mu_runs_f5",      team_away, mu_away_f5),
        ]
        if avg_ip_a is not None:
            signals_to_log.append(("starter_avg_ip", team_a, avg_ip_a))
        if avg_ip_b is not None:
            signals_to_log.append(("starter_avg_ip", team_b, avg_ip_b))
        if fatigue_a.get("games_last_3") is not None:
            signals_to_log.append(("games_last_3", team_a, fatigue_a["games_last_3"]))
            signals_to_log.append(("bullpen_fatigue_mult", team_a, fatigue_a["bullpen_fatigue_mult"]))
        if fatigue_b.get("games_last_3") is not None:
            signals_to_log.append(("games_last_3", team_b, fatigue_b["games_last_3"]))
            signals_to_log.append(("bullpen_fatigue_mult", team_b, fatigue_b["bullpen_fatigue_mult"]))
        if fatigue_a.get("rest_days") is not None:
            signals_to_log.append(("rest_days", team_a, fatigue_a["rest_days"]))
        if fatigue_b.get("rest_days") is not None:
            signals_to_log.append(("rest_days", team_b, fatigue_b["rest_days"]))
        signals_to_log.append(("weather_factor", None, weather_factor))
        for sig, key in (
            ("siera",               "siera"),
            ("xfip",                "xfip"),
            ("barrel_pct_against",  "barrel_pct_against"),
            ("xwoba_against",       "xwoba_against"),
            ("f_strike_pct",        "f_strike_pct"),
            ("zone_pct",            "zone_pct"),
            ("o_swing_pct",         "o_swing_pct"),
            ("csw_pct",             "csw_pct"),
            ("avg_fb_velo",         "avg_fb_velo"),
            ("hr_fb_pct",           "hr_fb_pct"),
        ):
            if starter_a.get(key) is not None:
                signals_to_log.append((sig, team_a, starter_a[key]))
            if starter_b.get(key) is not None:
                signals_to_log.append((sig, team_b, starter_b[key]))
        for sig_name, participant, val in signals_to_log:
            log_signal(match_id, sig_name, participant,
                       signal_value=float(val), source="mlb_api")

        # Data confidence (text signal → drives audit/calibration by-confidence)
        log_signal(match_id, "data_confidence", None,
                   signal_text=data_confidence, source="mlb_api")

        # Weather signals (participant=None → game-level, not team-specific)
        forecast_ts = weather_data.get("forecast_time", "") if weather_data else None
        weather_source = "openweather" if weather_data else "fallback"
        for sig_name, sig_val in weather_sigs.items():
            log_signal(match_id, sig_name, None,
                       signal_value=sig_val,
                       signal_text=forecast_ts,
                       source=weather_source)

        with get_db() as conn:
            conn.execute(
                """INSERT INTO predictions
                       (match_id, method, prob_a, prob_b, prob_draw, explanation, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    match_id, "baseball_v2",
                    round(prob_a, 6), round(prob_b, 6), 0.0,
                    explanation,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        steps.append({"step": "persist", "status": "ok", "match_id": match_id})
    except Exception as exc:
        steps.append({"step": "persist", "status": "error", "error": str(exc),
                      "trace": traceback.format_exc()})

    # ── 8. Kelly stake + market comparison ──────────────────────────────────
    kelly   = kelly_stake(prob_a, odds_a, bankroll)
    kelly_b = kelly_stake(prob_b, odds_b, bankroll)

    # Market comparison: only meaningful when the caller provided real odds
    _using_default_odds = (abs(odds_a - 1.909) < 0.001 and abs(odds_b - 1.909) < 0.001)
    if not _using_default_odds:
        market_comparison = market_edge_summary(prob_a, prob_b, odds_a, odds_b)
    else:
        market_comparison = {
            "has_real_odds": False,
            "note": ("Include sportsbook odds in your query to see market edge. "
                     "Example: 'NYY -130 vs BOS +110 tonight'"),
        }

    # ── 9. Narrative ─────────────────────────────────────────────────────────
    narrative = ""
    try:
        from ai_agent_baseball import generate_baseball_narrative
        narrative_context = {
            **context,
            "mu_home": round(mu_home, 2),
            "mu_away": round(mu_away, 2),
        }
        narrative = generate_baseball_narrative(
            team_home, team_away,
            prob_home_final, prob_away_final,
            explanation, narrative_context,
        )
        steps.append({"step": "narrative", "status": "ok"})
    except Exception as exc:
        narrative = (
            f"{team_home} win probability {prob_home_final*100:.1f}%, "
            f"{team_away} {prob_away_final*100:.1f}%. "
            f"Expected runs: home {mu_home:.1f}, away {mu_away:.1f}."
        )
        steps.append({"step": "narrative", "status": "error", "error": str(exc)})

    # ── Format ────────────────────────────────────────────────────────────────
    # Override moneyline with Elo-blended + capped probabilities (computed
    # right after the Elo blend, above) so BET recommendations use the final
    # model output, not raw Poisson.
    markets_raw["moneyline"]["p_home_win"] = prob_home_final
    markets_raw["moneyline"]["p_away_win"] = prob_away_final
    formatted_markets = _format_baseball_markets(markets_raw, team_home, team_away)

    # Starters in home/away order for easy frontend rendering
    home_starter = starter_a if is_home_a else starter_b
    away_starter = starter_b if is_home_a else starter_a

    # ── NRFI model override (replaces mu/9 approximation when trained) ────────
    # predict_nrfi() returns None when models/nrfi_xgb.json doesn't exist yet.
    # Fetch today's confirmed top-3 lineup OPS from MLB Stats API; falls back
    # to None (model uses league-average 100) if lineup not yet posted.
    _home_t3_wrc: float | None = None
    _away_t3_wrc: float | None = None
    _lineup_note = ""
    try:
        from fetchers.nrfi_lineup import get_nrfi_lineup_wrc
        _home_t3_wrc, _away_t3_wrc = get_nrfi_lineup_wrc(
            home_team=team_home, away_team=team_away, game_date=game_date,
        )
        if _home_t3_wrc is not None and _away_t3_wrc is not None:
            _lineup_note = (f" | Lineup wRC+ (approx): home top-3 {_home_t3_wrc:.0f}, "
                            f"away top-3 {_away_t3_wrc:.0f}")
    except Exception:
        pass

    _nrfi_ml_prob = predict_nrfi(
        home_starter, away_starter, team_home, park_factor,
        home_top3_wrc=_home_t3_wrc, away_top3_wrc=_away_t3_wrc,
        home_fi_rate=home_starter.get("fi_rate"),
        away_fi_rate=away_starter.get("fi_rate"),
    )
    if _nrfi_ml_prob is not None and "nrfi" in formatted_markets:
        _p_nrfi_ml = round(_nrfi_ml_prob * 100, 1)
        _p_yrfi_ml = round((1 - _nrfi_ml_prob) * 100, 1)
        formatted_markets["nrfi"] = {
            "label": "NRFI / YRFI",
            "options": [
                {"label": "NRFI (No Run First Inning)", "prob": _p_nrfi_ml,
                 "best": _p_nrfi_ml >= _p_yrfi_ml},
                {"label": "YRFI (Yes Run First Inning)", "prob": _p_yrfi_ml,
                 "best": _p_yrfi_ml > _p_nrfi_ml},
            ],
            "note": f"XGBoost model trained on 2022–2025 MLB first-inning outcomes{_lineup_note}",
            "model": "xgb_calibrated",
        }
        steps.append({"step": "nrfi_model", "status": "ok",
                      "p_nrfi": _p_nrfi_ml, "source": "xgboost",
                      "home_top3_wrc": _home_t3_wrc, "away_top3_wrc": _away_t3_wrc})
    else:
        # Poisson mu/9 approximation — unvalidated, label it clearly
        if "nrfi" in formatted_markets:
            formatted_markets["nrfi"]["note"] = (
                "Poisson approximation (mu/9) — unvalidated. "
                "Run build_nrfi_dataset.py + nrfi_model.py --train to activate XGBoost model."
            )
            formatted_markets["nrfi"]["model"] = "poisson_approx"
        steps.append({"step": "nrfi_model", "status": "skipped",
                      "reason": "model not trained yet"})
    home_hitting  = hitting_a if is_home_a else hitting_b
    away_hitting  = hitting_b if is_home_a else hitting_a
    home_record   = record_a  if is_home_a else record_b
    away_record   = record_b  if is_home_a else record_a

    home_bp_fip_out = bullpen_fip_b if is_home_a else bullpen_fip_a
    away_bp_fip_out = bullpen_fip_a if is_home_a else bullpen_fip_b

    # ── Build ai_signals list for "Model signals used" table ──────────────────
    _fat_home = fatigue_a if is_home_a else fatigue_b
    _fat_away = fatigue_b if is_home_a else fatigue_a
    _bp_home  = bullpen_fip_a if is_home_a else bullpen_fip_b
    _bp_away  = bullpen_fip_b if is_home_a else bullpen_fip_a

    ai_signals: list[dict] = []
    def _add(team: str, signal: str, value, source: str) -> None:
        if value is not None:
            ai_signals.append({"team": team, "signal": signal,
                                "value": str(value), "source": source})

    # Offense
    _add(team_home, "wRC+", home_hitting.get("wrc_plus"),
         home_hitting.get("wrc_plus_source", "espn"))
    _add(team_away, "wRC+", away_hitting.get("wrc_plus"),
         away_hitting.get("wrc_plus_source", "espn"))
    if home_hitting.get("woba"):
        _add(team_home, "wOBA", round(home_hitting["woba"], 3), "fangraphs")
    if away_hitting.get("woba"):
        _add(team_away, "wOBA", round(away_hitting["woba"], 3), "fangraphs")
    if home_hitting.get("iso"):
        _add(team_home, "ISO", round(home_hitting["iso"], 3), "fangraphs")
    if away_hitting.get("iso"):
        _add(team_away, "ISO", round(away_hitting["iso"], 3), "fangraphs")

    # Starters
    for tm, s in [(team_home, home_starter), (team_away, away_starter)]:
        nm  = s.get("name", "")
        src = s.get("fip_source", "espn")
        metric = "SIERA" if s.get("siera") else ("xFIP" if s.get("xfip") else "FIP")
        val    = s.get("siera") or s.get("xfip") or s.get("fip")
        if val:
            _add(tm, f"{metric} ({nm})", round(float(val), 2), src)
        if s.get("era"):
            _add(tm, f"ERA ({nm})", round(float(s["era"]), 2), src)
        if s.get("csw_pct"):
            _add(tm, f"CSW% ({nm})", f"{s['csw_pct']*100:.1f}%", "savant")
        if s.get("avg_fb_velo"):
            _add(tm, f"FB Velo ({nm})", f"{s['avg_fb_velo']:.1f} mph", "savant")
        if s.get("o_swing_pct"):
            _add(tm, f"O-Swing% ({nm})", f"{s['o_swing_pct']*100:.1f}%", "fangraphs")
        if s.get("barrel_pct_against"):
            # Already a raw percent from the Savant feature store (e.g. 10.4
            # meaning 10.4%) — no *100 needed here, unlike csw_pct/o_swing_pct
            # above which are decimal-scale. Fixed 2026-07-05 (was showing
            # e.g. 1040.0% instead of 10.4%).
            _add(tm, f"Barrel% vs ({nm})", f"{s['barrel_pct_against']:.1f}%", "savant")
        if s.get("fastball_pct"):
            _add(tm, f"Fastball% ({nm})", f"{s['fastball_pct']:.0f}%", "savant")

    # Bullpen
    _add(team_home, "Bullpen FIP (adj)", round(_bp_home, 2), "derived")
    _add(team_away, "Bullpen FIP (adj)", round(_bp_away, 2), "derived")

    # Fatigue & rest
    for tm, fat in [(team_home, _fat_home), (team_away, _fat_away)]:
        if fat.get("games_last_3") is not None:
            _add(tm, "Games last 3 days", fat["games_last_3"], "espn_schedule")
        if fat.get("rest_days") is not None:
            _add(tm, "Rest days", fat["rest_days"], "espn_schedule")
        mult = fat.get("bullpen_fatigue_mult", 1.0)
        if mult != 1.0:
            _add(tm, "Bullpen fatigue mult", round(mult, 2), "espn_schedule")

    # Park & weather
    _add("—", "Park factor", park_factor, "espn")
    if is_dome:
        _add("—", "Venue type", "Dome — weather neutral", "stadium")
    else:
        wconf = weather_sigs.get("weather_confidence", 0.0)
        _wsrc = "openweather" if wconf > 0 else "no OPENWEATHER_API_KEY"
        tf = weather_sigs.get("temp_f", 72.0)
        wm = weather_sigs.get("wind_mph", 0.0)
        if tf != 72.0:
            _add("—", "Temperature", f"{tf:.0f}°F", _wsrc)
        if wm > 0:
            _add("—", "Wind speed", f"{wm:.0f} mph", _wsrc)
        if weather_factor != 1.0:
            _add("—", "Weather factor", round(weather_factor, 3), _wsrc)

    result = {
        "match_id":   match_id,
        "team_a":     team_a,
        "team_b":     team_b,
        "team_home":  team_home,
        "team_away":  team_away,
        "sport":      "baseball",
        "date":       game_date,
        "venue":      context["game"].get("venue", ""),
        "park_factor": park_factor,
        "data_confidence": data_confidence,
        "starter_injury_flag": starter_injury_flag,
        "starter_a_injury": starter_a.get("injury_status"),
        "starter_b_injury": starter_b.get("injury_status"),
        "plain_summary": _build_plain_summary(
            team_home, team_away,
            (prob_a if team_a == team_home else prob_b),
            (prob_b if team_a == team_home else prob_a),
            mu_home, mu_away, mu_home_f5, mu_away_f5,
        ),
        "narrative":  narrative,
        "prob_a":     round(prob_a * 100, 1),
        "prob_b":     round(prob_b * 100, 1),
        "prob_draw":  0.0,
        "mu_home":    round(mu_home, 2),
        "mu_away":    round(mu_away, 2),
        "mu_home_f5": round(mu_home_f5, 2),
        "mu_away_f5": round(mu_away_f5, 2),
        "mu_home_l4": round(mu_home_l4, 2),
        "mu_away_l4": round(mu_away_l4, 2),
        "starters": {
            "home": {
                "team":            team_home,
                "name":            home_starter.get("name", "TBD"),
                "throws":          home_starter.get("throws", "R"),
                "fip":             home_starter.get("fip"),
                "era":             home_starter.get("era"),
                "whip":            home_starter.get("whip"),
                "k9":              home_starter.get("k9"),
                "bb9":             home_starter.get("bb9"),
                "innings_pitched":      home_starter.get("innings_pitched"),
                "recent_games":         home_starter.get("recent_games", []),
                "bullpen_fip":          round(home_bp_fip_out, 2),
                "siera":                home_starter.get("siera"),
                "xfip":                 home_starter.get("xfip"),
                "fip_source":           home_starter.get("fip_source", "espn"),
                "barrel_pct_against":   home_starter.get("barrel_pct_against"),
                "xwoba_against":        home_starter.get("xwoba_against"),
                "f_strike_pct":         home_starter.get("f_strike_pct"),
                "zone_pct":             home_starter.get("zone_pct"),
                "o_swing_pct":          home_starter.get("o_swing_pct"),
                "csw_pct":              home_starter.get("csw_pct"),
                "avg_fb_velo":          home_starter.get("avg_fb_velo"),
                "fastball_pct":         home_starter.get("fastball_pct"),
                "breaking_pct":         home_starter.get("breaking_pct"),
                "pitch_details":        home_starter.get("pitch_details", []),
                "injury_status":        home_starter.get("injury_status"),
                "injury_detail":        home_starter.get("injury_detail"),
            },
            "away": {
                "team":                 team_away,
                "name":                 away_starter.get("name", "TBD"),
                "throws":               away_starter.get("throws", "R"),
                "fip":                  away_starter.get("fip"),
                "era":                  away_starter.get("era"),
                "whip":                 away_starter.get("whip"),
                "k9":                   away_starter.get("k9"),
                "bb9":                  away_starter.get("bb9"),
                "innings_pitched":      away_starter.get("innings_pitched"),
                "recent_games":         away_starter.get("recent_games", []),
                "bullpen_fip":          round(away_bp_fip_out, 2),
                "siera":                away_starter.get("siera"),
                "xfip":                 away_starter.get("xfip"),
                "fip_source":           away_starter.get("fip_source", "espn"),
                "barrel_pct_against":   away_starter.get("barrel_pct_against"),
                "xwoba_against":        away_starter.get("xwoba_against"),
                "f_strike_pct":         away_starter.get("f_strike_pct"),
                "zone_pct":             away_starter.get("zone_pct"),
                "o_swing_pct":          away_starter.get("o_swing_pct"),
                "csw_pct":              away_starter.get("csw_pct"),
                "avg_fb_velo":          away_starter.get("avg_fb_velo"),
                "fastball_pct":         away_starter.get("fastball_pct"),
                "breaking_pct":         away_starter.get("breaking_pct"),
                "pitch_details":        away_starter.get("pitch_details", []),
                "injury_status":        away_starter.get("injury_status"),
                "injury_detail":        away_starter.get("injury_detail"),
            },
        },
        "team_stats": {
            "home": {
                "team":          team_home,
                "record":        home_record,
                "wrc_plus":      home_hitting.get("wrc_plus"),
                "wrc_source":    home_hitting.get("wrc_plus_source", "espn"),
                "woba":          home_hitting.get("woba"),
                "iso":           home_hitting.get("iso"),
                "ops":           home_hitting.get("ops"),
                "runs_per_game": home_hitting.get("runs_per_game"),
            },
            "away": {
                "team":          team_away,
                "record":        away_record,
                "wrc_plus":      away_hitting.get("wrc_plus"),
                "wrc_source":    away_hitting.get("wrc_plus_source", "espn"),
                "woba":          away_hitting.get("woba"),
                "iso":           away_hitting.get("iso"),
                "ops":           away_hitting.get("ops"),
                "runs_per_game": away_hitting.get("runs_per_game"),
            },
        },
        "model_explanation": explanation,
        "kelly_a":           kelly,
        "kelly_b":           kelly_b,
        "kelly_note":        kelly.get("note", ""),
        "markets":           formatted_markets,
        "bet_recommendations": _bet_recommendations(
            formatted_markets, home_starter, away_starter, team_home, team_away,
            bankroll=bankroll,
            data_confidence=data_confidence,
            market_implied_home=(1.0 / odds_a if not _using_default_odds else None),
            market_implied_away=(1.0 / odds_b if not _using_default_odds else None),
        ),
        "ai_signals":        ai_signals,
        "market_comparison": market_comparison,
        "raw_sources":       context.get("sources", []),
        "game_pk":           context.get("game", {}).get("game_pk"),
        "steps":             steps,
    }

    # ── Log every market's prediction to DB for performance tracking ─────────
    # Fires on every call — manual website queries AND the automated daily
    # pipeline — so ad-hoc "just checking a matchup" queries get graded later
    # too, not just the scheduled slate. See resolve_pending_bets().
    _log_prediction(result, game_date, team_home, team_away,
                    home_starter, away_starter, bankroll)
    return result
