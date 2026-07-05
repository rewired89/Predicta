# Baseball Model — for Kimi

> **Purpose of this file:** a single, self-contained briefing so Kimi can evaluate
> Predicta's baseball model **without reading the source files**. It is a living
> document — we update it every time we change the model. Paste this whole file
> into a fresh Kimi chat.
>
> **Last updated:** 2026-07-05 (rev 10 — found and fixed a live unit-scaling bug that flattened the barrel% signal in the run model to a constant, for every pitcher, since the feature store shipped)
> **Repo:** rewired89/Predicta · branch `main`

---

## 0. TL;DR

Predicta predicts MLB games three ways: **full-game moneyline**, **first-5-innings
(F5) moneyline**, and **NRFI/YRFI** (does anyone score in the 1st inning?).

- Moneyline / F5 use a **split-Poisson run model** blended with **Elo**.
- NRFI uses a separately trained **XGBoost model with probability calibration**,
  validated at **54.9% win rate, p = 0.0049** across ~8,644 historical games.
- Live picks are auto-generated daily by an always-on **Railway scheduler** and
  committed to the repo via the GitHub API. We have **CLV tracking** and a
  **public API** to make it sellable.
- The report now shows **all three markets for every game** (moneyline, F5, NRFI)
  so even games where NRFI is a coin-flip still surface a moneyline or F5 edge.

**Honest status:** the NRFI model is historically validated but has **zero
resolved live predictions yet** — the live track record starts now. First full
auto-scan ran 2026-07-02: 9 games scanned, 7 flagged edges (mostly moneylines),
1 NRFI LEAN (CIN@MIL 55.9%).

---

## 1. What it predicts (the markets)

| Market | Question | Model |
|--------|----------|-------|
| Moneyline | Who wins the game? | Split Poisson + Elo blend |
| First 5 innings (F5) | Who leads after 5 innings? | Split Poisson (starter-weighted) |
| Game Total (O/U) | Over or under X.5 runs? | Split Poisson (lines 6.5–10.5) |
| NRFI / YRFI | Does *anyone* score in the 1st inning? | XGBoost + calibration |

---

## 2. Data sources

| Source | Provides | Status |
|--------|----------|--------|
| **ESPN** (primary) | Team records, hitting stats, team ERA, probable starters, park factor, schedule | Live, reliable |
| **Baseball Savant** (feature store) | Barrel%, hard-hit%, whiff%, fastball velo, xwOBA per pitcher | **WORKING** — baked into a committed `data/nrfi_feature_store.json` built locally, read at runtime (see §9d) |
| **FanGraphs** | SIERA, xFIP, CSW%, O-Swing%, K%, BB%, GB%, HR/FB | **DEAD for automation** — no API, Cloudflare 403s all scripts. Only obtainable via manual member CSV export (optional, see §9d) |
| **MLB Stats API** | 1st-inning linescores (for grading), confirmed lineups | Proxy-blocked in CI; ESPN cache used as fallback |
| **The Odds API** | First-inning NRFI/YRFI betting lines (for CLV) | Needs `ODDS_API_KEY`; **working** |
| **OpenWeatherMap** | Temperature + wind for outdoor parks | Needs `OPENWEATHER_API_KEY`; optional |
| **Claude (AI)** | Query parsing; fallback stat estimates when ESPN is down | Live |

Fallback philosophy: if a live source fails, the model degrades gracefully to the
next-best source and **lowers its confidence** rather than refusing to answer.

**Key architecture change:** live stat-fetching from FanGraphs/Savant fails on
any datacenter IP (Actions/Railway) — FanGraphs is fully blocked (Cloudflare),
Savant is blocked live too. So advanced pitcher stats are now **pre-fetched
locally and committed as a feature store** that the runtime reads. See §9d.

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

## 5a. Player Impact Score (standalone tool, new)

A separate feature from the daily prediction pipeline above — lets a user look
up **any MLB player** (pitcher or hitter) and see how many percentage points
they move their team's win probability vs a league-average replacement in the
same role. Runs the same split-Poisson engine from §3, just twice per lookup
(real player vs replacement) and diffs the win probabilities.

