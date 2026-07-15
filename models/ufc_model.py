"""
UFC win-probability + method-of-victory model.

Two separate pieces, per Kimi's original brief — this is NOT a score model
(there's no continuous "score" in MMA the way there is in soccer/rugby):

  1. Win probability — Glicko-2 (models/glicko.py, reused as-is, scoped
     sport="ufc" and surface=weight_class) blended with a stats-based
     logistic. The blend weight leans on Glicko-2 more as its RD (rating
     uncertainty) shrinks, and leans on raw stats more when RD is still high
     (a fighter with few logged fights) — the same "RD = doubt" framing used
     to justify Glicko-2 over plain Elo for this sport in the first place.

  2. Method of victory — P(KO/TKO), P(submission), P(decision), conditional on
     each fighter winning. Built from that fighter's own finish-rate history
     blended with the opponent's finish-*susceptibility* history (how often
     they've been finished that way). This is a categorical model, not a
     score distribution — closer in spirit to a classifier than to Dixon-Coles.

CHANGED 2026-07-14: the stats-based signal used to run on ufcstats.com's
per-minute striking/takedown stats (SLpM, TD accuracy, etc.). ufcstats.com
turned out to serve a JavaScript anti-bot challenge to any non-browser
client — not something this repo will try to defeat (see fetchers/ufc.py's
module docstring) — so fetchers/ufc.py switched to fightmatrix.com
(Elo-style ranking) + tapology.com (record/bio/history), neither of which
exposes that per-minute granularity. `stats_win_prob` now runs on
FightMatrix ranking (converted to an Elo-like rating the same way
analyze_esports.py turns a world ranking into a rating) plus reach and age
— a real signal, just less rich than the original plan.

Known v1 limitation (not fixed here): true "ring rust" needs days-since-last-
fight, which needs a per-event date lookup this fetcher doesn't do yet (see
fetchers/ufc.py: fetch_tapology_fight_history's docstring). Also, models/glicko.py's
RD only updates on a recorded win/loss — it does not grow RD for elapsed time
with no fights, so a 3-year-layoff fighter won't show extra uncertainty from
that alone. Both are open items, not silently papered over.
"""
from __future__ import annotations
import math
from typing import Optional

DEFAULT_GLICKO_RD = 350.0   # models/glicko.py DEFAULT_RD — max uncertainty (unrated fighter)
CONFIDENT_RD       = 60.0   # roughly what RD looks like after ~10+ recorded fights
RANKING_SCALE       = 1.0    # logistic scale on the ranking+reach+age composite score
REACH_COEF          = 0.015  # per inch of reach advantage
AGE_DECLINE_START    = 34.0  # UFC performance decline typically starts mid-30s
AGE_DECLINE_PER_YEAR = 0.03  # score penalty per year past AGE_DECLINE_START
MODEL_PROB_CAP       = 0.82  # provisional cap — MMA has bigger mismatches than
                              # team sports, but no fighter should be modeled
                              # as a near-lock; mirrors the baseball/rugby cap culture

LEAGUE_AVG_LOSS_KO_RATE  = 0.45  # provisional — rough share of UFC losses that are by KO/TKO
LEAGUE_AVG_LOSS_SUB_RATE = 0.20  # provisional — rough share of UFC losses that are by submission
LEAGUE_AVG_WIN_KO_RATE   = 0.45
LEAGUE_AVG_WIN_SUB_RATE  = 0.20


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _age_penalty(age: Optional[float]) -> float:
    if age is None or age <= AGE_DECLINE_START:
        return 0.0
    return (age - AGE_DECLINE_START) * AGE_DECLINE_PER_YEAR


def _rating_from_rank(rank: Optional[int]) -> Optional[float]:
    """
    Same formula analyze_esports.py's _elo_from_ranking uses: rank 1 ≈ 2200,
    floors at 1300. Returns None (not a default rating) for an unranked
    fighter — the composite score below treats "unranked" as no signal from
    this term, not as "bad," since plenty of real UFC fighters won't be in
    FightMatrix's top rankings.
    """
    if not rank or rank <= 0:
        return None
    return max(1300.0, 2200.0 - 400.0 * math.log10(max(1, rank)))


