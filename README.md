# Predicta — Multi-Sport Prediction & Calibration Engine

A local-first, paper-mode prediction engine for **MLB Baseball**, **Tennis (ATP/WTA)**, **Table Tennis**, and **Soccer**.  
Models are fully quantitative — every probability is derived from explicit formulas, not black-box ML.

---

## Sports Covered

| Sport | Data Source | Model |
|-------|------------|-------|
| MLB Baseball | ESPN API / SportsData.io | Split Poisson (F5+L4) + Elo blend |
| Tennis (ATP/WTA) | ESPN API | Nested Markov chain (points→games→sets→match) |
| Table Tennis | Setka Cup / TT Cup / ITTF | Glicko-2 + AQI/RQI signals |
| Soccer | ESPN / Understat / The Odds API | Dixon-Coles Poisson + Elo |

---

## Architecture

```
User query (natural language)
        │
        ├─► Claude AI — parse teams, date, surface
        │
        ├─► Fetcher layer (ESPN / SportsData.io / ITTF / Setka)
        │       └─► data/live/ cache (GitHub Actions pre-fetches on schedule)
        │
        ├─► Quantitative model (Poisson / Markov / Dixon-Coles)
        │
        ├─► Elo / Glicko-2 blend
        │
        ├─► Market calculations (moneyline, spread, totals, F5/L4, NRFI…)
        │
        ├─► Kelly criterion stake sizing (quarter-Kelly, paper mode only)
        │
        ├─► SQLite persistence (predictions, signals, outcomes, odds)
        │
        └─► Claude AI — generate human-readable narrative
```

### Live Data Pipeline

Sports APIs are blocked in the remote container (egress policy). A GitHub Actions
workflow runs at **11:00 UTC** and **17:00 UTC** daily, fetches data with full
internet access, and commits JSON files to `data/live/`. The app reads these files
via `fetchers/data_cache.py`.

```
GitHub Actions → python fetchers/live_data.py → data/live/*.json → git commit → main
                                                                         ↓
                                                              Container reads cached data
```

---

## Mathematical Models

### 1. Elo Rating System (Soccer + Baseball seed)

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
- `M` = goal-difference multiplier: 1.0 (≤1 goal), 1.5 (2), 1.75 (3), +0.5 per additional goal

**Seeding from win%** (baseball):
```
R_seeded = 1500 − 400 × log₁₀((1 − win_pct) / win_pct)
```

---

### 2. Glicko-2 (Tennis + Table Tennis, per surface)

**Win probability:**
```
E(A,B) = 1 / (1 + exp(−g(RD_comb) × (r_A − r_B) / 400))

g(RD) = 1 / √(1 + 3·RD² / π²)
RD_comb = √(RD_A² + RD_B²)
```
- `RD` = Rating Deviation — uncertainty (starts ~200, shrinks toward ~50 with matches)
- Rated per surface: clay / grass / hard / all

---

### 3. Baseball — Split Poisson Run Model

The core model separates the game into two windows driven by different pitchers:

```
F5  = innings 1–5  (starting pitcher)
L4  = innings 6–9  (bullpen)
```

#### Constants
| Constant | Value | Meaning |
|----------|-------|---------|
| `LEAGUE_AVG_RUNS` | 4.50 | 2024 MLB average runs/team/game |
| `LEAGUE_AVG_FIP` | 4.00 | 2024 MLB average starter FIP |
| `LEAGUE_BULLPEN_FIP` | 4.40 | MLB bullpen average FIP |
| `STARTER_FRAC` | 5/9 ≈ 0.556 | Weight of starter window |
| `BULLPEN_FRAC` | 4/9 ≈ 0.444 | Weight of bullpen window |
| `HOME_BOOST` | 1.03 | Home field run advantage |

#### FIP (Fielding Independent Pitching)
```
FIP = ((13 × HR + 3 × BB − 2 × K) / IP) + 3.20
```
Counts only outcomes the pitcher controls. HR weighted 13× because each guarantees ≥1 run.

#### ERA and WHIP (derived when not pre-computed)
```
ERA  = (earnedRuns × 9) / IP
WHIP = (H + BB) / IP
```

#### Bullpen FIP Derivation
```
team_ERA ≈ (starter_FIP × 5 + bullpen_FIP × 4) / 9
=> bullpen_FIP = (team_ERA × 9 − starter_FIP × 5) / 4
```
Clamped to [3.0, 7.5].