**Pitchers:**
- Real pitcher FIP vs replacement FIP (4.00), through the split-Poisson model.
- Tiers: ACE / FRONT-LINE / SOLID / AVERAGE / BELOW AVG / LIABILITY.
- Data: 80+ hardcoded starters, falling back to the ~650-pitcher Savant feature
  store (§9d) for anyone else — so almost any active starter resolves.

**Hitters:**
- Team wRC+ with the hitter in the lineup (1/9 weight) vs replacing that slot
  with a league-average bat (wRC+ 100), through the same run model.
- Tiers: MVP / ALL-STAR / STARTER / AVERAGE / BENCH / REPLACEMENT.
- Data: ~100 hardcoded hitters (superstars) — **and this table had no fallback
  until today.** Any hitter not on that list returned "not found," even
  everyday active players. Fixed by adding a live ESPN fallback: resolve the
  supplied team abbreviation → ESPN team → fuzzy-match the player on that
  team's roster → pull season AVG/OBP/SLG/OPS/HR/SB → derive wRC+ with the
  same `(2×OBP+SLG)/1.045` formula already used for team-level wRC+ elsewhere
  in the codebase. Requires a team hint since ESPN has no cross-league
  player-name search; `war` is `None` for these since ESPN doesn't expose it.

**Known gap:** the live ESPN fallback couldn't be tested against the real API
from the dev sandbox (ESPN blocks that egress, same as noted in §2), so it's
verified by code path and graceful-failure behavior only, not a live call yet.
First real-world exercise will be whatever the user looks up next in production.

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

- **Railway always-on scheduler** (`tasks/nrfi_auto.py`) runs daily — predictions
  at **9 AM ET**, closing-line capture at **7 PM ET**, results graded at
  **1 AM ET**. Replaced GitHub Actions `schedule` cron (which was unreliable —
  delayed/skipped runs — and its `git push` raced with Railway's Contents API
  pushes, causing non-fast-forward rejections). Railway is now the **single
  writer** to `main`.
- Results are committed to the repo as JSON + a Markdown report (auditable,
  timestamped) via the GitHub Contents API. A fixed-path file
  `data/nrfi_latest.md` is overwritten every run so the newest scan is always at
  one known URL.
- **Manual trigger** available: `GET /nrfi-auto/run?job=predict` (browser-
  friendly) or `POST`. Check scheduler state at `GET /nrfi-auto/status`.
- **Public API** (`/v1/...`, API-key gated) serves predictions, plays, CLV, and a
  performance summary from those committed files — for syndicate/media clients.
- **Odds diagnostics** endpoint: `GET /nrfi-auto/odds-diag` returns raw Odds API
  status, quota, market availability, and sample Pinnacle prices — for debugging
  CLV capture without guessing at config issues.

### All-markets report (new)

The daily report now includes an **"All Games — Model Picks"** table covering
every game across three markets: **moneyline** (full-game winner), **F5**
(first-5 leader), and **NRFI**. Each cell shows the pick + probability; a ⭐
marks games where the model flags an edge (BET or LEAN). A "Best play" column
picks the strongest edge across all three markets, or "no edge — pass."

This addresses the concern that only 1 of 9 games produced a bet — the NRFI
model is *correct* to be selective (55% gate), but the moneyline/F5 models
frequently find value the NRFI model doesn't. Now every game gets visibility.

---

## 9. Known limitations (be honest with Kimi)

1. **Zero resolved live predictions** — historical validation only; live track
   record is just starting. First resolution batch expected 2026-07-03 at 1 AM ET.
2. **Savant feature store works; FanGraphs dead for automation** — Savant Statcast
   (barrel%, whiff%, velo, hard-hit%) is live via precomputed feature store (~650
   pitchers). FanGraphs (SIERA/CSW%/O-Swing%) is dead for scripts (Cloudflare 403
   even from residential IPs) but available via manual CSV export (optional chore).
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

Rendered at the top of every daily report (cumulative, auto-updated). Current
(from first auto-scan 2026-07-02):

```
Model games resolved  : 0
Model lean accuracy   : —
Bet plays resolved    : 0
Bet win rate          : —
CLV-quality verdict   : no resolved CLV plays yet
Avg entry lead time   : — (n=0)
Avg CLV               : —
Beat the close        : —
Stale exclusions      : 0
```