def stats_win_prob(fighter_a: dict, fighter_b: dict) -> float:
    """
    Logistic win probability for A from FightMatrix ranking + reach + age.
    Returns 0.5 when neither fighter has a usable ranking or reach/age
    signal (caller should treat that as "no signal", same as any other
    missing-data default in this repo).
    """
    rating_a = _rating_from_rank(fighter_a.get("fm_rank"))
    rating_b = _rating_from_rank(fighter_b.get("fm_rank"))
    rank_diff = 0.0
    if rating_a is not None and rating_b is not None:
        rank_diff = (rating_a - rating_b) / 400.0   # standard Elo scaling

    reach_adv = (fighter_a.get("reach_in") or 0.0) - (fighter_b.get("reach_in") or 0.0)
    age_pen = _age_penalty(fighter_b.get("age")) - _age_penalty(fighter_a.get("age"))

    score = rank_diff + reach_adv * REACH_COEF + age_pen
    return _sigmoid(score * RANKING_SCALE)


def composite_win_prob(
    fighter_a: dict, fighter_b: dict,
    glicko_prob_a: float, rd_a: float, rd_b: float,
) -> dict:
    """
    Blend Glicko-2 win probability with the stats-based logistic. Weight on
    Glicko-2 grows as both fighters' RD shrinks toward CONFIDENT_RD (i.e. as
    the rating becomes more trustworthy); weight on raw stats dominates for
    freshly-rated or long-inactive fighters (RD near DEFAULT_GLICKO_RD).
    """
    avg_rd = (rd_a + rd_b) / 2.0
    span = DEFAULT_GLICKO_RD - CONFIDENT_RD
    glicko_weight = max(0.0, min(1.0, (DEFAULT_GLICKO_RD - avg_rd) / span)) * 0.6
    stats_weight = 1.0 - glicko_weight

    stats_prob_a = stats_win_prob(fighter_a, fighter_b)
    blended_a = glicko_weight * glicko_prob_a + stats_weight * stats_prob_a
    return {
        "prob_a": blended_a,
        "prob_b": 1.0 - blended_a,
        "glicko_weight": glicko_weight,
        "stats_weight": stats_weight,
        "glicko_prob_a": glicko_prob_a,
        "stats_prob_a": stats_prob_a,
        "avg_rd": avg_rd,
    }


def method_of_victory(fighter: dict, opponent: dict) -> dict:
    """
    P(KO/TKO), P(submission), P(decision) conditional on `fighter` winning.
    Blends the fighter's own finish rate (from their wins) with the
    opponent's finish-*susceptibility* rate (from their losses) — 50/50 when
    both are available, falls back to the available one, falls back to
    league-average provisional rates when neither fighter has fight history.
    """
    def _blend(own: Optional[float], opp_susceptibility: Optional[float], league_avg: float) -> float:
        if own is None and opp_susceptibility is None:
            return league_avg
        if own is None:
            return opp_susceptibility
        if opp_susceptibility is None:
            return own
        return 0.5 * own + 0.5 * opp_susceptibility

    p_ko = _blend(fighter.get("win_ko_rate"), opponent.get("loss_ko_rate"), LEAGUE_AVG_WIN_KO_RATE)
    p_sub = _blend(fighter.get("win_sub_rate"), opponent.get("loss_sub_rate"), LEAGUE_AVG_WIN_SUB_RATE)

    # Normalize so p_ko + p_sub never exceeds 1.0, decision takes the residual.
    if p_ko + p_sub > 1.0:
        scale = 1.0 / (p_ko + p_sub)
        p_ko *= scale
        p_sub *= scale
    p_dec = max(0.0, 1.0 - p_ko - p_sub)

    return {"p_ko": p_ko, "p_sub": p_sub, "p_dec": p_dec}


def explain(
    fighter_a_name: str, fighter_b_name: str,
    blend: dict, method_a: dict, method_b: dict,
) -> str:
    return (
        f"Glicko-2/stats blend ({blend['glicko_weight']*100:.0f}% Glicko-2 / "
        f"{blend['stats_weight']*100:.0f}% stats, avg RD={blend['avg_rd']:.0f}): "
        f"{fighter_a_name} {blend['prob_a']*100:.1f}% / {fighter_b_name} {blend['prob_b']*100:.1f}%. "
        f"If {fighter_a_name} wins: {method_a['p_ko']*100:.0f}% KO/TKO, "
        f"{method_a['p_sub']*100:.0f}% submission, {method_a['p_dec']*100:.0f}% decision. "
        f"If {fighter_b_name} wins: {method_b['p_ko']*100:.0f}% KO/TKO, "
        f"{method_b['p_sub']*100:.0f}% submission, {method_b['p_dec']*100:.0f}% decision."
    )