#### wRC+ from OPS
```
wRC+ ≈ round((OPS / 0.730) × 100)
```
0.730 = 2025-26 MLB average OPS. 100 = league average; 120 = 20% above average.

#### Platoon Adjustment
```
wRC+_adj = wRC+ × 1.05   (if opposing starter is Left-handed)
wRC+_adj = wRC+ × 1.00   (if Right-handed)
```
~65% of MLB lineups are Right-handed batters, who have a ~5% wRC+ advantage vs LHP.

#### Expected Runs
```
off  = wRC+_adj / 100
home = 1.03 if home team, else 1.0
base = LEAGUE_AVG_RUNS × off × park_factor × home

mu_f5    = base × STARTER_FRAC × (starter_FIP / LEAGUE_AVG_FIP)
mu_l4    = base × BULLPEN_FRAC × (bullpen_FIP / LEAGUE_AVG_FIP)
mu_total = mu_f5 + mu_l4
```
Clamped: `mu_f5 ∈ [0.5, 6.0]`, `mu_l4 ∈ [0.4, 5.0]`, `mu_total ∈ [1.5, 10.0]`

**Example:** wRC+=110, starter FIP=3.50, neutral park:
```
base  = 4.50 × 1.10 × 1.00 × 1.00 = 4.95
mu_f5 = 4.95 × 0.556 × (3.50/4.00) = 2.41
mu_l4 = 4.95 × 0.444 × (4.40/4.00) = 2.42
mu_total = 4.83 runs
```

#### Park Factors (3-year run multipliers)
| Stadium | Team | Factor |
|---------|------|--------|
| Coors Field | COL | 1.19 |
| Great American | CIN | 1.08 |
| Fenway Park | BOS | 1.06 |
| Oracle Park | SF | 0.92 |
| T-Mobile Park | SEA | 0.93 |
| Petco Park | SD | 0.94 |

#### Score Matrix & Markets
```
P(home=i, away=j) = Poisson(i; mu_home) × Poisson(j; mu_away)
```
Independent Poisson, matrix dimension 21×21 (0–20 runs).

**Moneyline:**
```
p_home = P(home>away) + P(tie) × 0.52   (home wins extras 52%)
p_away = P(away>home) + P(tie) × 0.48
```

**Run Line (±1.5):**  P(home − away > 1.5) vs P(away − home > 1.5)

**Totals O/U:**  P(home + away > L) for lines 7.5, 8.0, 8.5, 9.0, 9.5

**NRFI:**
```
P(NRFI) = e^(−mu_home/9) × e^(−mu_away/9)
```

#### Elo Blend
```
prob_a_final = 0.70 × prob_a_poisson + 0.30 × prob_a_elo
```
Normalised to sum to 1.0.

---

### 4. Tennis — Nested Markov Chain

Three nested dynamic programs: points → games → sets → match.

#### Serve Quality Index (SQI) and Return Quality Index (RQI)
```
SQI = ((first_serve_pct / AVG_FIRST_SERVE_PCT)
     + (first_won_pct  / AVG_FIRST_WON_PCT)
     + (second_won_pct / AVG_SECOND_WON_PCT)) / 3 × 100

RQI = (bp_converted / AVG_BP_CONVERTED × 0.6
     + return_points_won_pct × 0.4) × 100
```
Centred at 100 = tour average.

**ATP tour averages:** first-serve% 0.62, 1st-won 0.73, 2nd-won 0.54, BP-conv 0.40  
**WTA tour averages:** first-serve% 0.60, 1st-won 0.68, 2nd-won 0.51, BP-conv 0.42

#### Surface Amplifier
| Surface | Multiplier |
|---------|-----------|
| Grass | 1.15 — amplifies serve dominance |
| Hard | 1.00 — neutral |
| Clay | 0.88 — suppresses serve, rewards rallying |

#### Point Probability
```
SQI_A_eff = SQI_A × surf_amp + swr_adj + form_adj
SQI_B_eff = SQI_B × surf_amp − swr_adj − form_adj

P_serve  = 1 / (1 + exp(−(SQI_A_eff − RQI_B) / 40))   (A serving)
P_return = 1 / (1 + exp(−(RQI_A − SQI_B_eff) / 40))   (B serving)
```
Scale=40: a 40-point SQI advantage ≈ +25pp win probability.

