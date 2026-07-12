"""
AI orchestration layer for baseball.
Uses Claude to parse queries and generate prediction narratives.
"""
from __future__ import annotations
import json
import os
import re

import anthropic
from ai_client import get_client

MODEL = "claude-haiku-4-5-20251001"


def _client() -> anthropic.Anthropic:
    return get_client()


# ── 1. Parse query ────────────────────────────────────────────────────────────

PARSE_SYSTEM = """You extract structured MLB baseball game information from a user query.
Return ONLY valid JSON with these keys:
  team_a           — home team (or first team mentioned if home/away unclear)
  team_b           — away team (or second team mentioned)
  date             — ISO date YYYY-MM-DD, or null to mean today
  odds_a_american  — American odds for team_a if mentioned (e.g. -130, +110), or null
  odds_b_american  — American odds for team_b if mentioned, or null
  notes            — any extra context (weather, injuries, series info)

CRITICAL — team_a/team_b must be COPIED VERBATIM from the query, exact
substring, same characters, same abbreviation. Do NOT expand an abbreviation
to a full name, do NOT substitute a different or "more likely" team, do NOT
guess which real franchise a short code refers to — a separate system
resolves the real team from whatever string you return, so your only job is
extraction, not identification. If the query says "CHW", return "CHW" — never
substitute a different team like "Washington Nationals" for it. If it says
"A's", return "A's", not "Athletics" or any other team.

Examples:
  "A's vs CHW today"             → team_a: "A's", team_b: "CHW"
  "NYY -130 vs BOS +110"         → team_a: "NYY", team_b: "BOS", odds_a_american: -130, odds_b_american: 110
  "HOU -125, TOR +105"           → team_a: "HOU", team_b: "TOR", odds_a_american: -125, odds_b_american: 105
  "LAD is -145 favorite"         → odds_a_american: -145, odds_b_american: null
  "Yankees vs Red Sox tonight"   → team_a: "Yankees", team_b: "Red Sox", odds_a_american: null, odds_b_american: null

No markdown, no prose — raw JSON only."""


def _plausible_team_match(parsed_val: str, user_text: str, teams: list[dict]) -> bool:
    """
    True if parsed_val is a plausible identification of a team mentioned in
    user_text — not necessarily verbatim.

    Two text-heuristic versions of this check were tried and both broke on
    real traffic (2026-07-11): an exact-substring requirement rejected every
    query where the model reasonably expanded a nickname to the full name
    ("Yankees" -> "New York Yankees"); a word-overlap version fixed that but
    still rejected the single most common pattern in this app — a 3-letter
    abbreviation expanding to a full name ("NYY" -> "New York Yankees",
    "A's" -> "Athletics") shares no words at all with the abbreviation.

    Instead of inventing more string heuristics, this reuses the same ESPN
    team-matching code (_match_team, with its alias table) that the rest of
    the pipeline already relies on: resolve parsed_val to a real team id, then
    check whether ANY word or adjacent-word-pair in the raw query resolves to
    that SAME id via the identical matcher. Abbreviation, nickname, and full
    name all normalize to one id through the one matcher, so this correctly
    accepts every surface form Claude might reasonably use while still
    rejecting a true hallucination like "CHW" -> "Washington Nationals" (no
    token in "A's vs CHW today" resolves to the Nationals' id).

    teams is fetched once by the caller and reused across both team_a/team_b
    checks. Fails open (returns True, i.e. doesn't block) if ESPN is
    unreachable (teams empty) or the parsed value itself doesn't resolve to
    any known team — a network hiccup here must never be able to block every
    query.
    """
    from fetchers.baseball import _match_team

    if not teams:
        return True

    claimed = _match_team(parsed_val, teams)
    if not claimed:
        return True  # can't resolve the claim itself — not this check's job to flag that
    claimed_id = claimed.get("id")

    tokens = re.findall(r"[A-Za-z']+", user_text)
    candidates = set(tokens)
    candidates.update(f"{tokens[i]} {tokens[i + 1]}" for i in range(len(tokens) - 1))
    for cand in candidates:
        m = _match_team(cand, teams)
        if m and m.get("id") == claimed_id:
            return True
    return False