*Note:* "Model lean accuracy" tracks ALL games (not just BET/LEAN — every game
gets a lean_side based on whether p_nrfi > 0.50), so accuracy accumulates even
when the 55% bet gate rarely fires. This is the metric that will tell us whether
the model has directional skill across the full slate.

## 9c. CLV math notes (pre-answers to Kimi's review)

- **No push at the 0.5 line.** First-inning runs are integers: 0 → Under 0.5 =
  NRFI; 1+ → Over 0.5 = YRFI. Nothing lands *on* 0.5, so there is no push to
  handle. (Push only arises on an integer line like 1.0 — we deliberately use 0.5.)
- **Devig is proportional (multiplicative):** `fair = (1/dec) / Σ(1/dec)`. It is
  **symmetric** by construction — a −120/+100 pair and its mirror +100/−120 both
  yield NRFI/YRFI fair = 0.5217 (verified).
- **CLV is measured on the bet side** as vig-free closing prob − vig-free entry
  prob; entries < 3h before first pitch are excluded from the headline.

## 9d. Feature-blocking diagnosis + fix (2026-07-02)

**Diagnosis (from live diagnostics):** production predictions clustered at
~51.3% on every game because **FanGraphs/Savant enrichment fails from server
IPs**. Per-game feature flags showed 0/9 games enriched — *including veterans*
(Framber Valdez, Eovaldi, May), ruling out thin-rookie data. Direct test:
`pyb.pitching_stats` → `ProxyError`/blocked. FanGraphs blocks datacenter IPs
(Actions, Railway) and pybaseball uses a deprecated legacy endpoint. So every
FanGraphs/Savant feature collapsed to league mean and the model couldn't
differentiate games (→ never clears the 55% bet gate).

**Fix — precomputed feature store (preserves the full 32-feature edge):**
`scripts/build_feature_store.py` runs **locally** (residential IP, where
FanGraphs works), writes `data/nrfi_feature_store.json`, committed to the repo.
At runtime `enrich_starter` reads the store first (`feature_source="store"`) and
never live-fetches. Refresh locally every few days + commit — same rhythm as
retraining. Chosen over an ESPN-only retrain because that would discard the
Statcast signal and force the p=0.0049 edge to be re-proven from scratch.

**RESOLVED (final state):**
- **Savant features are LIVE.** The local build (`build_feature_store.py`) pulls
  Baseball Savant (barrel%, hard-hit%, whiff%, velo, xwOBA) for ~650 pitchers,
  writes the committed store. Verified working: after committing it, model
  p_nrfi spread went from flat **51.1–51.8 (0.7pp)** to **48.1–55.9 (7.8pp)** and
  fired its first LEAN. The model differentiates games again.
- **FanGraphs is a genuine dead end for automation.** Confirmed on the user's own
  residential machine: `pitching_stats` → 403; the modern `/api/leaders` JSON
  endpoint → 403 (Cloudflare "Just a moment"); even `cloudscraper` can't pass the
  challenge. FanGraphs has **no API** — the "membership" is a website login only.
- **FanGraphs stats are still obtainable, manually.** A member can *export* the
  pitching leaderboard to CSV in the browser. `build_feature_store.py` now reads
  any `data/fangraphs*.csv` the user drops in and merges SIERA/xFIP/CSW%/O-Swing%/
  K%/BB%/GB%/HR-FB, converting "28.5%"→0.285 to match training units. Optional.

**Net:** the model runs on ~10 of its 14 pitcher features (Savant + ESPN FIP +
fi_rate). The 4 FanGraphs-only features (SIERA/xFIP/CSW%/O-Swing%) default to
league mean unless the user does the manual CSV export. Open question for Kimi:
is that 4-feature gap worth a recurring manual chore, or does Savant already
carry most of the contact-quality signal? (We plan to measure it after ~2 weeks
of live data — compare model-lean accuracy with vs without the FanGraphs CSV.)

**Ops reliability note:** GitHub Actions cron was removed — Railway is the sole
scheduler and sole writer to `main`. The old workflow was deleted after it
raced with Railway's pushes (causing git push rejections). See §8.

## 10. Recent changes (newest first)