Surface win rate and form adjustments:
```
swr_adj  = (SWR_A / (SWR_A + SWR_B) − 0.5) × 20   (±10 max)
form_adj = (form_A / (form_A + form_B) − 0.5) × 10  (±5 max)
```

#### Game DP
```
DP_game(pA, pB):
  if pA≥4 and pA−pB≥2 → 1.0
  if pB≥4 and pB−pA≥2 → 0.0
  if deuce (pA≥3, pB≥3):
      return p² / (p² + (1−p)²)   ← closed form
  return p × DP(pA+1,pB) + (1−p) × DP(pA,pB+1)
```

#### Set DP
```
DP_set(gA, gB, server):
  if gA==6 and gB==6: tiebreak ≈ (P_serve + P_return)/2
  if gA≥6 and gA−gB≥2 → 1.0
  if gB≥6 and gB−gA≥2 → 0.0
  p_game = DP_game(server)
  return p_game × DP_set(gA+1,gB,flip) + (1−p_game) × DP_set(gA,gB+1,flip)
```

#### Match DP
```
sets_needed = ceil(best_of/2)   [2 for BO3, 3 for BO5]

DP_match(sA, sB, server):
  if sA==sets_needed → 1.0
  if sB==sets_needed → 0.0
  p_set = DP_set(server)
  return p_set × DP_match(sA+1,sB,flip) + (1−p_set) × DP_match(sA,sB+1,flip)

prob_A = 0.5 × DP_match(0,0,A_serves) + 0.5 × DP_match(0,0,B_serves)
```

---

### 5. Soccer — Dixon-Coles Poisson

```
mu_home = attack_home × defense_away × league_avg × 1.15 (home advantage)
mu_away = attack_away × defense_home × league_avg
```

**Dixon-Coles correction** (low-score probability fix):
```
ρ(0,0) = 1 + mu_home × mu_away × τ
ρ(1,0) = 1 − mu_away × τ
ρ(0,1) = 1 − mu_home × τ
ρ(1,1) = 1 + τ
ρ(i,j) = 1.0  for all other scores

τ = 0.10
```

Score probabilities: `P(i,j) = Poisson(i; mu_home) × Poisson(j; mu_away) × ρ(i,j)`

---

### 6. Kelly Criterion Stake Sizing

```
edge = p × (decimal_odds − 1) − (1 − p)
f*   = edge / (decimal_odds − 1)           ← full Kelly fraction
f    = f* × 0.25                           ← quarter-Kelly (conservative)
stake = f × bankroll
```
No bet if `edge ≤ 0`. All sizing is paper-mode — no real bets are placed automatically.

---

### 7. Calibration

| Metric | Formula | Target |
|--------|---------|--------|
| Brier Score | `(1/N) Σ(pᵢ − oᵢ)²` | < 0.22 (lower = better) |
| Log-Loss | `−(1/N) Σ[oᵢ·log(pᵢ) + (1−oᵢ)·log(1−pᵢ)]` | lower = better |
| ROI | `total_profit / total_staked × 100%` | > 0% |
| CLV | `fair_prob_at_close − model_implied_prob` | > 0% |

**ML layer** activates after 50+ resolved predictions: calibrated logistic regression
or gradient boosting on stored signals, improving raw model output.

---

## Calibration Parameters

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `LEAGUE_AVG_RUNS` | 4.50 | Baseball baseline runs/game |
| `LEAGUE_AVG_FIP` | 4.00 | Starter FIP baseline |
| `LEAGUE_BULLPEN_FIP` | 4.40 | Bullpen FIP baseline |
| `STARTER_FRAC` | 5/9 | Starter innings weight |
| `BULLPEN_FRAC` | 4/9 | Bullpen innings weight |
| `HOME_BOOST` | 1.03 | Baseball home advantage |
| `PLATOON_VS_LHP` | 1.05 | RHB lineup bonus vs LHP |
| `FIP_CONSTANT` | 3.20 | FIP calibration offset |
| `wRC+_OPS_baseline` | 0.730 | 2025-26 MLB avg OPS |
| `ELO_BLEND` | 70% Poisson / 30% Elo | Baseball model blend |
| `KELLY_FRACTION` | 0.25 | Quarter-Kelly sizing |
| `DEFAULT_ELO` | 1500 | Initial Elo for unknown teams |
| `DEFAULT_K` | 30 | Default Elo K-factor |
| `surf_amp_grass` | 1.15 | Grass serve amplifier |
| `surf_amp_clay` | 0.88 | Clay serve suppressor |
| `logistic_scale` | 40.0 | SQI → point-prob scale |
| `extras_home_pct` | 0.52 | MLB extra-innings home win rate |
| `DIXON_COLES_TAU` | 0.10 | Soccer low-score correction |
| `soccer_home_adv` | 1.15 | Soccer home xG multiplier |
| `MIN_ML_SAMPLES` | 50 | ML training threshold |

