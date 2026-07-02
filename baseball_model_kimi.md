# Baseball Model — for Kimi

> **Purpose of this file:** a single, self-contained briefing so Kimi can evaluate
> Predicta's baseball model **without reading the source files**. It is a living
> document — we update it every time we change the model. Paste this whole file
> into a fresh Kimi chat.
>
> **Last updated:** 2026-07-02
> **Repo:** rewired89/Predicta · branch `main`

---

## 0. TL;DR

Predicta predicts MLB games three ways: **full-game moneyline**, **first-5-innings
(F5) moneyline**, and **NRFI/YRFI** (does anyone score in the 1st inning?).

- Moneyline / F5 use a **split-Poisson run model** blended with **Elo**.
- NRFI uses a separately trained **XGBoost model with probability calibration**,
  validated at **54.9% win rate, p = 0.0049** across ~8,644 historical games.
- Live picks are auto-generated daily and committed to the repo. We just added
  **Closing Line Value (CLV) tracking** and a **public API** to make it sellable.

**Honest status:** the NRFI model is historically validated but has **zero
resolved live predictions yet** — the live track record starts now.

---

## 1. What it predicts (the markets)

| Market | Question | Model |
|--------|----------|-------|
| Moneyline | Who wins the game? | Split Poisson + Elo blend |
| First 5 innings (F5) | Who leads after 5 innings? | Split Poisson (starter-weighted) |
| NRFI / YRFI | Does *anyone* score in the 1st inning? | XGBoost + calibration |

---

## 2. Data sources

| Source | Provides | Status |
|--------|----------|--------|
| **ESPN** (primary) | Team records, hitting stats, team ERA, probable starters, park factor, schedule | Live, reliable |
| **FanGraphs** (pybaseball) | Pitcher SIERA/xFIP/CSW%/O-Swing%, team wRC+/wOBA/ISO | Enrichment; **proxy-blocked in prod** → falls back to ESPN |
| **Baseball Savant** (pybaseball) | Barrel% against, xwOBA, fastball velo, whiff% | Enrichment; same fallback |
| **MLB Stats API** | 1st-inning linescores (for grading), confirmed lineups | Used by the daily automation |
| **The Odds API** | First-inning NRFI/YRFI betting lines (for CLV) | Needs `ODDS_API_KEY`; optional |
| **OpenWeatherMap** | Temperature + wind for outdoor parks | Needs `OPENWEATHER_API_KEY`; optional |
| **Claude (AI)** | Query parsing; fallback stat estimates when ESPN is down | Live |

Fallback philosophy: if a live source fails, the model degrades gracefully to the
next-best source and **lowers its confidence** rather than refusing to answer.

---

## 3. How the run model works (moneyline & F5)

Runs are modeled as **Poisson-distributed**. Each half-game is split into two
segments so the starting pitcher and the bullpen are priced separately:

- **F5 (innings 1–5):** `offense (wRC+) × opposing starter quality (FIP/SIERA) ×
  park factor × home edge × weather × platoon × pitch-process adjustment`
- **L4 (innings 6–9):** `offense × derived bullpen FIP × park factor ×
  rest/fatigue multiplier`

Key adjustments layered in:
- **wRC+** scales offense vs league average (100).
- **Platoon:** right/left-handed starter vs lineup handedness.
- **Pitch process:** CSW% (called+swinging strikes), fastball velocity, O-Swing%
  each nudge expected runs ±2–5% beyond what FIP alone captures.
- **Bullpen FIP** is derived from team ERA minus the starter's share of innings.
- **Weather:** temp deviation from 72°F + wind direction/speed → a multiplier
  bounded to [0.85, 1.15]; domes are locked to neutral.
- **Park factor:** per-stadium historical run environment (e.g. Coors ≈ 1.15).

Total expected runs → win probability, then **blended 70% model / 30% Elo**
(Elo seeded from current-season win %).

---

## 4. How the NRFI model works

A two-stage trained pipeline (separate from the run model):

1. **XGBoost classifier** — trained on **2022–2025 MLB** first-inning outcomes,
   **32 features**.
2. **Platt scaling (logistic calibration)** — converts raw scores into
   well-calibrated probabilities.

**The 32 features:** home/away starter first-inning run rate, barrel% against,
hard-hit% against, whiff%, fastball velo; each starter's SIERA, xFIP, FIP, CSW%,
O-Swing%, K%, BB%, GB%, HR/FB%; home/away top-3 lineup wRC+; park factor; dome flag.

