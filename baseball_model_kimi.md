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

- Capture the NRFI line at **pick time** (entry, 9 AM ET) and again **near first
  pitch** (closing, 7 PM ET), preferring Pinnacle.
- `clv_pp` = vig-free closing probability − entry probability, on our bet side.
  Positive = we were early = we beat the close.
- Stored in the daily reports and the database; exposed via the API.

**Timing safeguard (added per Kimi's caveat).** CLV only means "we led the
market" if the entry line was captured *before* the sharps moved it. So every
entry now records `entry_hours_to_fp` (hours before first pitch) and an
`entry_stale` flag. Entries captured **< 3 hours** before first pitch are
**excluded from the headline CLV** and reported separately (`n_stale_excluded`),
and the API reports `avg_entry_lead_hrs` so the number is auditable. For typical
7 PM ET games the 9 AM ET entry is ~10h early (good); afternoon games get less
lead and may be flagged. **Still to verify with live data:** that our 9 AM entry
is genuinely ahead of the bulk of same-day line movement.

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

## 9a. How we talk about performance (honest claims)

Adopted from Kimi. With zero resolved live predictions, the **only** claims we
make — and that the API enforces via a state-aware `disclaimer` field on every
performance response:

1. **Historically validated:** "Trained on 2022–2025; 54.9% win rate at the 55%
   threshold, p=0.0049. Break-even at −110 is 52.4%, so a ~2.5pp historical margin."
2. **Tracked in real time, in public:** "Every pick, its confidence, and its
   closing-line value are published before the game — auditable in our commit history."
3. **Transparent about the unknown:** "We have NOT proven this edge live yet.
   We're building the track record in public."

**Never say:** "our model wins," "profitable system," "beat the market,"
"guaranteed ROI." Sell **access + transparency**, not guaranteed profit.

---

## 9b. Live Validation Tracker

Rendered at the top of every daily report (cumulative, auto-updated). Current:

```
Resolved predictions : 0
Win rate             : —
CLV-quality verdict  : INCONCLUSIVE (awaiting ~30–50 games)
Avg entry lead time  : 15.3 hrs (n=9, first live capture)
Avg CLV              : — (first close not yet captured)
Beat the close       : —
Stale exclusions     : 0
```

## 9c. CLV math notes (pre-answers to Kimi's review)

- **No push at the 0.5 line.** First-inning runs are integers: 0 → Under 0.5 =
  NRFI; 1+ → Over 0.5 = YRFI. Nothing lands *on* 0.5, so there is no push to
  handle. (Push only arises on an integer line like 1.0 — we deliberately use 0.5.)
- **Devig is proportional (multiplicative):** `fair = (1/dec) / Σ(1/dec)`. It is
  **symmetric** by construction — a −120/+100 pair and its mirror +100/−120 both
  yield NRFI/YRFI fair = 0.5217 (verified).
- **CLV is measured on the bet side** as vig-free closing prob − vig-free entry
  prob; entries < 3h before first pitch are excluded from the headline.

## 10. Recent changes (newest first)

- **2026-07-02** — Added **Live Validation Tracker** to every daily report + a
  `nrfi_store.validation_tracker()` snapshot (resolved count, win rate, CLV-quality
  verdict, avg entry lead time, avg CLV, beat-close %, stale exclusions).
- **2026-07-02** — Added **CLV-vs-win-rate diagnostic** (`/v1/nrfi/clv-quality`):
  buckets resolved plays by CLV and significance-tests whether higher CLV
  actually tracks higher win rate. Flags "line-chasing" if positive CLV does not
  predict winners (Kimi's subtle-risk watch-item #3, now instrumented).
- **2026-07-02** — Added CLV **timing safeguard**: entry lines now record hours-
  before-first-pitch and a stale flag; headline CLV excludes entries captured
  < 3h before first pitch (so it measures *leading* the market, not moving with it).
- **2026-07-02** — Added a state-aware **honesty disclaimer** to the performance
  API so results can't be quoted as a profit claim before the edge is proven.
- **2026-07-02** — Fixed baseball `data_confidence` (was hardcoded "medium" for
  every game); now derived from real data quality.
- **2026-07-02** — Rewrote the starter first-inning-rate feature to a **short
  rolling window**: last 6 starts, 21-day half-life decay, shrinkage toward league
  mean. (Needs local retrain to activate.)
- **2026-07-02** — Added **CLV tracking** (entry vs closing line) end-to-end.
- **2026-07-02** — Added **public `/v1` API** with API-key auth.
- **2026-07-02** — Added the **daily automation** pipeline (predict / capture / grade).

---

## 11. Decisions (resolved with Kimi) + what to watch

1. **CLV method → DECIDED: Way A** (track line movement toward our pick).
   *Watch:* verify entry lines aren't stale — handled by the timing safeguard in
   §7. If live data shows our 9 AM entry is already post-movement, revisit.

2. **Rolling window → DECIDED: ship 6 starts / 21-day half-life.**
   *Validation plan:* after retrain + ~200 live predictions, A/B the new feature
   vs the old season-to-date feature; keep whichever performs better. Don't
   over-tune pre-launch.

3. **First-customer claim → DECIDED:** the three honest claims in §9a, enforced
   by the API disclaimer. Sell access + transparency, never guaranteed profit.

4. **Sequencing → DECIDED: ship baseball-only.** Soccer sits behind "coming soon"
   until its data pipeline is reworked and given a *refuse-to-predict* gate when
   data quality is too low (it currently predicts on empty data — reputationally
   risky). Tracked as the next engineering project, not a launch blocker.

### Watch-items (from Kimi — status)
1. **9 AM ET actually beats the market** — instrumented (`avg_entry_lead_hrs`,
   stale-exclusion). Confirm with first week of live data.
2. **Rolling-window A/B after retrain** — planned; ~200 live predictions post-retrain.
3. **CLV actually predicts wins** — instrumented (`/v1/nrfi/clv-quality`,
   significance-tested). Read after ~50–100 resolved games.
4. **Soccer refuse-to-predict gate** — not started; deferred until soccer is picked back up.

### Still open for Kimi
- Review the actual CLV math / API JSON for soundness (samples can be shared once
  `ODDS_API_KEY` is set and real lines flow).

---

*To extend Predicta's model briefings, create sibling files: `soccer_model_kimi.md`,
`tennis_model_kimi.md`, etc., in the same format.*