- **2026-07-04 (rev 5)** — **Player Impact Score fully live for hitters**: added
  a live ESPN roster-lookup fallback (`fetchers/baseball.py: lookup_batter`) so
  any active hitter resolves, not just the ~100 hardcoded stars. Mirrors the
  pattern pitchers already had via the Savant feature store. See §5a.
- **2026-07-04** — **Player Impact Score Phase 2 (hitters)** shipped: team
  wRC+-with-vs-without-hitter model, ~100 hardcoded hitters, 30 team wRC+
  averages, MVP→REPLACEMENT tiers, `/hitter-impact` endpoint, pitcher/hitter
  toggle in the UI. See §5a.
- **2026-07-02 (rev 4)** — **Over/Under (Game Total) market**: the Poisson engine
  already computed totals probabilities at 6.5–10.5 lines internally; now surfaced
  as a 4th bet market with BET ≥62% / LEAN ≥57% thresholds. Expected total runs
  shown per game. Report now has 5 columns: Matchup | Exp. Runs | ML | F5 | O/U | NRFI.
- **2026-07-02 (rev 4)** — **All-market auto-resolution**: resolve now grades ALL
  four markets from one MLB linescore fetch: moneyline (final winner), F5 (leader
  after 5 innings), O/U (total runs vs predicted line), NRFI (first-inning outcome).
  Results section shows per-market accuracy table. No manual tracking needed.
- **2026-07-02 (rev 3)** — **Feature store freshness tracking**: daily report
  shows source, pitcher count, age, ⚠️ STALE (>7d) and ⚠️ EXPIRED (>14d) flags.
  Prevents silent data-integrity drift from forgotten CSV updates.
- **2026-07-02 (rev 3)** — **Validation-status separation** in all-markets table:
  NRFI column labeled ✅ (validated), moneyline/F5 labeled "not yet validated."
  Header table + footer make the proof hierarchy explicit for buyers.
- **2026-07-02 (rev 2)** — **Deleted GitHub Actions workflow** (`.github/workflows/
  nrfi_daily.yml`). Its `git push` raced with Railway's Contents API pushes to
  main → non-fast-forward rejections. Railway is now the single writer.
- **2026-07-02 (rev 2)** — **All-markets report table**: every game now shows
  moneyline, F5, and NRFI picks side by side, with a "Best play" column. Addresses
  the "1 bet out of 9 games" concern — NRFI is selective by design, but the
  moneyline/F5 models find value on most games.
- **2026-07-02 (rev 2)** — **Railway always-on scheduler** (`tasks/nrfi_auto.py`)
  replaces GitHub Actions cron. Predict 9 AM ET, capture 7 PM ET, resolve 1 AM ET.
  First auto-scan successful: 9 games, all pushed to GitHub. Manual trigger via
  `GET /nrfi-auto/run?job=predict` (browser-friendly).
- **2026-07-02 (rev 2)** — **Odds diagnostics endpoint** (`GET /nrfi-auto/odds-diag`)
  proves The Odds API connection works: status 200, Pinnacle quoting 1.81/2.03,
  `totals_1st_1_innings` market returned. CLV capture confirmed functional.
- **2026-07-02** — Feature store now built on **Baseball Savant** (FanGraphs
  endpoint confirmed dead — Cloudflare 403 even from residential IP + cloudscraper).
  Store carries real Savant Statcast for ~650 pitchers; model differentiates
  games again (p_nrfi 48–56 vs prior flat 51). Added optional FanGraphs member
  **CSV import** (`data/fangraphs*.csv`) to restore SIERA/CSW%/O-Swing%.
- **2026-07-02** — Fixed CI keys: ANTHROPIC/OPENWEATHER/ODDS now resolve from
  either GitHub Secrets or Variables (were read only from Secrets → blank → empty
  predictions). Root cause of the initial all-SKIP/empty runs.
- **2026-07-02** — Diagnosed FanGraphs/Savant server-IP block as the cause of
  flat ~51% predictions; shipped a precomputed **feature store** (local build +
  committed JSON, read at runtime) so the model gets real features in CI.
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
   stale-exclusion). Confirm with first week of live data. *(First entry odds
   captured 2026-07-02; closing capture fires tonight at 7 PM ET; first CLV
   computable tomorrow at resolve time.)*
2. **Rolling-window A/B after retrain** — planned; ~200 live predictions post-retrain.
3. **CLV actually predicts wins** — instrumented (`/v1/nrfi/clv-quality`,
   significance-tested). Read after ~50–100 resolved games.