**Validation (walk-forward — train on older years, test on newer, rolling):**
- Win rate: **54.9%** at the 55% confidence threshold
- p-value: **0.0049** (≈ 0.5% chance the edge is luck)
- Break-even at standard −110 juice is 52.4%, so 54.9% is a real edge *if it holds live*.

If the trained model file is missing, the system falls back to a rough Poisson
approximation and clearly labels it as unvalidated.

---

## 5. Bet selection & staking

**NRFI verdict rules:** BET when model ≥ 55% **and** at least one starter shows an
elite signal (CSW% > 30% or barrel% < 6.5%); LEAN when ≥ 55% without that signal;
otherwise SKIP. **Staking:** Kelly criterion, half-Kelly recommended, paper-mode
only (no real bets placed by the system).

---

## 6. Data confidence (recently fixed)

Every prediction now carries a **data-confidence label** based on how much input
is real vs default:

- **low** — data source down (AI-estimated), OR **both probable starters TBD**
  (common when you ask about *tomorrow's* games before lineups post)
- **medium** — one starter TBD, missing team wRC+, or < 10 games played
- **high** — both starters named with FIP + real wRC+ + ≥ 10 games played

*Practical note:* querying next-day games often yields **low** confidence until
starters are confirmed on game-day morning. This is intended behavior.

---

## 7. Closing Line Value (CLV) tracking (new)

CLV measures whether the betting market moved **toward our pick** after we made it
— the sharpest proof of edge because it accumulates every game, win or lose.

- Capture the NRFI line at **pick time** (entry) and again **near first pitch**
  (closing), preferring Pinnacle.
- `clv_pp` = vig-free closing probability − entry probability, on our bet side.
  Positive = we were early = we beat the close.
- Stored in the daily reports and the database; exposed via the API.

---

## 8. Automation & delivery

- **GitHub Actions** runs daily: predictions at 9 AM ET, closing-line capture at
  7 PM ET, results graded at 1 AM ET. Everything is committed to the repo as
  JSON + a Markdown report (auditable, timestamped).
- **Public API** (`/v1/...`, API-key gated) serves predictions, plays, CLV, and a
  performance summary from those committed files — for syndicate/media clients.

---

## 9. Known limitations (be honest with Kimi)

1. **Zero resolved live predictions** — historical validation only; live track
   record is just starting.
2. **FanGraphs/Savant blocked in production** — advanced pitcher metrics often
   fall back to ESPN's simpler FIP.
3. **Weather + lineup APIs** need keys and are sometimes proxy-blocked.
4. **Model retrain pending** — the improved rolling-window feature (below) needs a
   local retrain before it takes effect.
5. **sklearn version mismatch warning** on the calibration file (non-breaking).

---

## 10. Recent changes (newest first)

- **2026-07-02** — Fixed baseball `data_confidence` (was hardcoded "medium" for
  every game); now derived from real data quality.
- **2026-07-02** — Rewrote the starter first-inning-rate feature to a **short
  rolling window**: last 6 starts, 21-day half-life decay, shrinkage toward league
  mean. (Needs local retrain to activate.)
- **2026-07-02** — Added **CLV tracking** (entry vs closing line) end-to-end.
- **2026-07-02** — Added **public `/v1` API** with API-key auth.
- **2026-07-02** — Added the **daily automation** pipeline (predict / capture / grade).

---

## 11. Open questions for Kimi

1. **CLV method.** We measure edge as *entry-line vs closing-line movement on our
   side* (we're paper, so we have no real bet price). Is that the right proxy, or
   should we instead compare *our model probability vs the closing fair
   probability*? Which is more defensible to a sharp buyer?

2. **Rolling window size.** Starters pitch every ~5 days, so 6 starts ≈ 30 days.
   Is a 6-start window with a 21-day half-life the right recency trade-off for
   MLB, or too short/too long?

3. **Honest first claim.** With zero resolved live predictions, what is the
   strongest *honest* thing we can tell a first paying customer?

4. **Sequencing.** Ship baseball-only now, or first fix sibling pipelines (e.g.
   soccer currently predicts even when it has no data)?

---

*To extend Predicta's model briefings, create sibling files: `soccer_model_kimi.md`,
`tennis_model_kimi.md`, etc., in the same format.*