def parse_baseball_query(user_text: str) -> dict:
    client = _client()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=256,
        system=PARSE_SYSTEM,
        messages=[{"role": "user", "content": user_text}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    parsed = json.loads(raw)

    # Safety net for a confirmed failure mode (2026-07-11): the model can
    # substitute a different real team for an abbreviation it misreads
    # (caught live: "CHW" in "A's vs CHW today" came back as "Washington
    # Nationals" — a team with no textual resemblance to "CHW" at all, pure
    # hallucination, not a lookup bug). See _plausible_team_match's docstring
    # for why this validates via the real ESPN team matcher rather than a
    # text heuristic — two text-heuristic attempts before this one both
    # broke normal queries.
    try:
        from fetchers.baseball import _all_teams
        teams = _all_teams()
    except Exception:
        teams = []  # ESPN unreachable — skip validation rather than block on a network hiccup

    for key in ("team_a", "team_b"):
        val = str(parsed.get(key, "")).strip()
        if val and not _plausible_team_match(val, user_text, teams):
            raise ValueError(
                f"Query parser returned {key}={parsed.get(key)!r}, which doesn't "
                f"share any real overlap with the original query {user_text!r} — "
                f"likely a hallucinated team substitution, not a real extraction."
            )
    return parsed


# ── 2. Fallback signal estimation (when MLB API is unreachable) ───────────────

SIGNALS_SYSTEM = """You are a baseball analytics assistant. Given two MLB teams, estimate the
key model inputs using your training knowledge (roster data, season stats, pitcher rotations).

Return ONLY valid JSON with this exact schema:
{
  "team_a": {
    "wrc_plus": <int — team offensive wRC+, 100=league avg>,
    "starter_name": <string or "Unknown">,
    "starter_fip": <float — starter's estimated FIP, use 4.0 if unknown>,
    "starter_era": <float>,
    "team_era": <float>,
    "runs_per_game": <float>,
    "record_win_pct": <float 0-1>
  },
  "team_b": { <same keys> },
  "park_factor": <float — venue run factor, 1.0 if neutral/unknown>,
  "home_team": <"team_a" or "team_b">,
  "confidence": <"low"|"medium">,
  "notes": <string — what is from training knowledge vs live data>
}
Lower FIP = better pitcher (league avg ≈ 4.00). wRC+ 110 = 10% above avg offense.
No markdown — raw JSON only."""


def interpret_baseball_signals(team_a: str, team_b: str, notes: str = "") -> dict:
    """Fallback when MLB API is unreachable — Claude estimates signals from training knowledge."""
    client = _client()
    prompt = f"""
Matchup: {team_a} vs {team_b}
Extra context: {notes or 'none'}

Estimate baseball model signals for this matchup."""

    msg = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=SIGNALS_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


# ── 3. Prediction narrative ───────────────────────────────────────────────────

NARRATIVE_SYSTEM = """You are a sharp baseball prediction analyst. Write a clear, confident 2–3 sentence
prediction summary.

Rules:
- State the favourite and their win probability as a percentage
- Lead with the most important factor: starting pitcher FIP matchup, team wRC+, or park factor
- If a starter is named (not TBD), mention them by name with their FIP
- End with a one-sentence uncertainty caveat (weather, bullpen, small sample, etc.)
- Tone: analytical, no hype. No emojis. Under 90 words total."""


def generate_baseball_narrative(
    team_home: str,
    team_away: str,
    prob_home: float,
    prob_away: float,
    explanation: str,
    context: dict,
) -> str:
    client = _client()

    game     = context.get("game", {})
    ta       = context.get("team_a", {})
    tb       = context.get("team_b", {})
    is_home_a = ta.get("is_home", True)
    starter_home = (ta if is_home_a else tb).get("starter", {})
    starter_away = (tb if is_home_a else ta).get("starter", {})
    hit_home     = (ta if is_home_a else tb).get("hitting", {})
    hit_away     = (tb if is_home_a else ta).get("hitting", {})

    prompt = f"""
MLB matchup: {team_home} (home) vs {team_away} (away)
Win probabilities: {team_home} {prob_home*100:.1f}%  {team_away} {prob_away*100:.1f}%
Expected runs: home {context.get('mu_home','?')}, away {context.get('mu_away','?')}
{team_home} starter: {starter_home.get('name','TBD')} — FIP {starter_home.get('fip','?')}, ERA {starter_home.get('era','?')}, K/9 {starter_home.get('k9','?')}
{team_away} starter: {starter_away.get('name','TBD')} — FIP {starter_away.get('fip','?')}, ERA {starter_away.get('era','?')}, K/9 {starter_away.get('k9','?')}
{team_home} offense: wRC+ {hit_home.get('wrc_plus','?')}, OPS {hit_home.get('ops','?')}
{team_away} offense: wRC+ {hit_away.get('wrc_plus','?')}, OPS {hit_away.get('ops','?')}
Venue: {game.get('venue','?')} (park factor {game.get('park_factor',1.0)})
Model notes: {explanation}

Write the prediction narrative."""

    msg = client.messages.create(
        model=MODEL,
        max_tokens=200,
        system=NARRATIVE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()