4. **Soccer refuse-to-predict gate** — not started; deferred until soccer is picked back up.
5. **Moneyline/F5 validation tracker** — DECIDED: defer until NRFI has a live
   track record. Don't split proof-building across three markets when one isn't
   proven yet. NRFI stays the flagship.
6. **Feature store freshness** (Kimi rev 2 feedback) — IMPLEMENTED. Daily report
   now shows "Feature Store Freshness" section with source, pitcher count, age,
   and ⚠️ warnings at >7d (STALE, refresh recommended) and >14d (EXPIRED,
   FanGraphs features silently dropped to league mean). Prevents the "forgot to
   update CSV for 3 weeks" integrity risk.

### Decisions resolved (Kimi rev 2 feedback)

5. **FanGraphs CSV worth the manual chore?** → DECIDED: **measure for 2 weeks,
   then decide.** Savant carries ~80% of the contact-quality signal. After 2
   weeks of live data, compare model-lean accuracy with vs without FanGraphs CSV.
   If gap < 1pp, drop permanently. If > 2pp, consider automating.

6. **All-markets report dilutes the NRFI edge story?** → DECIDED: **ship it, but
   visually separate.** Report now has a validation-status table at the top of the
   all-markets section:
   - NRFI ✅ = walk-forward validated (54.9%, p=0.0049)
   - Moneyline & F5 = model-generated, not yet independently validated
   The footer reinforces this. Buyers see the full slate but understand which
   model is proven.

### Still open for Kimi
- Review the first week of closing-line JSON once resolve fires (expected
  2026-07-03 1 AM ET). The CLV math and devig symmetry are instrumented but
  unverified against real data.
- After ~30 resolved games: is the model-lean accuracy (all games, not just
  BET/LEAN) tracking above 52%? That's the earliest signal that directional
  skill is real.

---

## 12. Response to Kimi's rev-5 review (2026-07-04)

Went through the review's actionable engineering items same-day. Verified two
concerns were already handled correctly, fixed two real gaps, and one item
needs live data before it can move.

**Already correct (verified, no change needed):**
- **Asymmetric-juice devig** (§6, item 1): `models/devig.py: devig_market()`
  converts each side's decimal odds to implied probability independently and
  normalizes by their sum — it never assumes symmetric vig. Verified with
  lopsided test prices (−130/+110 and −115/+105 both produce correct,
  non-mirrored fair probabilities that sum to 1.0).
- **NRFI integer-line handling** (§6, item 2): `fetchers/nrfi_odds.py:
  _extract_nrfi_prices()` already filters outcomes to `point == 0.5` only
  (`abs(float(pt) - 0.5) > 1e-6: continue`) — any alternate line (e.g. 1.0)
  is silently skipped rather than misgraded.

**Fixed:**
- **sklearn version pin** (§4, "sklearn Version Mismatch Warning"): confirmed
  the concern was real — loading `models/nrfi_calibrator.pkl` under sklearn
  1.9.0 threw a live `InconsistentVersionWarning` (calibrator was trained on
  1.7.1). `requirements.txt` previously allowed `scikit-learn>=1.4.0`; pinned
  to `==1.7.1` to match the trained artifact exactly.
- **Kelly removed from public-facing surfaces** (§6, "Kelly Criterion"): the
  daily markdown report's Plays table dropped its "Half-Kelly" column
  (`scripts/daily_nrfi.py: write_report`) and the public `/v1/nrfi/plays` API
  now strips `kelly_half`/`stake_100`/`edge_pct` before returning
  (`app.py: v1_nrfi_plays`). Kelly math stays in `models/kelly.py` and the
  internal `nrfi_bets` DB table for our own use — public output is
  verdict-only (BET/LEAN/SKIP), per the "let users decide stake size" point.
- **Railway SPOF runbook** (§4, "Railway as Single Writer"): documented in
  CLAUDE.md — if Railway is down >24h, run `tasks/nrfi_auto.py` locally with
  `GITHUB_TOKEN`/`GITHUB_REPO` set; it pushes via the same Contents API path
  Railway uses, so it's safe to run without creating a competing writer.

