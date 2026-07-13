# Predicta — Multi-Sport Prediction & Paper-Trading Engine

A local-first, paper-mode prediction engine covering **7 sports** and **2 stock-trading
engines**. Models are quantitative and inspectable — every probability is derived from
an explicit formula, not a black box. This document is written to be handed to another
AI (Kimi, Gemini, etc.) for independent review — it documents exactly what the code
does today, including a few places where the code, comments, and docs disagree with
each other (see [Known Discrepancies](#known-discrepancies--doc-vs-code-mismatches)).

---

## Systems Covered

| System | Data Source | Model | Status |
|--------|------------|-------|--------|
| MLB Baseball | ESPN / FanGraphs (pybaseball) / Baseball Savant | Split Poisson (F5/L4) + 60/40 Elo blend + dedicated NRFI XGBoost model | Live, most-calibrated pipeline |
| Tennis (ATP/WTA) | ESPN | Nested Markov chain (points→games→sets→match) | Live, `logistic_scale=40` flagged uncalibrated |
| Table Tennis | Setka Cup / TT Cup / ITTF | Weighted blend: Markov matchup + form + Glicko-2 + context | Live, but hidden from site nav (too unpredictable) |
| Soccer | ESPN / Understat / The Odds API | Dixon-Coles Poisson + dynamic Elo blend | Live |
| E-Sports (CS2/LoL/Dota2/Valorant) | PandaScore, Claude AI fallback | Elo-from-ranking + form blend + H2H nudge | Live |
| Rugby League (NRL) | ESPN | Negative-Binomial split score + dynamic Elo blend | **v1, uncalibrated** |
| UFC | ufcstats.com scrape | Glicko-2 + career-stats logistic blend + method-of-victory model | **v1, uncalibrated, data source unverified live** |
| High Value (stocks) | Alpaca | 8-signal intraday ensemble + n-gram pattern matching | Live, paper trades only |
| Low Value (stocks) | Alpaca / Finnhub / SEC EDGAR / OpenInsider / FINRA | 8-signal daily contrarian composite | Live, paper trades only, data-collection sprint mode ON |

An ML calibration layer (`models/ml_layer.py`) exists for the 4 core sports but is
**disconnected** — `predict()` is never called by any pipeline. It activates once
100+ resolved predictions exist per sport (see [ML Layer](#ml-layer-disconnected)).

---

## Architecture

```
User query (natural language)
        │
        ├─► Claude AI — parse teams, date, surface
        │
        ├─► Fetcher layer (ESPN / FanGraphs / Savant / PandaScore / Alpaca / Finnhub / ...)
        │       └─► data/live/ cache (GitHub Actions / Railway pre-fetch on schedule)
        │
        ├─► Quantitative model (Poisson / Markov / Dixon-Coles / Negative-Binomial / Glicko-2 / signal ensemble)
        │
        ├─► Elo / Glicko-2 blend
        │
        ├─► Market calculations (moneyline, spread, totals, F5/L4, NRFI…)
        │
        ├─► Kelly criterion / ATR / fixed-dollar stake sizing (paper mode only)
        │
        ├─► SQLite persistence (predictions, signals, outcomes, odds, intraday_trades)
        │
        └─► Claude AI — generate human-readable narrative
```

### Live Data Pipeline

Sports/market APIs are blocked in the remote dev container (egress policy). Two
separate always-on mechanisms keep `data/live/` fresh in production:
- A GitHub Actions workflow fetches sports JSON on a schedule and commits to `data/live/`.
- **Railway is the single writer to `main` for the NRFI daily pipeline** — an in-process
  scheduler (`tasks/nrfi_auto.py`) predicts at 13:00 UTC, captures odds at 23:00 UTC,
  and resolves outcomes at 05:00 UTC, pushing results via the GitHub Contents API.
  The GitHub Actions cron for this was deliberately removed — it raced with Railway's pushes.

The app reads cached files via `fetchers/data_cache.py`; nothing in the request path makes
a live sports-API call from the container itself.

---

## Mathematical Models — Sports

### 1. Elo Rating System (Soccer, Baseball seed, E-Sports, Rugby, UFC)

**Win probability:**
```
E(A) = 1 / (1 + 10^((R_B − R_A) / 400))
```

**Rating update:**
```
R'_A = R_A + K × M × (S_A − E(A))
```
- `S_A` = 1 (win), 0.5 (draw), 0 (loss)
- `K` = importance factor: Grand Slam/playoff = 60, international = 50, default = 30
- `M` = goal-difference multiplier (soccer/baseball-style sports): 1.0 (≤1 goal), 1.5 (2), 1.75 (3), +0.5 per additional goal

**Seeding from win%** (baseball, used when a team has no prior Elo history):
```
R_seeded = 1500 − 400 × log₁₀((1 − win_pct) / win_pct)
```

**E-Sports variant — Elo derived from world ranking, not match history**
(`analyze_esports.py`):
```python
def _elo_from_ranking(ranking: int) -> float:
    if not ranking or ranking <= 0:
        return 1500.0
    return max(1300.0, 2200.0 - 400.0 * log10(max(1, ranking)))
```
Rank 1 → ~2200, rank 100 → ~1400, floored at 1300.

**Rugby/soccer/UFC-style "dynamic" Elo blend weight** (replaces a fixed ratio in every
pipeline that uses it — see individual sections below for each one's exact formula):
```
elo_weight = 0.35 × min(1.0, rating_delta / 50.0)     # rugby, soccer
model_weight = 1.0 − elo_weight
```
Elo gets more say the further apart two teams' ratings are; caps at 35%.

---

### 2. Glicko-2 (Tennis + Table Tennis + UFC, per-surface / per-weight-class)

**Win probability:**
```
E(A,B) = 1 / (1 + exp(−g(RD_comb) × (r_A − r_B) / 400))

g(RD) = 1 / √(1 + 3·RD² / π²)
RD_comb = √(RD_A² + RD_B²)
```
- `RD` (Rating Deviation) = uncertainty. Tennis/TT start ~200, shrink toward ~50 with
  matches. UFC (`models/glicko.py`, `glicko2` library) starts at RD=350, volatility=0.06,
  and RD only shrinks on a *recorded* win/loss — **not** on elapsed real time with zero
  fights, so a 3-year-layoff fighter shows no extra rating uncertainty from inactivity
  alone (a known, explicitly documented gap — see [UFC](#9-ufc--glicko-2--career-stats-logistic-blend)).
- Rated per surface (tennis/TT: clay/grass/hard/all) or per weight class (UFC).

---

### 3. Baseball — Split Poisson Run Model

The core model separates the game into two windows driven by different pitchers:
```
F5  = innings 1–5  (starting pitcher)
L4  = innings 6–9  (bullpen)
```

#### Constants (verified against `models/baseball_market.py`, current as of this doc)
| Constant | Value | Meaning |
|----------|-------|---------|
| `LEAGUE_AVG_RUNS` | 4.50 | League average runs/team/game |
| `LEAGUE_AVG_FIP` | 4.00 | League average starter FIP |
| `LEAGUE_BULLPEN_FIP` | 4.40 | League average bullpen FIP |
| `STARTER_FRAC` | 5/9 ≈ 0.556 (or `min(max(avg_ip,3),7)/9` if a starter's real average IP is known from ≥3 starts) | Weight of starter window |
| `BULLPEN_FRAC` | 4/9 ≈ 0.444 | Weight of bullpen window |
| Home boost | 1.03 (inline, not a named constant) | Home field run advantage |

#### FIP (Fielding Independent Pitching)
```
FIP = ((13 × HR + 3 × BB − 2 × K) / IP) + 3.20
```
Counts only outcomes the pitcher controls. HR weighted 13× because each guarantees ≥1 run.

#### wRC+ (two formulas exist depending on data available)
```
wRC+ = ((2×OBP + SLG) / 1.045) × 100          ← preferred, when OBP/SLG both known
wRC+ ≈ (OPS / 0.730) × 100                     ← fallback, OPS-only
```
100 = league average.

#### Platoon Adjustment
```
wRC+_adj = wRC+ × 1.05   (RHB-heavy lineup vs a Left-handed starter)
wRC+_adj = wRC+ × 1.00   (vs a Right-handed starter)
```

#### Expected Runs
```
off  = wRC+_adj / 100
home = 1.03 if home team else 1.0
base = LEAGUE_AVG_RUNS × off × park_factor × home × weather_factor

mu_f5    = base × STARTER_FRAC × (starter_FIP / LEAGUE_AVG_FIP)
mu_l4    = base × BULLPEN_FRAC × (bullpen_FIP / LEAGUE_AVG_FIP)
mu_total = mu_f5 + mu_l4
```
**Clamps (loosened 2026-07-05 from 6.0/5.0/— so extreme parks like Coors aren't
artificially suppressed):** `mu_f5 ∈ [0.5, 8.0]`, `mu_l4 ∈ [0.4, 7.0]`, `mu_total ∈ [1.5, 10.0]`.

#### Weather Factor (actively applied — see discrepancy note below)
```
temp_factor = 1.0 + (temp_f − 72.0) × 0.003
wind_contrib = wind_alignment(-1..1) × wind_mph × 0.004
weather_factor = clamp(temp_factor × (1 + wind_contrib), 0.85, 1.15)   # 1.0 if dome
```

#### Pitcher process adjustment — barrel% bug (fixed 2026-07-05)
`pitcher_process_adjustment()` blends Statcast contact-quality signals into a
multiplier on top of `mu_f5`. Barrel% is stored **percent-scale** (e.g. `10.4` meaning
10.4%) in the feature store, but the league-average constant is decimal-scale
(`LEAGUE_AVG_BARREL_PCT = 0.075`). For months, every real pitcher's barrel% blew past
the ±8% cap in the same direction because the raw percent value was never divided by
100 — every single pitcher got the *identical* +8% run-inflation penalty regardless of
whether their real barrel% was elite (4%) or poor (12%). Fixed with a scale guard:
```python
barrel_frac = barrel_pct_against / 100.0 if barrel_pct_against > 1.5 else barrel_pct_against
adj -= max(-0.08, min(0.08, (barrel_frac - LEAGUE_AVG_BARREL_PCT) * 2.50))
```
Combined multiplier clamped to `[0.85, 1.15]`. The dedicated NRFI XGBoost model (below)
trains directly on the percent-scale feature-store data and was **never affected** —
only this specific moneyline/F5/O-U adjustment path was.

#### Park Factors (3-year run multipliers, `fetchers/baseball.py`, all 30 parks)
| Park | Factor | | Park | Factor | | Park | Factor |
|------|--------|-|------|--------|-|------|--------|
| COL | **1.38** | | HOU | 1.01 | | CLE | 0.96 |
| CIN | 1.10 | | LAA | 1.00 | | WSH | 0.96 |
| BOS | 1.08 | | MIL | 1.00 | | OAK | 0.96 |
| TEX | 1.07 | | MIA | 0.99 | | LAD | 0.96 |
| PHI | 1.05 | | DET | 0.99 | | TB | 0.95 |
| CHW | 1.04 | | MIN | 0.99 | | SD | 0.93 |
| ATL | 1.03 | | NYY | 0.99 | | SEA | 0.92 |
| ARI | 1.03 | | PIT | 0.98 | | SF | 0.91 |
| CHC | 1.02 | | KC | 0.98 | | | |
| BAL | 1.02 | | TOR | 0.98 | | | |
| | | | NYM | 0.97 | | | |
| | | | STL | 0.97 | | | |

Coors Field was corrected from 1.19 → 1.38 (FanGraphs multi-year run factor) on
2026-07-05 — see [Known Discrepancies](#known-discrepancies--doc-vs-code-mismatches)
for a second, still-stale copy of this table elsewhere in the codebase.

#### Score Matrix & Markets
```
P(home=i, away=j) = Poisson(i; mu_home) × Poisson(j; mu_away)     [21×21 matrix, 0–20 runs]

Moneyline:  p_home = P(home>away) + P(tie)×0.52   (extra-innings home win rate)
            p_away = P(away>home) + P(tie)×0.48
Run Line:   P(home−away > 1.5) vs P(away−home > 1.5)
Totals:     P(home+away > L) for lines 7.5–9.5
NRFI:       e^(−mu_home/9) × e^(−mu_away/9)   — superseded by the dedicated XGBoost model when it's loaded
```

#### Elo Blend, Probability Cap, and Data-Confidence Gate
```
prob_a_final = 0.60 × prob_a_poisson + 0.40 × prob_a_elo      (60/40 — corrected from an earlier 70/30)

MAX_MLB_PROB = 0.65   if park_factor ≥ 1.20   (Coors-type parks — inherently more volatile)
             = 0.72   otherwise

data_confidence = "low"     if AI query-parse fallback used, both starters missing/TBD,
                               OR either probable starter appears on the team injury report
                             (fixed 2026-07-05 — force-downgrades regardless of other signals)
                = "medium"  if exactly one weak signal (missing starter/wRC+, <10 GP)
                = "high"    otherwise
```
`data_confidence != "low"` gates BET/LEAN verdicts throughout the pipeline.

#### BET / LEAN / SKIP Thresholds
| Market | LEAN | BET | HIGH confidence |
|--------|------|-----|------------------|
| NRFI | ≥55% + elite-starter gate | ≥55% + elite signal (CSW%>30% or barrel%<6.5%) | ≥57% & 2 elite signals |
| F5 moneyline | ≥58% | ≥62% + SIERA/FIP starter gap ≥1.0 | ≥67% & gap≥1.5 |
| Full-Game moneyline | ≥58% | ≥65% + `data_confidence != "low"` + model-vs-market gap ≤20pp | ≥70% |
| Game Total | ≥57% | ≥62% | ≥68% |

Full-Game BET raised from 62%→65% and F5 BET from 60%→62% after early losses (documented
in `CLAUDE.md`'s "Bugs Fixed" log — thresholds were tightened only after real losses, not
loosened preemptively).

#### Dedicated NRFI Model (separate from the moneyline/F5/O-U Poisson path)
`models/nrfi_model.py` — walk-forward-validated XGBoost, **not** the general ML layer
below. 30 features: starter FIP-family rates, Baseball Savant metrics (barrel%,
hard-hit%, whiff%, avg velocity, home+away), season stats (SIERA/xFIP/FIP/CSW%/O-Swing%/
K%/BB%/GB%/HR-FB%, home+away), top-3-hitter wRC+ (home+away), park factor, dome flag.
`BET_THRESH = 0.55`. Falls back to the Poisson NRFI formula above if
`models/nrfi_xgb.json` isn't present. `FEATURE_DEFAULTS` documented per-feature for the
missing-data case (e.g. barrel 8.5%, hard-hit 37.0%, whiff 24.5%, velo 93.5 mph — see
[Known Discrepancies](#known-discrepancies--doc-vs-code-mismatches) for a scale-mismatch
question flagged but not yet fixed here).

---

### 4. Tennis — Nested Markov Chain

Three nested dynamic programs: points → games → sets → match.

#### Serve Quality Index (SQI) and Return Quality Index (RQI)
```
SQI = ((first_serve_pct / AVG_FIRST_SERVE_PCT)
     + (first_won_pct  / AVG_FIRST_WON_PCT)
     + (second_won_pct / AVG_SECOND_WON_PCT)) / 3 × 100

RQI = (bp_converted_pct / AVG_BP_CONVERTED) × 100        ← single-signal in the actual code (see below)
```
Centred at 100 = tour average.

| | ATP | WTA |
|---|---|---|
| First-serve % | 0.62 | 0.60 |
| 1st-serve won % | 0.73 | 0.68 |
| 2nd-serve won % | 0.54 | 0.51 |
| BP converted % | 0.40 | 0.42 |

#### Surface Amplifier (applied to SQI only)
| Surface | Multiplier |
|---------|-----------|
| Grass | 1.15 |
| Hard | 1.00 |
| Clay | 0.88 |

#### Point Probability
```
SQI_A_eff = SQI_A × surf_amp + swr_adj + form_adj
SQI_B_eff = SQI_B × surf_amp − swr_adj − form_adj

P_serve  = 1 / (1 + exp(−(SQI_A_eff − RQI_B) / 40))   (A serving)
P_return = 1 / (1 + exp(−(RQI_A − SQI_B_eff) / 40))   (B serving)

swr_adj  = (SWR_A / (SWR_A + SWR_B) − 0.5) × 20   (±10 max)
form_adj = (form_A / (form_A + form_B) − 0.5) × 10  (±5 max)
```
`logistic_scale = 40` — **flagged, unresolved**: `tests/validate_tennis_scale.py` shows
this implies an ~80pp hold-rate gap vs. the real ATP reference of ~20-25pp, i.e. probably
too aggressive. Also applies same-day (`×0.96`) and next-day (`×0.99`) fatigue penalties to SQI.

#### Game / Set / Match DP
```
DP_game: standard win-by-2 recursion; deuce has a closed-form shortcut p²/(p²+(1−p)²)
DP_set:  games DP into sets; 6-6 tiebreak is approximated as (P_serve+P_return)/2,
         NOT simulated point-by-point
DP_match: sets DP into the match; averages over who serves the match's first set (50/50)
```

---

### 5. Table Tennis — Weighted Blend (Markov + Form + Glicko-2 + Context)

No dedicated math module — all logic lives in `analyze_table_tennis.py`, reusing
`models/glicko.py`. Not a sequential blend (a stale docstring elsewhere describes an
older "75/25 → 70/30" version) — it's a single weighted sum:
```
W_MATCHUP = 0.40   (point-level Markov sim + handedness + first-time-opponent premium)
W_FORM    = 0.20   (recent win rate)
W_GLICKO  = 0.30   (ranking-based Glicko-2)
W_CONTEXT = 0.10   (fatigue + line movement, split evenly)
```
AQI/RQI-equivalent signal, same `scale=40.0` logistic as tennis:
```
p_a_serves  = logistic(AQI_a_eff − RQI_b, scale=40.0)
p_a_returns = logistic(RQI_a − AQI_b_eff, scale=40.0)
```
**Style matchup**: chopper/defender vs. attacker suppresses the attacker's AQI ×0.90;
blocker vs. attacker suppresses ×0.95.
**Fatigue**: continuous decay `exp(-0.12 × max(0, matches_today-2))` — not a discrete flag
despite `fatigue_flag` appearing in the ML layer's feature schema.

**5pp value gate** (only when real market odds are supplied and `data_confidence != "low"`):
```
VALUE_GATE = 0.05
recommend the side  if edge >= VALUE_GATE  else PASS
```
For low-confidence/club-circuit matches, a *different* threshold applies instead —
`SHARP_THRESHOLD = 0.05` (or `0.08` for club circuit), based on line movement, not the
value gate.

**Hidden from site nav 2026-07-12** (user judgment call — "too unpredictable to bet on").
The pipeline, `/ping-pong` route, and `/analyze-table-tennis` endpoint are all still live.

---

### 6. Soccer — Dixon-Coles Poisson

```
mu_home = attack_home × defense_away × league_avg × home_advantage
mu_away = attack_away × defense_home × league_avg
```
`home_advantage`: per-league (`models/soccer_leagues.py`), ranging 1.10 (Bundesliga) to
1.20 (RFPL), default 1.15.

**Low-score correlation correction** (code names this constant `TAU`, not the literature's
conventional `ρ`):
```
τ = 0.10
adjustment(0,0) = 1 − mu_home·mu_away·τ
adjustment(1,0) = 1 + mu_away·τ
adjustment(0,1) = 1 + mu_home·τ
adjustment(1,1) = 1 − τ
adjustment(i,j) = 1.0   for every other score

P(i,j) = Poisson(i; mu_home) × Poisson(j; mu_away) × adjustment(i,j)
```

**Dynamic Elo blend** (not a fixed ratio):
```
elo_weight = 0.35 × min(1.0, rating_delta / 50.0)     # caps at 35%
dc_weight  = 1.0 − elo_weight
```

**xG input**: exponential time-decay, not a fixed rolling window — tries half-lives
`[90, 150, 240]` days and picks the best-fitting one via Kish effective-sample-size;
falls back to a plain last-5-match average if the decayed fetch errors.

**Form blend**: `DEFAULT_RECENT_WEIGHT = 0.6` — last-5 form gets 60% weight, full-season 40%.

**H2H**: `h2h_decayed` is listed as an ML-layer feature name but is **not actually
computed anywhere** in the live soccer pipeline — see [Known Discrepancies](#known-discrepancies--doc-vs-code-mismatches).

---

### 7. E-Sports (CS2 / LoL / Dota2 / Valorant)

```
prob_elo_a  = standard 400-point logistic Elo win prob, using ranking-derived Elo (see §1)
prob_form_a = form_a / (form_a + form_b)          form = win rate over last ≤10 PandaScore matches, default 0.5 if unknown

prob_blend = 0.60 × prob_elo_a + 0.40 × prob_form_a,   clamped to [0.05, 0.95]

H2H nudge (only if ≥3 prior encounters exist):
  nudge = (h2h_rate − 0.5) × 0.06                  (±3pp max)
```
Same formula for all four games — only the PandaScore endpoint slug differs. PandaScore
is primary (needs `PANDASCORE_API_KEY`); absent a key or a match, falls back to a
Claude Haiku JSON-estimate per team.

---

### 8. Rugby League (NRL) — Negative-Binomial Split Score

**v1, uncalibrated — every constant below is a starting estimate, not fitted to real
NRL history.** This repo's build/test sandbox cannot reach espn.com at all, so there
was no historical score data available to calibrate against when this was built.

```python
LEAGUE_AVG_POINTS      = 22.0
HOME_ADVANTAGE          = 1.12
NB_DISPERSION_K          = 8.0     # Negative-Binomial dispersion
SHRINKAGE_K              = 8.0     # small-sample shrinkage toward league mean
DEFAULT_RECENT_WEIGHT    = 0.6
MODEL_PROB_CAP           = 0.78
```
```
mu_home = LEAGUE_AVG_POINTS × home_attack × away_defense × HOME_ADVANTAGE   (1.0 if neutral site)
mu_away = LEAGUE_AVG_POINTS × away_attack × home_defense

Each side's score ~ NegativeBinomial(k=NB_DISPERSION_K, p=k/(k+mu)), scored 0–60,
outer-producted into an independent score matrix — no Dixon-Coles-style correction
(rugby doesn't have soccer's 0-0/1-0 clustering problem).
```
**Shrinkage**: `w = matches/(matches+SHRINKAGE_K)`, `shrunk = w×value + (1−w)×league_mean`
— applied after blending season/recent form (60/40) and home/away splits (venue weight
0.40, ramped by `min(1, venue_matches/6)`).

**Elo blend**: same dynamic-ramp formula as soccer (`elo_weight = 0.35 × min(1, delta/50)`),
applied to the decisive (non-draw) probability mass, then `MODEL_PROB_CAP` enforced.

**Points aggregation**: pulled from ESPN's `/scoreboard` over a wide date range (the
per-team `/schedule` endpoint 500s for this league specifically — a confirmed
ESPN-side bug, not this codebase's), filtered per team. `before_date` excludes
same-day games to prevent a just-finished match from leaking its own result into its
own prediction (a real bug found and fixed 2026-07-12 during live UI testing).

**Known modeling gap**: real rugby scores are lumpy combinations of 1/2/4/6-point
plays; a smooth Negative Binomial over all integers likely inflates the model's draw
probability above NRL's real, golden-point-suppressed draw rate. Not fixed — documented.

---

### 9. UFC — Glicko-2 + Career-Stats Logistic Blend

**v1, uncalibrated, and the data source itself is unverified** — `fetchers/ufc.py`
scrapes ufcstats.com (no official API exists) using CSS selectors inferred from the
site's long-stable public structure, but this repo's sandbox cannot reach ufcstats.com
at all, so those selectors have never been confirmed against a real live response.

```python
DEFAULT_GLICKO_RD    = 350.0
CONFIDENT_RD          = 60.0
STATS_SCALE            = 0.35
REACH_COEF             = 0.015
AGE_DECLINE_START       = 34.0
AGE_DECLINE_PER_YEAR    = 0.03
MODEL_PROB_CAP          = 0.82
LEAGUE_AVG_WIN_KO_RATE  = 0.45
LEAGUE_AVG_WIN_SUB_RATE = 0.20
LEAGUE_AVG_LOSS_KO_RATE = 0.45
LEAGUE_AVG_LOSS_SUB_RATE= 0.20
```

**Career-stats logistic** (`stats_win_prob`):
```
striking_diff = (slpm_a − sapm_a) − (slpm_b − sapm_b)
td_a = takedown_avg_a × takedown_accuracy_a / 100
td_edge = (td_a × (1 − takedown_defense_b)) − (td_b × (1 − takedown_defense_a))
age_penalty(age) = (age − 34) × 0.03   if age > 34   else 0
age_pen = age_penalty(fighter_b) − age_penalty(fighter_a)
reach_adv = (reach_a − reach_b) × REACH_COEF

score = striking_diff + td_edge + reach_adv + age_pen
prob_stats_a = sigmoid(score × STATS_SCALE)
```

**Composite blend** (Glicko-2 trusted less as RD grows — this is the whole reason
Glicko-2 was chosen over plain Elo, since fighters go inactive for months/years):
```
avg_rd = (rd_a + rd_b) / 2
glicko_weight = clamp((350 − avg_rd) / (350 − 60), 0, 1) × 0.6    ← capped at 60% even at max confidence
stats_weight  = 1 − glicko_weight

prob_a = glicko_weight × prob_glicko_a + stats_weight × prob_stats_a,   capped at MODEL_PROB_CAP (0.82)
```

**Method of victory** (separate categorical model, no equivalent elsewhere in this
codebase — MMA has no continuous score to predict a margin for): blends a fighter's own
win-KO/win-submission rate 50/50 with the opponent's loss-KO/loss-submission rate
(finish susceptibility), falling back to the league-average constants above when data
is missing. If `p_ko + p_sub > 1`, both are rescaled proportionally; `p_decision` is the
residual. Draws (real in MMA, <2%) are deliberately hardcoded `prob_draw = 0.0` rather
than fabricated as a near-zero estimate.

**Known gaps, documented not fixed**: (1) no "ring rust" signal — needs days-since-last-
fight, which `fetch_fight_history` doesn't currently return a date for; (2) Glicko-2's
RD only widens on a *recorded* win/loss, never on elapsed inactive time alone, so a
3-year-layoff fighter shows no extra uncertainty from that fact alone — a gap shared by
every other Glicko-2 usage in this repo, not unique to UFC.

---

### 10. Position Sizing (Sports paper-mode + Trading engines)

**Kelly Criterion** (sports paper mode, and High Value's `trading_kelly`):
```
edge = p × (decimal_odds − 1) − (1 − p)
f*   = edge / (decimal_odds − 1)           ← full Kelly fraction
f    = f* × 0.25                           ← quarter-Kelly (conservative)
stake = f × bankroll
```
No bet if `edge ≤ 0`.

**ATR-based sizing** (High Value's actual default — not classical Kelly):
```
stop_distance = 1.5 × ATR
risk_amount   = account_value × 0.01        (1% of capital risked per trade)
shares        = risk_amount / stop_distance
```
Position size stays ATR-based regardless of win rate until 50+ trades exist; a
risk-parity variant (inverse-volatility sizing, Bridgewater-style) exists in code but
is gated off (`RISK_PARITY_MODE = False`) pending validation that it beats fixed-risk sizing.

**Fixed-dollar sizing** (Low Value): `$25` per trade, deliberately not scaled by
account size or volatility — caps the damage of a single bad pick on these thin,
sub-$20 names regardless of bankroll.

All sizing across every pipeline is **paper mode only** — no real orders are ever placed automatically.

---

### 11. Calibration Framework

| Metric | Formula | Notes |
|--------|---------|-------|
| Brier Score | `(1/N) Σ(pᵢ − oᵢ)²` | Target < 0.22 |
| Log-Loss | `−(1/N) Σ[oᵢ·log(pᵢ) + (1−oᵢ)·log(1−pᵢ)]` | Lower = better |
| ROI | `total_profit / total_staked × 100%` | Target > 0% |
| CLV | `fair_prob_at_close − model_implied_prob` | Target > 0% |
| Wilson score interval | see below | Small-sample-safe confidence interval |

**Trading calibration gate** (`models/trading/shared/signal_calibration.py`):
```
MIN_TRADES_FOR_ANY_CALIBRATION = 100    # below this, calibrated_win_rate/veto_decision refuse to act — static behavior only
WILSON_CONFIDENCE = 0.80                 # confidence level for the veto's CI test
```
`veto_decision()` replaces a naive point-estimate check ("win rate < 45% → veto") with
a Wilson-score confidence-interval test — vetoes only when the CI *upper* bound sits
entirely below a floor (default 48%), which in practice needs ~25-30+ trades in a score
bucket to ever trigger. A raw 45% win rate at n=15 could easily be a true 55% rate with
bad variance; the CI test avoids reacting to that noise.

**Low Value's own tiers** (`signal_calibration.py`):
```
LOW_VALUE_THESIS_PRELIMINARY_MIN_TRADES = 20    # directional hints only
LOW_VALUE_DYNAMIC_WEIGHT_MIN_TRADES     = 50    # meaningful per-thesis-type calibration
LOW_VALUE_EMPIRICAL_SIZING_MIN_TRADES   = 100   # empirical/Kelly-style sizing unlocks
```

**ML layer** activates after 100+ resolved predictions per sport (not 50 — see
[ML Layer](#ml-layer-disconnected) below; the number in an earlier version of this
document was wrong).

---

## Trading Models

Two **independent** engines — separate signal logic, separate background threads,
sharing only the Alpaca market-data fetcher and the `intraday_trades` DB table (scoped
by an `engine` column). "Two tabs, two engines. No mixing."

### High Value — Intraday Paper Trading

8-signal ensemble on 5-minute bars, weighted and normalized to −100..+100:

```python
WEIGHTS = {
    "vwap": 0.20, "or": 0.15, "rsi": 0.15, "relvol": 0.10,
    "gap": 0.10, "trend": 0.15, "bollinger": 0.10, "volsurge": 0.05,
}
composite = Σ(signal_score × weight) / max_possible_weighted_score × 100

SCORE_LABELS = [(60,"Strong Buy"), (20,"Buy"), (-20,"Neutral"), (-60,"Sell"), (-101,"Strong Sell")]
```
1. **VWAP deviation** — regime-conditioned: above VWAP in an uptrend reads as momentum,
   not overextension (and the reverse below VWAP in a downtrend), gated by the trend
   signal's own confidence level.
2. **Opening range** — breakout above/below the first-15-minute range.
3. **RSI-9** — short-period momentum oscillator.
4. **Relative volume** — time-of-day-adjusted vs. historical average.
5. **Gap fill** — pre-market gap direction/fill probability.
6. **Trend bias** — daily MA20 context; also sets the "regime confidence" used to
   condition VWAP and gap.
7. **Bollinger %B** — position within bands.
8. **Volume surge** — abnormal volume in the last N bars.

**Post-composite modifiers**: liquidity filter (hard reject or 50% haircut on wide
spreads), time-of-day modifier (lunch-chop suppression below a 40-point conviction
floor, zero on market-closed), and an **n-gram pattern blend**.

**N-gram signal** (`models/trading/high_value/ngram.py`) — treats 4-bar (20-minute)
5-min price-direction sequences like a language model treats words, predicting the
next bar's direction from historical frequency tables built from 6+ months of data.
Only fires at >52% historical directional edge with enough sample occurrences;
3-bar context was tested and rejected (too weak, ~0.05-0.12 autocorrelation, too close
to the 50% noise floor).

**Macro regime overlay** (Bridgewater "Four Boxes" / Dalio "3 forces", via FRED —
`fetchers/fred.py`, needs `FRED_API_KEY`, fails safe when absent):
```
yield_curve_slope = 10Y − 2Y Treasury yield
long_term_force_contraction = True if:
    credit_spread (HY-OAS) > its 1yr-daily mean + 2 std devs
    OR debt/GDP > its ~5yr-quarterly mean + 1 std dev
```
Read-only and additive — elevates the day's regime to EXTREME (which changes model
behavior elsewhere) alongside an intraday SPY-gap threshold; never a standalone gate.

**Position sizing / sprint mode**: see [§10](#10-position-sizing-sports-paper-mode--trading-engines)
for ATR sizing. `DATA_COLLECTION_SPRINT_MODE = True`, `SPRINT_MIN_SCORE = 10` (vs. the
"real" 40-point action threshold used elsewhere) — same rationale as Low Value's sprint
mode below: reach 100 closed trades for calibration in weeks, not the ~6-12 months the
strict floor would take, with zero data loss (`entry_score` is always stored).

**Portfolio controls**: max 3 concurrent positions (the 8-symbol tech-heavy watchlist
is a single correlated cluster, not diversified sectors — capping total exposure
substitutes for real pairwise-correlation math), force-close at 15:50 ET.

---

### Low Value — Sub-$20 Contrarian Engine

Daily-bar-only "buy the fear" contrarian thesis. A low price-vs-20-day-low ratio,
oversold RSI, and a capitulation volume spike are treated as **bullish** (beaten down,
about to turn) — opposite polarity from a trend-following engine.

```python
SIGNAL_WEIGHTS = {
    "price_vs_20d_low":         0.20,
    "rsi_14":                   0.15,
    "volume_spike":             0.10,
    "insider_buying_30d":       0.20,   # includes insider SELLING as of 2026-07-13, see below
    "short_interest_pct":       0.10,
    "sector_relative_strength": 0.10,
    "cash_burn_months":         0.10,
    "news_sentiment":           0.05,
}
ENTRY_THRESHOLD = 40.0     # the "real" high-conviction bar

composite = weighted average of every AVAILABLE signal (weights proportionally
            redistributed across present signals — a missing signal is EXCLUDED,
            never defaulted to a fabricated value)
```

**Signal formulas** (`models/trading/low_value/thesis_tracker.py`):
```
price_vs_20d_low:  ratio = (close − 20d_low) / 20d_low
                    score = 100 × max(0, 1 − ratio/0.30)         (at the low → 100; 30%+ above it → 0)
rsi_14:             score = clamp(-100, 100, (50 − RSI) × 2)      (RSI 30 → +40, RSI 10 → +80, RSI 70 → -40)
volume_spike:       spike = today_vol / 20d_avg_vol
                    score = clamp(0, 100, (spike − 1) × 40)       (magnitude only — direction comes from the other signals)
short_interest_pct: score = clamp(0, 100, short_interest_% × 4)   (squeeze-potential framing — contrarian upside, not risk)
sector_rel_strength: score = clamp(-100, 100, (symbol_5d_ret − sector_5d_ret) × 10)
cash_burn_months:   score = clamp(-100, 100, (runway_months − 6) × 10)   (<3mo runway scores negative — real bankruptcy risk)
news_sentiment:     score = VADER compound sentiment × 100
```

**Insider buying/selling** (`_score_insider_buying`, updated 2026-07-13 — user-requested):
originally only checked open-market insider *purchases* via OpenInsider (free, no key).
Now also checks *sales* from the same page fetch — "if the company's own leadership is
dumping shares alongside a retail panic sell-off, that's real confirmation, not just
fear" was the reasoning. True institutional/13F ownership data would answer this
better but is 45-day-lagged by SEC rule with no free source available; Form-4 insider
selling (2-business-day disclosure) is the closest fresh, free proxy.
```
bought, not sold  →  +100   (strongest confirmation the drop is fear-driven)
bought AND sold   →   +40   (mixed signal)
neither           →     0   (no signal, same as the old default)
sold, not bought  →   −60   (insiders dumping into the dip contradicts the thesis)
```
v1, uncalibrated — like every other weight in this file. Known limitation: doesn't yet
distinguish a routine, pre-scheduled 10b5-1 plan sale from a discretionary one.

**Data-collection sprint mode** (added 2026-07-13, on by default): a live production
scan found only 2 candidate symbols out of 500 evaluated even before scoring, at the
strict 40-point bar — far too slow to reach the 20/50/100-trade calibration tiers.
```python
LOW_VALUE_DATA_COLLECTION_SPRINT_MODE = True
LOW_VALUE_SPRINT_MIN_SCORE = 15.0     # logging bar while sprint mode is on, vs. the real 40
```
Lossless: `entry_score` is always the real composite regardless of which bar let a
trade in, so any later analysis can re-filter to |score|≥40.

**Universe scanner** (`models/trading/low_value/scanner.py`) — daily pipeline:
1. All active, tradable, non-OTC Alpaca US equities.
2. Cheap batch-snapshot price filter → last close < $20.
3. Per-candidate, cheapest-check-first: earnings blackout → volatility floor (excludes
   stocks flat all week — needs ≥2% single-day move OR ≥5% 5-day range) → 20-day avg
   volume > 100k → market cap > $50M (Finnhub primary, Yahoo backup) → no 8-K
   bankruptcy filing (SEC EDGAR, Item 1.03) in the last 90 days.

Hard wall-clock budgets (`SCAN_TIME_BUDGET_SEC=240s`, `PRICE_FILTER_TIME_BUDGET_SEC=60s`)
degrade to a partial result instead of hanging if any dependency is slow — plus an outer
watchdog (`MAX_UNIVERSE_BUILD_SECONDS=360s`, `MAX_SCAN_SECONDS=900s`) that self-heals
the "in progress" flag if something hangs past even that. A per-stage funnel
(`stagnant/low-volume/no-bar-data/cap-unavailable/cap-too-small/bankruptcy` counts) is
logged so a day with zero results is diagnosable instead of a silent empty universe.

**Exits** (checked once daily, not intraday — this engine holds multi-day by design):
target +50%, stop −50%, 5-trading-day time limit, or (only for a negative-news-flagged
entry) a fresh positive-catalyst headline confirming the reversal thesis played out.

---

## ML Layer (disconnected)

`models/ml_layer.py` — logistic regression / gradient boosting calibration layer for
the 4 core sports. **`predict()` is never called by any live pipeline.**
```python
MIN_SAMPLES = 100
BASEBALL_FEATURES     = ["wrc_plus","starter_fip","bullpen_fip","park_factor","platoon_adj","starter_avg_ip","home_boost","elo_rating","wind_factor","temp_factor","is_dome"]
TENNIS_FEATURES       = ["sqi","rqi","surface_win_rate","form_score","rest_days","glicko2_rating","surface_amp"]
SOCCER_FEATURES       = ["elo_diff","glicko2_diff","form_diff","h2h_decayed","rest_diff","key_player_out_flag","neutral_site_flag","fatigue_flag","home_adv"]
TABLE_TENNIS_FEATURES = ["elo_diff","glicko2_diff","form_diff","h2h_decayed","fatigue_flag"]
```
Will activate once 100+ resolved predictions with logged signals exist for a sport.
Note `soccer`'s `h2h_decayed` and `table_tennis`'s `fatigue_flag`/`h2h_decayed` are
feature *names* only — see [Known Discrepancies](#known-discrepancies--doc-vs-code-mismatches)
for which of these aren't actually computed by the live pipelines yet, meaning training
would currently default them to 0.0 for every row.

---

## Known Discrepancies / Doc-vs-Code Mismatches

Surfaced while writing this document (2026-07-13) — listed here deliberately rather
than silently fixed, since an outside reviewer should know about them:

1. **Tennis RQI formula** — `tests/validate_tennis_scale.py`'s docstring describes a
   two-component weighted RQI (`bp_converted×0.6 + return_points_won×0.4`). The real,
   live code in `fetchers/tennis.py` computes RQI as a **single** signal
   (`bp_converted_pct / AVG_BP_CONVERTED × 100` only) — the documented formula doesn't
   exist in the running code.

2. **Soccer H2H** — `h2h_decayed` is listed as an ML-layer feature name for both soccer
   and table tennis, but `analyze_soccer.py`/`models/dixon_coles.py` never actually
   compute a head-to-head signal anywhere. It would silently default to 0.0 if the
   (currently disconnected) ML layer were ever trained.

3. **Two Coors Field park-factor tables, only one fixed** — `fetchers/baseball.py`'s
   table was corrected 1.19 → 1.38 on 2026-07-05 (see §3). `models/nrfi_model.py` has
   its own, separate `_PARK_FACTORS` dict, still at the old, uncorrected 1.19 and
   several other stale values — never synced with the fix.

4. **`fetchers/weather.py` docstring says weather doesn't apply to the run model yet**
   ("enable after 50+ predictions validate the effect") — it's stale. `analyze_baseball.py`
   actively multiplies `weather_factor` into `mu_f5`/`mu_l4` today.

5. **Dixon-Coles naming** — the correlation-correction parameter is universally called
   ρ (rho) in the Dixon-Coles literature; this codebase names the constant `TAU`/`tau`.
   Not a bug, just worth knowing if cross-referencing against the original paper.

6. **NRFI model's missing-data fallback may be wrong-scale** — `models/nrfi_model.py`'s
   `FEATURE_DEFAULTS` (e.g. `barrel_pct: 0.085`) are decimal-scale, but the model
   trained on percent-scale Savant data (tree splits at 4.4–9.8, clearly percent, not
   decimal). Only the rare *missing-data* fallback path is affected — real per-pitcher
   data (the common case) is correct. Flagged in `CLAUDE.md` as a candidate follow-up,
   deliberately not touched without a proper backtest first.

---

## Calibration Parameters (quick reference)

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `LEAGUE_AVG_RUNS` | 4.50 | Baseball baseline runs/game |
| `LEAGUE_AVG_FIP` | 4.00 | Starter FIP baseline |
| `LEAGUE_BULLPEN_FIP` | 4.40 | Bullpen FIP baseline |
| Baseball Elo blend | 60% Poisson / 40% Elo | Corrected from an earlier 70/30 |
| `MAX_MLB_PROB` | 72% (65% at Coors-type parks) | Hard probability cap |
| Full-Game ML BET threshold | 65% | Raised from 62% after early losses |
| F5 ML BET threshold | 62% | Raised from 60% |
| Coors Field park factor | 1.38 | Corrected from 1.19 |
| `KELLY_FRACTION` | 0.25 | Quarter-Kelly sizing |
| `DEFAULT_ELO` | 1500 | Initial Elo for unknown teams |
| `logistic_scale` (tennis/TT) | 40.0 | SQI/AQI → point-prob scale — flagged possibly too aggressive |
| `surf_amp_grass` / `surf_amp_clay` | 1.15 / 0.88 | Tennis surface serve amplifier |
| `DIXON_COLES_TAU` | 0.10 | Soccer low-score correction |
| Soccer/Rugby Elo blend | dynamic, `0.35 × min(1, Δ/50)` | Not a fixed ratio |
| `MIN_ML_SAMPLES` | 100 | ML layer training threshold (corrected from an earlier "50") |
| `MIN_TRADES_FOR_ANY_CALIBRATION` | 100 | Trading calibration gate (High Value + Low Value) |
| `WILSON_CONFIDENCE` | 0.80 | Trading veto's CI test confidence level |
| Low Value `ENTRY_THRESHOLD` | 40 | Real high-conviction bar |
| Low Value `SPRINT_MIN_SCORE` | 15 | Data-collection logging bar (sprint mode) |
| High Value `SPRINT_MIN_SCORE` | 10 | Data-collection logging bar (sprint mode) |
| Rugby `MODEL_PROB_CAP` | 0.78 | v1, uncalibrated |
| UFC `MODEL_PROB_CAP` | 0.82 | v1, uncalibrated |

---

## Setup

```powershell
git clone <repo-url>
cd Predicta
.\setup.ps1
```

**Required/optional environment variables** (`.env` file or Railway/GitHub Secrets):
```
ANTHROPIC_API_KEY=sk-ant-...      # Claude AI — query parsing, narratives, e-sports fallback
SPORTSDATA_API_KEY=...            # SportsData.io — currently invalid/unused, ESPN fallback always runs
ODDS_API_KEY=...                  # The Odds API — market odds for CLV
OPENWEATHER_API_KEY=...           # optional — baseball weather signal
PANDASCORE_API_KEY=...            # optional — e-sports; falls back to Claude AI estimates
FRED_API_KEY=...                  # optional — High Value's macro regime overlay; fails safe when absent
FINNHUB_API_KEY=...               # Low Value's market-cap check; falls back to Yahoo, then fails the candidate
ALPACA_API_KEY / ALPACA_SECRET_KEY # required for both trading engines — paper trading account
GITHUB_TOKEN / GITHUB_REPO        # Railway's NRFI auto-pipeline push target
```

---

## Quick Start

```powershell
.\.venv\Scripts\Activate.ps1

python cli.py predict --sport baseball --query "Dodgers vs Yankees tonight"
python cli.py predict --sport tennis --query "Alcaraz vs Sinner Wimbledon"
python cli.py predict --sport soccer --query "Man City vs Arsenal Premier League"
python cli.py calibration
python cli.py report
```

## API

```powershell
uvicorn app:app --reload
# Docs:   http://127.0.0.1:8000/docs
# Report: http://127.0.0.1:8000/report
```

---

## Signals Reference

| System | Signal | Description |
|--------|--------|--------------|
| Baseball | `wrc_plus` | Weighted Runs Created+ (100=avg) |
| Baseball | `starter_fip` / `bullpen_fip` | Fielding-Independent Pitching |
| Baseball | `park_factor` | 3-year park run multiplier |
| Baseball | `platoon_adj` | Handedness matchup boost |
| Baseball | `barrel_pct_against` / `csw_pct` / `o_swing_pct` | Baseball Savant contact-quality (feature store) |
| Baseball | `weather_factor` | Temp + wind alignment multiplier |
| Baseball | `data_confidence` | low/medium/high — gates BET/LEAN |
| Tennis | `sqi` / `rqi` | Serve / Return Quality Index (100=tour avg) |
| Tennis | `surface_win_rate` / `form_score` | Recency-weighted performance |
| Tennis / TT / UFC | `glicko2_rating` | Per-surface / per-weight-class Glicko-2 |
| TT | `aqi` / `rqi` | Serve advantage / return quality |
| TT | `style_matchup` | Looper/chopper/blocker tag |
| Soccer | `elo_rating` | Team Elo |
| Soccer | `xg_for_decayed` / `xg_against_decayed` | Time-decayed xG (90/150/240-day half-life search) |
| Soccer | `form_weighted10` | 60/40 recent/season blend |
| E-Sports | `elo_from_ranking` | World-ranking-derived Elo |
| E-Sports | `form_ratio` | Win rate, last ≤10 matches |
| E-Sports | `h2h_rate` | Head-to-head (min. 3 encounters) |
| Rugby | `ppg_for` / `ppg_against` | Points-for/against, shrunk toward league mean |
| Rugby | `recent_ppg_for` / `recent_ppg_against` | Last-5-only (fixed a 2026-07-12 display bug) |
| UFC | `slpm` / `sapm` / `td_avg` / `td_defense` / `reach` / `age` | Career-stats logistic inputs |
| UFC | `win_ko_rate` / `win_sub_rate` / `loss_ko_rate` / `loss_sub_rate` | Method-of-victory model inputs |
| High Value | `vwap` / `or` / `rsi` / `relvol` / `gap` / `trend` / `bollinger` / `volsurge` | 8-signal intraday ensemble |
| High Value | `ngram_signal` / `ngram_confidence` | 4-bar pattern-frequency prediction |
| High Value | `market_regime` | NORMAL / EXTREME (SPY gap + Dalio 3-force) |
| Low Value | `price_vs_20d_low` / `rsi_14` / `volume_spike` | Technical contrarian signals |
| Low Value | `insider_buying_30d` / `insider_selling_30d` | OpenInsider Form-4 activity |
| Low Value | `short_interest_pct` | FINRA/Nasdaq, squeeze-potential framing |
| Low Value | `sector_relative_strength` | 5-day return vs. sector ETF |
| Low Value | `cash_burn_months` | Runway from Finnhub fundamentals |
| Low Value | `news_sentiment` | VADER compound score |
| Low Value | `lv_thesis_type` | EARNINGS_MISS / ANALYST_DOWNGRADE / REGULATORY_RISK / OPERATIONAL_CRISIS / POSITIVE_CATALYST / INSIDER_BUYING / TECHNICAL_OVERSOLD |

---

## Paper Mode

All wager/position sizing across every sport and both trading engines is computed for
tracking and calibration purposes only. No real bets or stock orders are placed or
assumed. Any real-money action requires a separate, explicitly flagged manual step
outside this system.

---

## Questions for AI Review (Kimi / Gemini)

If you're reviewing this system, here's where independent judgment would help most:

1. **Baseball run model** — Is the FIP-based Poisson approach sound? `STARTER_FRAC` now
   adapts to a starter's real average IP when known (≥3 starts) rather than a fixed
   5/9 — does that meaningfully help vs. modern bulk-reliever/opener usage, or is the
   3-start sample too thin to trust?

2. **wRC+ approximations** — Two formulas exist (OBP/SLG-based when available, OPS-only
   fallback). How large is the real-world error between them, and is the fallback
   introducing meaningful bias on the games where it's used?

3. **Tennis logistic scale (40)** — Still flagged as possibly too aggressive by the
   codebase's own validation script. What would a properly fitted scale look like
   given the ATP reference hold-rate gap?

4. **Table Tennis's actual RQI formula** vs. the discrepancy noted above — which is
   "correct," and does the single-signal live version undervalue return quality?

5. **Rugby/UFC v1 constants** — Every number in both sections is a starting estimate
   with zero backtested history behind it (documented sandbox limitation, not
   negligence). Which constants would you prioritize fitting first once real resolved
   predictions exist?

6. **UFC's Glicko-2 + logistic blend weight cap (60% max)** — Is capping Glicko-2's
   maximum influence at 60% (even at full RD confidence) the right call, or should a
   very experienced, active fighter's rating carry more weight than the career-stats
   model?

7. **Low Value's insider buy/sell scoring** (+100/+40/0/−60) — freshly added, entirely
   a judgment call with zero backtested data. Is the asymmetry (buying rewarded more
   than selling is punished) justified, or backwards?

8. **High Value's n-gram context window (4 bars)** — chosen because 3-bar
   autocorrelation was too weak (~0.05-0.12) to trust. Is 4 bars (20 min) actually
   long enough to be meaningfully non-random, or still mostly noise dressed up as signal?

9. **Trading calibration gate (100 trades, Wilson 80% CI)** — Is 100 trades a reasonable
   floor before letting empirical data influence sizing/vetoes? Should the CI
   confidence level tighten as sample size grows, rather than staying fixed at 80%?

10. **Cross-cutting**: several "v1, uncalibrated" pipelines (Rugby, UFC) reuse
    baseball's conservative 65%/62pp BET thresholds by default, reasoning "start
    strict, loosen only after real data justifies it" (the opposite of what caused
    baseball's own early losses when thresholds started too loose). Is borrowing
    another sport's thresholds a reasonable placeholder, or does it risk masking how
    uncalibrated these models really are?