---

## Setup

```powershell
git clone <repo-url>
cd Predicta
.\setup.ps1
```

**Required environment variables** (`.env` file or GitHub Secrets):
```
ANTHROPIC_API_KEY=sk-ant-...      # Claude AI for query parsing and narratives
SPORTSDATA_API_KEY=...            # SportsData.io — richer MLB pitcher stats
ODDS_API_KEY=...                  # The Odds API — market odds for CLV
```

---

## Quick Start

```powershell
.\.venv\Scripts\Activate.ps1

# Baseball
python cli.py predict --sport baseball --query "Dodgers vs Yankees tonight"

# Tennis
python cli.py predict --sport tennis --query "Alcaraz vs Sinner Wimbledon"

# Soccer
python cli.py predict --sport soccer --query "Man City vs Arsenal Premier League"

# Calibration report
python cli.py calibration

# HTML report
python cli.py report
```

---

## API

```powershell
uvicorn app:app --reload
# Docs:   http://127.0.0.1:8000/docs
# Report: http://127.0.0.1:8000/report
```

---

## Signals Reference

| Sport | Signal | Description |
|-------|--------|-------------|
| Baseball | `wrc_plus` | Weighted Runs Created+ (100=avg) |
| Baseball | `starter_fip` | Starter Fielding Independent Pitching |
| Baseball | `bullpen_fip` | Bullpen FIP (derived from team ERA) |
| Baseball | `park_factor` | 3-year park run multiplier |
| Baseball | `platoon_adj` | Handedness matchup boost |
| Tennis | `sqi` | Serve Quality Index (100=tour avg) |
| Tennis | `rqi` | Return Quality Index (100=tour avg) |
| Tennis | `surface_win_rate` | Win rate on current surface |
| Tennis | `form_score` | Recent form (weighted rolling window) |
| Tennis | `glicko2_rating` | Per-surface Glicko-2 rating |
| TT | `aqi` | Service advantage index |
| TT | `rqi` | Return quality index |
| TT | `fatigue_flag` | Intraday matches played |
| TT | `style_matchup` | Looper/chopper/blocker tag |
| Soccer | `elo_rating` | Team Elo rating |
| Soccer | `xg_for_avg5` | Rolling 5-match xG for |
| Soccer | `xg_against_avg5` | Rolling 5-match xG against |
| Soccer | `form_weighted10` | Exp-decayed form, last 10 |
| Soccer | `h2h_decayed` | H2H win rate (time-decayed) |

---

## Paper Mode

All wager sizing is computed for tracking and calibration purposes only.
No real bets are placed or assumed. Any real-money action requires a separate,
explicitly flagged manual step outside this system.

---

## Questions for AI Review (Kimi / Gemini)

If you're reviewing this system, key areas to evaluate:

1. **Baseball run model** — Is the FIP-based Poisson approach sound? Should park factors be applied multiplicatively or additively? Is `STARTER_FRAC=5/9` the right split given modern bullpen usage trends (openers, bulk relievers)?

2. **Bullpen FIP derivation** — The formula `(team_ERA×9 − starter_FIP×5) / 4` assumes starters pitch exactly 5 innings. Is this a reasonable approximation? How would a 2024-era opener strategy affect this?

3. **wRC+ from OPS** — `wRC+ ≈ (OPS / 0.730) × 100` is an approximation. How large is the error vs true wRC+? Could a linear regression on OBP and SLG separately improve this?

4. **Tennis logistic scale** — `scale=40` in `P = 1/(1+exp(−ΔSQI/40))`. Is this well-calibrated? A 40-point SQI gap should map to roughly what win probability?

5. **Elo blend weight** — 70% Poisson + 30% Elo. Is this optimal? When should Elo get more weight (e.g. short series, playoff contexts)?

6. **Kelly fraction** — Quarter-Kelly (0.25) is conservative. Given this is a new model still in calibration, is this appropriate, or should it be lower?

7. **Dixon-Coles τ** — τ=0.10 is a standard starting value. How sensitive are low-score probabilities to this parameter, and how would you calibrate it?
