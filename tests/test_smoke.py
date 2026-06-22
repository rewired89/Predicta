"""
Smoke tests for core algorithms.
Run: python -m pytest tests/ -v
Or:  python tests/test_smoke.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import math
import tempfile
from pathlib import Path


def setup_temp_db():
    """Point the DB at a temp file so tests don't pollute predicta.db."""
    import db.database as db_mod
    tmp = tempfile.mktemp(suffix=".db")
    db_mod.DB_PATH = Path(tmp)
    db_mod.init_db(Path(tmp))
    return tmp


def test_devig_argentina_austria():
    """FanDuel -185/+310/+550 → vig ~1.046, fair probs sum exactly 1.0."""
    from models.devig import american_to_decimal, devig_market

    prices = {
        "a":    american_to_decimal(-185),   # 1.5405
        "draw": american_to_decimal(310),    # 4.10
        "b":    american_to_decimal(550),    # 6.50
    }
    fair = devig_market(prices)
    total = sum(fair.values())
    assert abs(total - 1.0) < 1e-9, f"Fair probs sum {total} ≠ 1.0"
    # Argentina should be favourite
    assert fair["a"] > fair["draw"] > fair["b"], "Order mismatch"
    # Rough range check (vig ~4.6%)
    assert 0.60 < fair["a"] < 0.65
    assert 0.22 < fair["draw"] < 0.25
    assert 0.13 < fair["b"] < 0.16


def test_elo_equal_ratings():
    """Two equal Elo ratings → expected result exactly 0.5."""
    from models.elo import _expected
    assert _expected(1500.0, 1500.0) == 0.5


def test_elo_asymmetric():
    """+100-point Elo advantage → ~64% expected win probability."""
    from models.elo import _expected
    e = _expected(1600.0, 1500.0)
    assert 0.63 < e < 0.65, f"Got {e}"


def test_elo_update_direction():
    """Winner gains rating, loser loses rating."""
    tmp = setup_temp_db()
    try:
        from models.elo import EloModel
        m = EloModel()
        m.set_rating("Alpha", 1500.0)
        m.set_rating("Beta", 1500.0)
        new_a, new_b = m.update("Alpha", "Beta", 2, 0)
        assert new_a > 1500, "Winner should gain Elo"
        assert new_b < 1500, "Loser should lose Elo"
        assert abs((new_a - 1500) + (new_b - 1500)) < 1e-9, "Elo is zero-sum"
    finally:
        os.unlink(tmp)


def test_dixon_coles_matrix_sums_to_one():
    """Score matrix (0-0 through 6-6) sums to ~1.0 for equal and skewed teams."""
    from models.dixon_coles import predict as dc_predict
    for attack_h, def_h, attack_a, def_a in [
        (1.0, 1.0, 1.0, 1.0),     # equal
        (1.5, 1.2, 0.8, 0.9),     # home-dominant
        (0.7, 0.8, 1.4, 1.3),     # away-dominant
    ]:
        r = dc_predict(attack_h, def_h, attack_a, def_a)
        total = r["prob_home"] + r["prob_draw"] + r["prob_away"]
        assert abs(total - 1.0) < 0.005, f"Matrix sum {total} for {attack_h}/{def_h} vs {attack_a}/{def_a}"


def test_dixon_coles_direction():
    """Strong home attack/defense → prob_home > prob_away."""
    from models.dixon_coles import predict as dc_predict
    r = dc_predict(attack_home=1.8, defense_home=0.6, attack_away=0.8, defense_away=1.4)
    assert r["prob_home"] > r["prob_away"], "Strong home team should win more often"


def test_dixon_coles_defense_sign():
    """Weak away defense (high xg_against) → more home goals, not fewer."""
    from models.dixon_coles import predict as dc_predict
    r_weak_away_def = dc_predict(1.2, 1.0, 1.0, 1.5)   # away concedes a lot
    r_strong_away_def = dc_predict(1.2, 1.0, 1.0, 0.7)  # away is miserly
    assert r_weak_away_def["mu_home"] > r_strong_away_def["mu_home"], (
        "Weak away defense should inflate home expected goals"
    )