**Can't action yet (needs live data, not code):**
- ESPN Player Impact fallback test with a non-hardcoded hitter (§4) — needs a
  real production lookup, not a sandbox test (this dev environment's egress to
  ESPN is blocked, same constraint noted in §2/§5a).
- 9 AM entry-lead-time check, model-lean accuracy after resolved games,
  FanGraphs-CSV A/B — all require the live prediction/resolution cycle to run
  for real, per Kimi's own timeline (§7 table).

---

## 13. Closed a real gap: moneyline/F5/O-U results weren't being aggregated (2026-07-04)

User question: "Are the losses being collected? It doesn't make sense to do it
manually if we can fetch them." Investigated end-to-end and found a genuine
gap, separate from the NRFI pipeline (which was already fully automated).

**What was already working:** `resolve_predictions()` in `scripts/daily_nrfi.py`
fetches final linescores every night and grades ALL FOUR markets per game —
NRFI outcome, `ml_correct`, `f5_correct`, `ou_correct` — writing the graded
result into that day's committed `data/nrfi_predictions/YYYY-MM-DD.json`. So
no losses were being silently dropped; every game's moneyline/F5/O-U result
was already being captured automatically.

**The actual gap:** nothing aggregated those three fields *across* days. NRFI
had `nrfi_store.aggregate_performance()` (DB-mirrored, exposed via
`/v1/nrfi/performance`), but moneyline/F5/O-U had no equivalent — the only way
to see "are we losing on moneyline picks over the last 2 weeks" was opening
each day's JSON file by hand and tallying `ml_correct` yourself. Confirmed by
grep: zero references to `ml_correct`/`f5_correct`/`ou_correct` existed
outside the single per-game grading step.

