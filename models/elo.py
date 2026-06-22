"""
World Football Elo methodology with goal-difference multiplier.
K is scaled by match importance; G scales by goal margin.
"""
from __future__ import annotations
import math
from datetime import datetime, timezone
from db.database import get_db


IMPORTANCE_K = {
    "world_cup_final": 60,
    "world_cup": 60,
    "continental_final": 50,
    "continental": 45,
    "world_cup_qualifier": 40,
    "continental_qualifier": 35,
    "friendly_major": 30,
    "friendly": 20,
}
DEFAULT_K = 30
DEFAULT_RATING = 1500.0


def _goal_diff_multiplier(goal_diff: int) -> float:
    gd = abs(goal_diff)
    if gd <= 1:
        return 1.0
    if gd == 2:
        return 1.5
    return (11 + gd) / 8.0


def _expected(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


class EloModel:
    def get_rating(self, team: str) -> float:
        with get_db() as conn:
            row = conn.execute(
                "SELECT rating FROM elo_ratings WHERE team = ?", (team,)
            ).fetchone()
            return row["rating"] if row else DEFAULT_RATING

    def set_rating(self, team: str, rating: float) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute(
                """INSERT INTO elo_ratings (team, rating, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(team) DO UPDATE SET rating=excluded.rating, updated_at=excluded.updated_at""",
                (team, rating, now),
            )

    def win_probability(self, team_a: str, team_b: str) -> tuple[float, float]:
        """Return (prob_a_wins, prob_b_wins) ignoring draw."""
        ra = self.get_rating(team_a)
        rb = self.get_rating(team_b)
        p_a = _expected(ra, rb)
        return p_a, 1.0 - p_a

    def update(
        self,
        team_a: str,
        team_b: str,
        score_a: int,
        score_b: int,
        importance: str = "default",
    ) -> tuple[float, float]:
        """Update ratings after a result; return new (rating_a, rating_b)."""
        ra = self.get_rating(team_a)
        rb = self.get_rating(team_b)
        k = IMPORTANCE_K.get(importance, DEFAULT_K)
        g = _goal_diff_multiplier(score_a - score_b)
        exp_a = _expected(ra, rb)

        if score_a > score_b:
            actual_a = 1.0
        elif score_a < score_b:
            actual_a = 0.0
        else:
            actual_a = 0.5

        new_ra = ra + k * g * (actual_a - exp_a)
        new_rb = rb + k * g * ((1.0 - actual_a) - (1.0 - exp_a))
        self.set_rating(team_a, new_ra)
        self.set_rating(team_b, new_rb)
        return new_ra, new_rb

    def explain(self, team_a: str, team_b: str) -> str:
        ra = self.get_rating(team_a)
        rb = self.get_rating(team_b)
        diff = ra - rb
        p_a, _ = self.win_probability(team_a, team_b)
        direction = "advantage" if diff > 0 else "deficit"
        return (
            f"Elo ratings: {team_a}={ra:.0f}, {team_b}={rb:.0f} "
            f"({abs(diff):.0f}-point {direction} for {team_a}). "
            f"Model win probability for {team_a}: {p_a*100:.1f}%."
        )