def test_kelly_positive_edge():
    """70% chance at 2.0 decimal → full Kelly=40%, quarter-Kelly=100 on 1000 bankroll."""
    from models.kelly import kelly_stake
    r = kelly_stake(0.70, 2.0, 1000.0)
    assert abs(r["full_kelly_fraction"] - 0.40) < 1e-9
    assert abs(r["recommended_stake"] - 100.0) < 1e-6
    assert r["paper_mode"] is True


def test_kelly_negative_edge():
    """Below-break-even probability → zero stake."""
    from models.kelly import kelly_stake
    r = kelly_stake(0.30, 2.0, 1000.0)
    assert r["recommended_stake"] == 0.0
    assert r["edge"] < 0


def test_strengths_from_signals_direction():
    """Higher xg_for → higher attack; higher xg_against → weaker defense (>1.0 multiplier)."""
    from models.dixon_coles import strengths_from_signals
    strong = strengths_from_signals({"xg_for_avg5": 2.0, "xg_against_avg5": 0.8})
    weak   = strengths_from_signals({"xg_for_avg5": 0.9, "xg_against_avg5": 1.6})
    assert strong["attack"] > weak["attack"], "Higher xg_for should give higher attack"
    # Strong team has low xg_against → defense < 1.0 (suppresses opponent goals)
    # Weak team has high xg_against → defense > 1.0 (inflates opponent goals)
    assert strong["defense"] < 1.0
    assert weak["defense"] > 1.0


def test_argentina_austria_end_to_end():
    """
    Argentina/Austria on neutral ground with realistic international signals
    should produce probabilities in the range three independent sources agreed on:
    Argentina 60-68%, Draw 20-27%, Austria 12-18%.
    """
    tmp = setup_temp_db()
    try:
        import db.database as db_mod
        from db.database import get_db
        from engine import predict_match
        from fetchers.signals import log_signal
        from fetchers.odds import log_manual_odds
        from models.elo import EloModel
        from models.devig import american_to_decimal

        elo = EloModel()
        # Realistic international Elo (not club-football inflated)
        elo.set_rating("Argentina", 1980.0)
        elo.set_rating("Austria",   1760.0)

        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO matches (sport, league, participant_a, participant_b,
                       scheduled_at, venue, neutral_site)
                   VALUES ('soccer','International Friendly','Argentina','Austria',
                           '2024-06-01T15:00:00','Madrid',1)"""
            )
            mid = cur.lastrowid

        # International-calibrated xG (not club-level):
        for name, part, val in [
            ("xg_for_avg5",     "Argentina", 1.65),
            ("xg_against_avg5", "Argentina", 0.95),
            ("form_weighted10", "Argentina", 0.80),
            ("xg_for_avg5",     "Austria",   1.25),
            ("xg_against_avg5", "Austria",   1.30),
            ("form_weighted10", "Austria",   0.55),
        ]:
            log_signal(mid, name, part, signal_value=val)

        log_manual_odds(mid, "FanDuel", "moneyline",
            american_to_decimal(-185), american_to_decimal(550), american_to_decimal(310))

        r = predict_match(mid, bankroll=1000.0)

        assert "error" not in r, r.get("error")
        # Three independent sources agreed on ~60-65% Argentina; allow ±7% for model/signal variance
        assert 0.53 <= r["prob_a"] <= 0.72, f"Argentina prob {r['prob_a']*100:.1f}% out of 53-72% range"
        assert 0.17 <= r["prob_draw"] <= 0.30, f"Draw prob {r['prob_draw']*100:.1f}% out of 17-30% range"
        assert 0.09 <= r["prob_b"] <= 0.22, f"Austria prob {r['prob_b']*100:.1f}% out of 9-22% range"
        assert abs(r["prob_a"] + r["prob_b"] + r["prob_draw"] - 1.0) < 1e-9
        assert r["explanation"]  # must not be empty
    finally:
        os.unlink(tmp)


if __name__ == "__main__":
    tests = [
        test_devig_argentina_austria,
        test_elo_equal_ratings,
        test_elo_asymmetric,
        test_elo_update_direction,
        test_dixon_coles_matrix_sums_to_one,
        test_dixon_coles_direction,
        test_dixon_coles_defense_sign,
        test_kelly_positive_edge,
        test_kelly_negative_edge,
        test_strengths_from_signals_direction,
        test_argentina_austria_end_to_end,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed.")
    if failed:
        sys.exit(1)