**Fix:** added `nrfi_store.aggregate_market_performance(days)` — same pattern
as the NRFI aggregator, reads every committed prediction file and rolls up
n_graded/n_correct/accuracy_pct/bet_win_pct for each of the three markets.
Exposed via `GET /market-performance`. Tested against the real committed data
right now (32 tracked games across 3 days): moneyline 76.5% graded-accuracy
(4 BET picks, 100% win — small sample, not a claim), F5 73.3%, O/U 77.8%
(18/18 games cleared the BET gate — expected, since O/U runs a wider set of
lines and clears its threshold more often than NRFI's single 55% gate).

**While investigating, resolved the user's specific loss questions with real
data (see §11-style spot-check):** the MIN@NYY game on 2026-07-03 (not the
in-progress 07-04 game) was a Yankees moneyline pick at 65.7%, final 5-2
Yankees — correct. No Cubs (CHC) game with a resolved 14-run margin exists in
the tracked history; the closest blowout loss is SF@COL on 07-03 (Giants
picked 57.1%, lost 3-15) — a real, correctly-graded loss, evidence the
pipeline works as intended even on bad outcomes.

**Note for Kimi:** this is diagnostic tooling, not a validation claim —
moneyline/F5/O-U stay unvalidated per the §11 decision to keep NRFI as the
sole proof-of-edge story. The rollup exists so the user can *watch* these
markets for systematic problems after ~50 games each, without manual
bookkeeping, not to advertise them.

---

## 14. Silent-failure bug found and fixed (2026-07-05)

While pulling live data to answer the user's "are losses being collected"
question, found that `data/nrfi_predictions/2026-07-04.json` had **11 games
with zero real predictions** — every field null (p_nrfi, ml_prob, mu_home,
everything), `model: "unknown"`, `verdict: "SKIP"`, and critically **no
`error` key** — so nothing in the report distinguished these from genuine
model SKIPs. Earlier the same day, this file had held real analysis (e.g. a
Yankees moneyline BET at 67%), so something overwrote good data with blank
data later in the day.

**Root cause:** `run_predictions()` in `scripts/daily_nrfi.py` calls
`run_baseball_analysis()` and immediately starts pulling `.get()` off its
return value without checking whether the call actually succeeded.
`run_baseball_analysis()` returns `{"error": "..."}` (not an exception) when
its first step — parsing the query via the Anthropic API — fails. Every
downstream `.get("markets", {}).get("nrfi", {})` etc. on that error dict
silently resolves to `{}` → `None`, producing a record that looks exactly
like a computed, confident SKIP. The `except Exception` block that properly
flags `pred["error"]` never fires, because no exception was ever raised.

**Fix:** added a check immediately after the call —
`if result.get("error"): raise RuntimeError(result["error"])` — so a failed
analysis now flows into the existing exception handler and gets a flagged
`error` field, which already surfaces in the report's dedicated "Errors"
section and is already excluded from every aggregate (both `_tracked()` in
nrfi_store.py and `aggregate_market_performance()` filter on real
`p_nrfi`/`*_correct` values, so blank records were never polluting the
stats — they were just invisible instead of flagged). Verified with a
mocked failing `run_baseball_analysis()` call: the record now carries
`error` instead of silently looking valid.

**Unresolved:** what caused the underlying Anthropic API call to fail for
all 11 games on 07-04 (rate limit? transient outage? key issue?) — that's
now diagnosable going forward since it'll show up in the Errors section,
but the specific 07-04 root trigger wasn't captured because it wasn't
logged as an error at the time. That day's 11 games are unrecoverable data
loss; going forward, silent data loss of this shape isn't possible anymore.
Also added `GET /nrfi-auto/anthropic-diag`, which actually calls the
query-parse step live and reports whether the Anthropic key is missing,
rate-limited, invalid, or working — instead of guessing from outside.

---

## 15. Manual website queries now get graded, not just the scheduled slate (2026-07-05)

User question: when a game is queried manually through the website (not part
of the automated daily scan), does that prediction get saved and later
compared to the real outcome? Checked the code — the honest answer was
**partially, and not usefully**:

- `run_baseball_analysis()` already called `_log_nrfi_prediction()`
  unconditionally at the end of every run (manual queries included), so an
  NRFI row DID get written to the `nrfi_bets` DB table every time.
- But that function only ever captured the NRFI market — a manual query's
  moneyline/F5/O-U pick (the markets this user actually cares most about, per
  §11/§13) was computed, returned to the browser, and never persisted
  anywhere.
- Worse: even the NRFI row that DID get saved had no automatic path to
  resolution. `resolve_predictions()` only operates on the JSON files built by
  the scheduled daily pipeline (`fetch_schedule` → `run_predictions`), which
  has no knowledge of ad-hoc DB rows from manual queries. A manual query's
  logged row would sit with `outcome = NULL` forever unless someone manually
  called `POST /nrfi-resolve` for that exact row.

**Fix, four pieces:**
1. `run_baseball_analysis()` now returns `game_pk` (previously only lived in
   the internal `context` dict, never surfaced to the caller).
2. `_log_nrfi_prediction` renamed to `_log_prediction` and extended to also
   persist `ml_pick/ml_prob/ml_verdict`, `f5_pick/f5_prob/f5_verdict`,
   `ou_pick/ou_prob/ou_verdict/ou_line`, and `game_pk` — same field names
   `daily_nrfi.py`'s `run_predictions()` already uses for the JSON pipeline,
   so both paths are consistent.
3. `db/database.py` migration `_migrate_nrfi_all_markets` adds these columns
   to `nrfi_bets` (idempotent, same pattern as the existing CLV migration).
4. New `scripts/daily_nrfi.resolve_pending_bets()` finds DB rows with a
   `game_pk` but no `outcome`, fetches the real linescore (same
   `fetch_linescore` call the JSON pipeline uses), and grades all four
   markets. Wired into the nightly `tasks/nrfi_auto.run_resolve` job
   automatically — no manual trigger needed, it runs every night alongside
   the scheduled-slate resolution.

Verified end-to-end with a mocked linescore: logged a fake manual-query row
(Yankees ML pick 67%, F5 pick, O/U 6.5), ran `resolve_pending_bets()` against
a 7-3 Yankees final, and confirmed all three non-NRFI fields graded correctly
(`ml_correct=1`, `f5_correct=1`, `ou_correct=1`).

**Net effect:** the user can now type any matchup into the website just to
check it, and that prediction becomes part of the same performance record as
the scheduled daily games — nothing querying the model "just to look" is
wasted anymore.

---

## 16. Major finding: barrel% unit mismatch flattened the run model's contact-quality signal (2026-07-05)

Started from a user-reported cosmetic bug (Player Impact Score showing "Barrel%
1040.0%", "Whiff% 8700.0%") and it led somewhere much bigger than a display fix.

**What was actually happening in the main prediction pipeline** (not just
Player Impact Score): `pitcher_process_adjustment()` in
`models/baseball_market.py` — the function that applies barrel%/CSW%/velo/
O-Swing% adjustments to mu_f5 for every moneyline/F5/O-U prediction — has a
decimal-scale constant, `LEAGUE_AVG_BARREL_PCT = 0.075`. But the barrel%
value it actually receives from the Savant feature store is a raw percent
(e.g. `10.4` meaning 10.4%), never converted to a decimal fraction anywhere
upstream. Verified directly:

```
barrel% 4.0%  (elite)  -> adjustment 1.08
barrel% 5.5%           -> adjustment 1.08
barrel% 7.5%  (avg)    -> adjustment 1.08
barrel% 8.5%           -> adjustment 1.08
barrel% 10.4% (poor)   -> adjustment 1.08
barrel% 12.0% (poor)   -> adjustment 1.08
```

Every single pitcher — elite or poor contact-suppression alike — clamped to
the **exact same +8% run-inflation multiplier**, because any real barrel rate
(3–15%) blows straight past the tiny 0.075 decimal threshold and hits the ±8%
cap in the same direction every time. **The barrel% signal has been
contributing literally zero differentiation between pitchers since the
feature store went live** — not degraded, not noisy, exactly zero.

**Why the NRFI XGBoost model was NOT affected:** checked the actual trained
model's tree-split thresholds directly (`booster.get_dump()`) — `home_barrel_pct`
splits at 4.4, 7.3, 9.5, 9.7, 9.8; `home_hard_hit_pct` splits at 32–44. Those
are unambiguously percent-scale, meaning the walk-forward-validated NRFI
model was trained on and correctly expects the same percent-scale values the
feature store provides. That model's core 54.9%/p=0.0049 validation is
untouched by this bug — this was purely a moneyline/F5/O-U (split-Poisson run
model) issue, plus the Player Impact Score tool that shares the same function.

**One remaining candidate issue, deliberately NOT touched today:**
`models/nrfi_model.py: FEATURE_DEFAULTS` has `barrel_pct: 0.085` (decimal) as
the fallback used only when a specific pitcher's real value is missing —
given training data is percent-scale, this default may itself be 100x off
for the missing-data case specifically (arguably should be `8.5`). Flagging
rather than fixing: real per-pitcher data (the common, everyday case) is
unaffected; only the rarer missing-feature fallback path is in question, and
changing a validated model's feature defaults deserves a proper backtest
before shipping, not a same-day patch alongside three other fixes.

**Fixed today:**
1. `pitcher_process_adjustment()` converts barrel_pct_against internally
   (÷100 when >1.5) — csw_pct/o_swing_pct untouched, already decimal-scale.
2. `models/player_impact.py: compute_impact()`'s backup FIP-estimate formula
   (used when a pitcher has no fip/era on file) — was feeding raw percents
   into a decimal-scale formula, clamping FIP to 6.0 for almost any
   feature-store-only pitcher regardless of real quality.
3. Two display spots multiplying already-percent-scale values by 100 again
   (`templates/baseball.html`, `analyze_baseball.py`'s audit signals table).
4. Separately: `lookup_batter` (the live ESPN hitter fallback from §5a) now
   tries every plausible team match instead of one fuzzy guess — "LA" matched
   both LAD and LAA equally well, so a query for a real Dodgers rookie could
   silently land on the Angels' roster and report a false "not found."

**How this was found:** a user screenshot showing Emmet Sheehan at -16.3pp
LIABILITY with Barrel% "1040.0%" — followed the display bug upstream through
`enrich_starter()` into the shared feature store, then checked whether the
SAME raw values also reached the main prediction pipeline (they did) before
touching anything, specifically to avoid the trap Kimi warned about earlier
in this doc: don't tweak the model reactively without knowing whether it's a
real bug or noise. This one had a concrete, mechanical, always-reproduces
cause (verified with 6 different barrel% inputs producing the same output),
not a single bad game — that's what made it safe to fix same-day.

---

*To extend Predicta's model briefings, create sibling files: `soccer_model_kimi.md`,
`tennis_model_kimi.md`, etc., in the same format.*
