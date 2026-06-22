"""
Glicko-2 wrapper using the `glicko2` PyPI package.
Handles table tennis and tennis (per surface) ratings.
"""
from __future__ import annotations
from datetime import datetime, timezone

try:
    import glicko2
    _HAS_GLICKO = True
except ImportError:
    _HAS_GLICKO = False

from db.database import get_db

DEFAULT_RATING = 1500.0
DEFAULT_RD = 350.0
DEFAULT_VOL = 0.06
SURFACES = ("all", "hard", "clay", "grass")


class Glicko2Model:
    def _check_dep(self) -> None:
        if not _HAS_GLICKO:
            raise RuntimeError(
                "glicko2 package not installed. Run: pip install glicko2"
            )

    def get_rating(
        self, participant: str, sport: str, surface: str = "all"
    ) -> dict:
        with get_db() as conn:
            row = conn.execute(
                """SELECT rating, rd, volatility FROM glicko2_ratings
                   WHERE participant=? AND sport=? AND surface=?""",
                (participant, sport, surface),
            ).fetchone()
        if row:
            return {"rating": row["rating"], "rd": row["rd"], "volatility": row["volatility"]}
        return {"rating": DEFAULT_RATING, "rd": DEFAULT_RD, "volatility": DEFAULT_VOL}

    def set_rating(
        self,
        participant: str,
        sport: str,
        surface: str,
        rating: float,
        rd: float,
        volatility: float,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute(
                """INSERT INTO glicko2_ratings
                       (participant, sport, surface, rating, rd, volatility, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(participant, sport, surface)
                   DO UPDATE SET rating=excluded.rating, rd=excluded.rd,
                       volatility=excluded.volatility, updated_at=excluded.updated_at""",
                (participant, sport, surface, rating, rd, volatility, now),
            )

    def win_probability(
        self, participant_a: str, participant_b: str, sport: str, surface: str = "all"
    ) -> tuple[float, float]:
        """Approximate win probability from rating difference (logistic)."""
        self._check_dep()
        a = self.get_rating(participant_a, sport, surface)
        b = self.get_rating(participant_b, sport, surface)
        player_a = glicko2.Player(rating=a["rating"], rd=a["rd"], vol=a["volatility"])
        # Use Glicko-2 expected score as win probability
        import math
        q = math.log(10) / 400
        g_rd_b = 1.0 / math.sqrt(1 + 3 * (q * b["rd"]) ** 2 / (math.pi ** 2))
        e = 1.0 / (1.0 + 10 ** (-g_rd_b * (a["rating"] - b["rating"]) / 400))
        return e, 1.0 - e

    def update(
        self,
        winner: str,
        loser: str,
        sport: str,
        surface: str = "all",
    ) -> None:
        """Update ratings: winner beat loser (score=1 for winner)."""
        self._check_dep()
        w = self.get_rating(winner, sport, surface)
        l = self.get_rating(loser, sport, surface)

        wp = glicko2.Player(rating=w["rating"], rd=w["rd"], vol=w["volatility"])
        lp = glicko2.Player(rating=l["rating"], rd=l["rd"], vol=l["volatility"])

        wp.update_player([l["rating"]], [l["rd"]], [1])
        lp.update_player([w["rating"]], [w["rd"]], [0])

        self.set_rating(winner, sport, surface, wp.rating, wp.rd, wp.vol)
        self.set_rating(loser, sport, surface, lp.rating, lp.rd, lp.vol)

    def explain(
        self, participant_a: str, participant_b: str, sport: str, surface: str = "all"
    ) -> str:
        a = self.get_rating(participant_a, sport, surface)
        b = self.get_rating(participant_b, sport, surface)
        p_a, _ = self.win_probability(participant_a, participant_b, sport, surface)
        surface_str = f" ({surface})" if surface != "all" else ""
        return (
            f"Glicko-2{surface_str}: {participant_a}={a['rating']:.0f}±{a['rd']:.0f}, "
            f"{participant_b}={b['rating']:.0f}±{b['rd']:.0f}. "
            f"Estimated win probability for {participant_a}: {p_a*100:.1f}%."
        )
