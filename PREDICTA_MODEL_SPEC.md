# Predicta — Full Model Specification
> Complete math, variables, algorithms, and pipeline logic for all sports and trading.
> Intended for external AI review and model tuning.
> Last updated: 2026-06-24

---

## Table of Contents
1. [Shared Infrastructure](#1-shared-infrastructure)
2. [Soccer](#2-soccer)
3. [Baseball](#3-baseball)
4. [Tennis](#4-tennis)
5. [Table Tennis](#5-table-tennis)
6. [Trading (Intraday Equities)](#6-trading-intraday-equities)
7. [Known Weaknesses & Open Questions](#7-known-weaknesses--open-questions)

---

## 1. Shared Infrastructure

### 1.1 Elo Rating System (Soccer)
**File:** `models/elo.py`

Standard World Football Elo with goal-difference multiplier.

```
E(A) = 1 / (1 + 10^((R_B - R_A) / 400))

K-factor by match importance:
  World Cup / final     → K = 60
  Continental final     → K = 50
  Continental           → K = 45
  WC qualifier          → K = 40
  Default               → K = 30
  Friendly              → K = 20

Goal-difference multiplier G:
  GD = 0 or 1  → G = 1.0
  GD = 2       → G = 1.5
  GD ≥ 3       → G = (11 + GD) / 8

Rating update:
  R_A_new = R_A + K * G * (actual_A - E(A))
  actual_A = 1 (win), 0.5 (draw), 0 (loss)

Default rating: 1500
```

### 1.2 Glicko-2 Rating System (Tennis, Table Tennis)
**File:** `models/glicko.py`

Used for racket sports (per-surface for tennis, "all" surface for table tennis).

```
Win probability approximation:
  q = ln(10) / 400
  g(RD_B) = 1 / sqrt(1 + 3*(q*RD_B)^2 / π^2)
  E(A) = 1 / (1 + 10^(-g(RD_B) * (R_A - R_B) / 400))

Default values: rating=1500, RD=350, volatility=0.06

Seeding from ranking (when no match history):
  rating = max(1300, 2400 - 400 * log10(max(1, ranking)))
  Rank 1   → ~2400
  Rank 50  → ~2000
  Rank 500 → ~1600
  Rank 999 → ~1300 (clamped minimum)
  RD = 200, vol = 0.06 (high uncertainty)

Surfaces tracked (tennis): hard, clay, grass, all
```

### 1.3 Kelly Criterion Staking
**File:** `models/kelly.py`

```
Full Kelly:  f* = (b*p - q) / b
  b = decimal_odds - 1
  p = model win probability
  q = 1 - p

Applied fraction: Quarter-Kelly (f = 0.25 * f*)
Stake = bankroll * f
Stake = 0 if edge ≤ 0

PAPER MODE ONLY — no real bets placed.
```

### 1.4 De-vig (Fair Probability)
**File:** `models/devig.py`

```
implied(decimal_odds) = 1 / decimal_odds
vig = sum of all implied probs - 1.0
fair_prob(outcome) = implied(outcome) / sum(all_implied)

American → Decimal:
  positive: decimal = american/100 + 1
  negative: decimal = 100/abs(american) + 1

CLV (Closing Line Value):
  CLV = your_model_prob - fair_closing_prob
  Positive = you had an edge over the closing line
```

### 1.5 Data Confidence & Shrinkage
Used in Tennis and Table Tennis when real data cannot be fetched.

```
data_confidence levels:
  "high"   = real stats from API/scraper (ITTF, WTT, ESPN, MLB)
  "medium" = AI recognizes player from training knowledge
  "low"    = AI has no meaningful data (club/regional/qualifier players)

Shrinkage toward 50%:
  shrink_factor = { "high": 1.0, "medium": 0.6, "low": 0.3 }
  prob_a_final = 0.5 + (prob_a_raw - 0.5) * shrink_factor

Effect: a raw edge of 60/40 becomes:
  high   → 60.0% (no shrink)
  medium → 56.0% (40% shrink)
  low    → 53.0% (70% shrink)

PASS rule: if data_confidence == "low" → recommendation = "PASS"
Rationale: book has more info on unknown regional players than we do.
```

---

## 2. Soccer

### 2.1 Data Sources
- **TheSportsDB** (free tier): team form, recent results, league
- **AI fallback** (Claude Haiku): xG estimates, injury flags, form from training knowledge

### 2.2 Signals Used
| Signal | Description | Default |
|--------|-------------|---------|
| `xg_for_avg5` | Average xG scored, last 5 matches | 1.35 |
| `xg_against_avg5` | Average xG conceded, last 5 | 1.35 |
| `form_weighted10` | Weighted win rate, last 10 | 0.5 |
| `rest_days` | Days since last match | - |
| `key_player_out_flag` | 1 if key player injured | 0 |
| `corners_for_avg5` | Avg corners won per game | 5.0 |
| `corners_against_avg5` | Avg corners conceded per game | 4.5 |

### 2.3 Dixon-Coles Poisson Model
**File:** `models/dixon_coles.py`

The primary scoring model. Generates a full (MAX_GOALS+1)² score matrix.

```
Expected goals:
  μ_home = league_avg * attack_home * defense_away * home_advantage
  μ_away = league_avg * attack_away * defense_home

  league_avg = 1.35 goals
  home_advantage = 1.15 (multiplicative, zeroed if neutral site)

Strength derivation from xG signals:
  attack  = xg_for_avg5  / 1.35
  defense = xg_against_avg5 / 1.35
  (1.0 = league average; >1.0 attack = dangerous, >1.0 defense = leaky)

Score probability:
  P(H=h, A=a) = Poisson(h; μ_home) * Poisson(a; μ_away) * τ(h, a)

Dixon-Coles low-score correction τ(h, a):
  (0,0) → 1 - μ_home * μ_away * ρ
  (1,0) → 1 + μ_away * ρ
  (0,1) → 1 + μ_home * ρ
  (1,1) → 1 - ρ
  else  → 1.0
  ρ (tau) = 0.1

Matrix normalized so all cells sum to 1.0.

Outcome probs:
  prob_home = sum of cells where h > a  (lower triangle)
  prob_draw = sum of diagonal cells
  prob_away = sum of cells where a > h  (upper triangle)
```

### 2.4 Final Blend
```
Draw probability: taken entirely from Dixon-Coles (Elo has no draw term)
p_decisive = 1 - prob_draw

Home win ratio within decisive matches:
  dc_home_ratio = dc.prob_home / (dc.prob_home + dc.prob_away)
  blended_ratio = 0.40 * elo_prob_a + 0.60 * dc_home_ratio

prob_a = p_decisive * blended_ratio
prob_b = 1 - prob_a - prob_draw
```

### 2.5 Additional Markets Computed (from score matrix)
- **Correct Score** — top 6 most probable scorelines
- **Match Result 2-Up** — win by 2+ goals
- **Spread** — home handicap ±0.5, ±1.5, ±2.5
- **Winner Push if Tied** — 2-way market
- **Next Shot on Target** — proportional to xG ratio
- **Method of Goal 2** — foot/header/penalty probabilities
- **Corners** — Poisson model using corners_for/against signals; over/under lines, handicap, first corner

---

## 3. Baseball

### 3.1 Data Sources
- **MLB Stats API** (free): starters, team stats, park factors
- **AI fallback** (Claude Haiku): wRC+, FIP, ERA, park factor from training knowledge

### 3.2 Signals Used
| Signal | Description | Source |
|--------|-------------|--------|
| `wrc_plus` | Weighted Runs Created+. 100 = league avg | MLB API |
| `starter_fip` | Fielding Independent Pitching. League avg ≈ 4.20 | MLB API |
| `starter_era` | Earned Run Average | MLB API |
| `park_factor` | Ballpark run factor (1.0 = neutral) | MLB API |
| `record_win_pct` | Season win percentage | MLB API |

### 3.3 Expected Runs Model
**File:** `analyze_baseball.py`

```
League avg FIP = 4.20
League avg wRC+ = 100

Pitching strength (how well pitcher suppresses runs):
  pitch_strength = 1 - (FIP - league_avg_FIP) / league_avg_FIP
  e.g. FIP 3.50 → pitch_strength = 1 - (3.50-4.20)/4.20 = 1.167  (good)
       FIP 5.00 → pitch_strength = 1 - (5.00-4.20)/4.20 = 0.810  (bad)

Offense strength:
  off_strength = wRC+ / 100

Expected runs:
  mu = league_avg_runs * off_strength * pitch_strength * park_factor
  home boost: +5% for home team
  league_avg_runs ≈ 4.5 runs/game
```

### 3.4 Baseball Markets (Poisson)
**File:** `models/baseball_markets.py`

```
P(home scores h, away scores a) = Poisson(h; μ_home) * Poisson(a; μ_away)
Score matrix: 0–15 runs each side

Markets computed:
  Moneyline: P(home wins) = sum cells where h > a
  Run line ±1.5: P(home covers -1.5) = sum cells where h - a > 1.5
  Total (O/U): P(over N) = sum cells where h + a > N
               Lines computed at 7.5, 8.5, 9.5
  First 5 innings: μ scaled to ~55% of full-game μ
```

### 3.5 Final Blend
```
Poisson model: 70% weight
Elo from win%: 30% weight

  Elo seeding: rating = 1500 + (win_pct - 0.5) * 1000
  (requires ≥ 10 games played; otherwise Elo skipped)

prob_a_final = 0.70 * poisson_prob_a + 0.30 * elo_prob_a
prob_b_final = 1 - prob_a_final  (no draw in baseball)
```

---

## 4. Tennis

### 4.1 Data Sources
- **ESPN API**: rankings, recent results
- **TheSportsDB**: player metadata, H2H
- **AI fallback** (Claude Haiku): SQI, RQI, surface win rate from training knowledge

### 4.2 Signals Used
| Signal | Description | Default |
|--------|-------------|---------|
| `serve_quality_index` (SQI) | Composite: 1st serve %, ace rate, double faults. 100 = tour avg | 100 |
| `return_quality_index` (RQI) | Return points won %. 100 = tour avg | 100 |
| `surface_win_rate` | Win rate on current surface (hard/clay/grass) | 0.5 |
| `recent_form` | Win rate last 10 matches | 0.5 |
| `ranking` | ATP/WTA ranking | 999 |

### 4.3 Serve/Return Model
```
SQI/RQI centered on 100 (tour average).

P(A wins) = logistic((SQI_A - RQI_B) - (SQI_B - RQI_A)) / 2
           — net serve edge minus net return edge

logistic(x) = 1 / (1 + e^(-x/40))
scale = 40 → a ±40-point gap ≈ 65%/35% win probability
```

### 4.4 Blend Pipeline (sequential — NOTE: not yet unified like TT)
```
Step 1: Serve model  (60%) + surface win rate (40%)
  prob_surface = 0.60 * prob_serve + 0.40 * (swr_a / (swr_a + swr_b))

Step 2: Form adjustment
  form_prob = form_a / (form_a + form_b)
  prob_form = 0.75 * prob_surface + 0.25 * form_prob

Step 3: Glicko-2 blend
  prob_final = 0.70 * prob_form + 0.30 * glicko_prob_a

Step 4: Confidence shrinkage
  prob_final = 0.5 + (prob_final - 0.5) * shrink_factor
```

**Known issue:** This sequential blending compresses extremes. Every 75/25 and 70/30 step pushes toward 50%. Example: a raw 65% → after two blend steps → ~59.5%. The Table Tennis model was already fixed to a unified single-pass blend; Tennis still uses the old sequential approach.

---

## 5. Table Tennis

### 5.1 Data Sources (priority order)
1. **results.ittf.link** — player profiles, rankings, match history, H2H
2. **worldtabletennis.com** — official WTT player list, ITTF ranking
3. **TheSportsDB** — fallback last-10 results
4. **AI fallback** (Claude Haiku) — AQI, RQI, style, handedness from training knowledge

**Current limitation:** Cloud proxy blocks both ITTF and WTT (403 errors). Works on user's local machine. Eastern European club players (Ukrainian DL, Czech DL, Romanian DL) are not in ITTF top 500 and not on TheSportsDB — all three sources return nothing → data_confidence = "low" → PASS.

### 5.2 Signals Used
| Signal | Description | Default |
|--------|-------------|---------|
| `attack_quality_index` (AQI) | Attack win rate + 3rd-ball win rate. 100 = tour avg | 100 |
| `return_quality_index` (RQI) | Return point win rate. 100 = tour avg | 100 |
| `style` | playing style: attacker/defender/chopper/all-round/penhold | all-round |
| `handedness` | right/left | right |
| `recent_form` | win rate last 10–20 matches | 0.5 |
| `ranking` | ITTF world ranking | 999 |

```
Tour averages (normalization constants):
  AVG_ATTACK_WIN_RATE   = 0.55  (% of rallies won when attacking)
  AVG_3RD_BALL_WIN_RATE = 0.60  (% of points won on serve + 3rd ball)
  AVG_RETURN_WIN_RATE   = 0.45  (% of return points won)

AQI = mean(attack_win_rate/0.55, third_ball_win_rate/0.60) * 100
RQI = (return_win_rate / 0.45) * 100
```

### 5.3 Style × AQI Interaction
Style modifies AQI **at the model input**, not as a post-hoc nudge.
```
_style_aqi_modifier(attacker_style, defender_style):
  defender/chopper vs attacker → AQI_eff = AQI * 0.90  (10% suppression)
  blocker vs attacker          → AQI_eff = AQI * 0.95  (5% suppression)
  all other matchups           → AQI_eff = AQI * 1.00  (no effect)

Applied:
  AQI_A_eff = AQI_A * modifier(style_A, style_B)
  AQI_B_eff = AQI_B * modifier(style_B, style_A)
```

### 5.4 True Serve/Return Matchup Model
```
In table tennis, serve alternates every 2 points (50/50 serve distribution).
We model both halves separately:

P_A_serves  = logistic(AQI_A_eff - RQI_B,  scale=40)
              — A attacks using their serve; B tries to return/defend

P_A_returns = logistic(RQI_A - AQI_B_eff, scale=40)
              — B attacks using their serve; A must defend/return

logistic(x) = 1 / (1 + e^(-x/40))

prob_A_matchup = 0.5 * P_A_serves + 0.5 * P_A_returns

WHY THIS IS BETTER than (AQI_A - RQI_B) - (AQI_B - RQI_A):
  Old formula reduces to: AQI_A + RQI_A - AQI_B - RQI_B
  = just the sum of each player's own stats — completely ignores matchup
  New formula: each logistic is a nonlinear interaction term
  Asymmetric players (high AQI / low RQI) are correctly disadvantaged
  when they must return, not just averaged out.
```

### 5.5 Markov Chain Match Simulation
The core match probability engine. Uses P_A_serves and P_A_returns directly.

```
STATE: (score_a, score_b, served_this_stint, server)
  score_a, score_b = points won in current game (0..11+)
  server = 0 (A serves) or 1 (B serves)
  served_this_stint = how many points served in this 2-point stint (0 or 1)
  Serve switches every 2 points in normal play.

TERMINAL STATES:
  score_a ≥ 11 AND score_a - score_b ≥ 2 → A wins game (return 1.0)
  score_b ≥ 11 AND score_b - score_a ≥ 2 → B wins game (return 0.0)

DEUCE (score_a ≥ 10 AND score_b ≥ 10):
  Serve alternates every 1 point.
  Closed-form: alternating A serves then B serves in 2-point segments
    P(A wins segment) = P_A_serves * P_A_returns
    P(B wins segment) = (1 - P_A_serves) * (1 - P_A_returns)
    P(deuce again)    = 1 - above two
    P(A wins from deuce) = P(A_seg) / (P(A_seg) + P(B_seg))

GAME PROBABILITY:
  Average over both starting server possibilities (toss is 50/50):
  P_game_A = 0.5 * dp(0,0,0,A_serves) + 0.5 * dp(0,0,0,B_serves)

MATCH PROBABILITY (best-of-7):
  games_needed = 4
  DP over (games_a, games_b) space using P_game_A per game.
  Output: full score distribution e.g. {"4-0": 0.12, "4-1": 0.28, ...}
         + prob_a, prob_b, expected_games

WHY MARKOV BEATS A FLAT WIN%:
  A player who wins 55% of individual points wins 73% of games to 11.
  A player who wins 53% of points wins 62% of games.
  The nonlinear amplification is captured exactly.
  The score distribution tells us if the match is likely to go long (5+ games)
  — useful for totals markets and fatigue estimation.
```

### 5.6 Handedness Edge
```
Left-handed players have structural crossover advantage vs right-handers.
ITTF analytics (2019): lefties beat equal-ranked righties ~54%.
  A is lefty, B is righty → +0.04 nudge to prob_A
  B is lefty, A is righty → -0.04 nudge to prob_A
  Same handedness         → 0.00
```

### 5.7 First-Time Premium
```
When H2H = 0 (players have never met):
  Unconventional styles (penhold, chopper, defender, long pips, anti)
  gain +3pp because the opponent has no film to adapt a game plan.

Unconventional set: {"penhold", "chopper", "defender", "long pips", "anti"}
  A is unconventional, B is not → +0.03 to prob_A
  B is unconventional, A is not → -0.03 to prob_A
  Both or neither unconventional → 0.00
```

### 5.8 Fatigue Adjustment
```
Each extra match played today (before this one) costs ~3pp.
  delta = matches_today_B - matches_today_A
  fatigue_nudge = delta * 0.03
  (positive = A is fresher)
```

### 5.9 Line Movement Signal
```
Sharp money signal from opening → current line movement.
  Threshold: only fires if |implied probability shift| ≥ 10pp
  Nudge = clamp(line_move * 0.5, -0.08, +0.08)

Implied probability from American odds:
  positive: p = 100 / (american + 100)
  negative: p = |american| / (|american| + 100)

De-vig applied before measuring movement.
```

### 5.10 Unified Weighted Blend
```
All signals enter one weighted blend (no sequential chaining).

W_MATCHUP = 0.40  → Markov simulation + handedness + first-time premium
W_FORM    = 0.20  → recent win rate
W_GLICKO  = 0.30  → Glicko-2 seeded from ITTF ranking
W_CONTEXT = 0.10  → fatigue + line movement (split equally)

form_prob_A = form_A / (form_A + form_B)
fatigue_prob = clamp(0.5 + fatigue_nudge, 0.05, 0.95)
line_prob    = clamp(0.5 + line_nudge, 0.05, 0.95)

prob_A = 0.40 * prob_markov_hand_ft
       + 0.20 * form_prob_A
       + 0.30 * glicko_prob_A
       + 0.10 * 0.5 * (fatigue_prob + line_prob)

WHY UNIFIED:
  Sequential 75/25 then 70/30 blend compresses a 65% signal to ~58%.
  Unified blend preserves the full signal magnitude.
```

### 5.11 Full Pipeline Summary (Table Tennis)
```
1.  Parse query (Claude Haiku)
2.  Fetch: ITTF → WTT → TSDB → AI fallback
3.  Style × AQI modifier
4.  P_A_serves, P_A_returns (logistic)
5.  Markov Chain match simulation → prob_markov
6.  Handedness edge → prob_hand
7.  First-time premium → prob_ft
8.  Glicko-2 from ranking
9.  Form probability
10. Fatigue + line movement
11. Unified weighted blend → prob_A
12. Confidence shrinkage
13. PASS rule if low confidence
14. Persist to DB (matches, signals, predictions tables)
15. Quarter-Kelly stake
16. Narrative (Claude Haiku)
```

---

## 6. Trading (Intraday Equities)

### 6.1 Data Sources
- **Alpaca Markets API** (paper account): real-time bars, snapshots, positions
- **Market movers**: top gainers/losers from Alpaca screener

### 6.2 Intraday Signal Engine
**File:** `models/trading/intraday.py`

10 independent signals, each scored on its own scale. Combined into a composite score.

#### Signal 1 — VWAP Deviation
```
VWAP = Σ(typical_price * volume) / Σ(volume)
typical_price = (high + low + close) / 3

deviation_pct = (price - VWAP) / VWAP * 100

Scoring:
  > +1.5%  → score = -20  (overextended above, mean-reversion risk)
  > +0.3%  → score = +15  (bullish, above VWAP)
  ±0.3%   → score =   0  (neutral, at VWAP)
  < -0.3%  → score = -15  (bearish, below VWAP)
  < -1.5%  → score = +20  (oversold, potential bounce)
```

#### Signal 2 — Opening Range Breakout
```
Opening range = first 3 bars × 5-min = first 15 minutes
OR_high = max(high) over first 15 min
OR_low  = min(low) over first 15 min

If price > OR_high:
  score = min(25, (price - OR_high) / OR_range * 100 * 5)  → bullish
If price < OR_low:
  score = -min(25, (OR_low - price) / OR_range * 100 * 5) → bearish
Else: score = 0  (inside range)
```

#### Signal 3 — RSI-9
```
Gains/losses over last 9 periods.
avg_gain = mean of positive deltas
avg_loss = mean of absolute negative deltas
RS = avg_gain / avg_loss
RSI = 100 - 100 / (1 + RS)

Scoring:
  RSI > 70 → score = -20  (overbought)
  RSI > 55 → score = +10  (bullish momentum)
  RSI 45–55 → score = 0   (neutral)
  RSI < 45 → score = -10  (bearish)
  RSI < 30 → score = +15  (oversold, potential bounce)
```

#### Signal 4 — Relative Volume
```
rel_vol = today_volume / avg_daily_volume_20day

Scoring:
  ≥ 3.0x → score = +20  (very high — strong institutional interest)
  ≥ 1.5x → score = +10  (elevated)
  ≥ 0.8x → score =  0   (normal)
  < 0.8x → score = -10  (thin volume — avoid)
```

#### Signal 5 — Gap Fill
```
gap_pct = (open - prev_close) / prev_close * 100

Gap up (open > prev_close):
  Gap > 2%:  score = -15  (extended gap, risk of fill)
  Gap 0–2%:  score = +10  (healthy gap, trend continuation)

Gap down (open < prev_close):
  Gap < -2%: score = +10  (washout, potential bounce)
  Gap -2–0%: score =  -5  (bearish open)
```

#### Signal 6 — ATR-Based Levels
```
ATR = mean True Range over last 14 bars
True Range = max(high-low, |high-prev_close|, |low-prev_close|)

Outputs:
  stop_long  = current_price - 1.5 * ATR
  stop_short = current_price + 1.5 * ATR
  target_1   = current_price ± 2.0 * ATR
  target_2   = current_price ± 3.0 * ATR

ATR is also used in Kelly position sizing:
  atr_pct = ATR / price * 100
  risk_per_share = 1.5 * ATR
```

#### Signal 7 — Trend Bias (Daily MA)
```
MA20 = 20-day simple moving average of closes
MA50 = 50-day SMA (if available)

Price > MA20:         score = +10  (above daily trend)
Price > MA20 > MA50:  score = +20  (strong uptrend)
Price < MA20:         score = -10  (below daily trend)
Price < MA20 < MA50:  score = -20  (strong downtrend)
```

#### Signal 8 — Bollinger %B
```
middle = 20-period SMA
std    = 20-period standard deviation
upper  = middle + 2*std
lower  = middle - 2*std

%B = (price - lower) / (upper - lower)

Scoring:
  %B > 1.0  → score = -20  (above upper band, overbought)
  %B > 0.8  → score = -10  (near upper band)
  %B 0.4–0.6 → score = 0   (middle zone)
  %B < 0.2  → score = +10  (near lower band)
  %B < 0.0  → score = +20  (below lower band, oversold)
```

#### Signal 9 — Price vs Previous Close
```
change_pct = (current_price - prev_close) / prev_close * 100

Scoring:
  > +3%:   score = -10  (overextended)
  > +1%:   score = +10  (bullish)
  ±1%:    score =   0   (flat)
  < -1%:   score = -10  (bearish)
  < -3%:   score = +10  (potential reversal)
```

#### Signal 10 — Volume Surge (last N bars)
```
surge_vol = volume of last 5 bars
recent_avg = average volume of prior 10 bars

surge_ratio = surge_vol / recent_avg

Scoring:
  ≥ 2.0x → score = +15  (buying surge)
  ≥ 1.3x → score =  +5
  < 0.7x → score = -10  (drying up)
```

### 6.3 Composite Score
```
composite_score = weighted sum of all 10 signals
  VWAP:           weight 1.5x
  Opening Range:  weight 1.5x
  RSI:            weight 1.0x
  Rel Volume:     weight 1.0x
  Gap:            weight 0.8x
  ATR levels:     informational only (no score contribution)
  Trend bias:     weight 1.2x
  Bollinger %B:   weight 1.0x
  Price change:   weight 0.8x
  Volume surge:   weight 0.8x

Score → Label:
  ≥ +40:  STRONG BUY
  ≥ +15:  BUY
  ≥  -15: NEUTRAL
  ≥  -40: SELL
  < -40:  STRONG SELL
```

### 6.4 Kelly Position Sizing (Trading)
**File:** `models/trading/kelly.py`

```
Converts intraday score to win probability, then applies Kelly.

score_to_prob(score):
  Uses logistic function calibrated so:
  score = +40 → p ≈ 0.65
  score =   0 → p = 0.50
  score = -40 → p ≈ 0.35

Kelly stake:
  b = expected reward-to-risk ratio (target / stop distance)
    = (2.0 * ATR) / (1.5 * ATR) = 1.33  (target_1 / stop)
  f* = (b*p - q) / b
  Applied: 20% Kelly for intraday (more conservative than sports)
  Stake in dollars = bankroll * f * 0.20

Risk floor: never risk more than 2% of bankroll on one trade.
```

### 6.5 Order Types Supported (Alpaca paper)
- Market order
- Limit order
- Stop-limit order
- Bracket order (limit entry + take-profit + stop-loss OCO)

---

## 7. Known Weaknesses & Open Questions

### Soccer
- xG signals from AI fallback are estimates from training knowledge, not real season data
- Dixon-Coles τ=0.1 is fixed; not fitted to league-specific low-score correlation
- No live in-play model — static pre-match only
- Elo K-factor choices are hardcoded, not calibrated to this dataset

### Baseball
- Park factor comes from MLB API or AI estimate — not computed from actual splits
- No bullpen model (only starting pitcher FIP)
- No platoon splits (LHP vs RHB, etc.)
- Poisson independence assumption breaks when starters have high innings limits

### Tennis
- **Sequential blend still in place** — should be unified like TT model
- No match-play Markov simulation (unlike TT) — just a flat win probability
- Surface win rate from TheSportsDB is often missing for lower-ranked players
- No serve/return breakdown by surface (all-surface average only)

### Table Tennis
- **Club circuit players (Eastern European, Asian club leagues) return no data from any source** — model outputs PASS for all of them
- AQI/RQI are always 100 (tour average) because no data source exposes rally-level stats
- Markov simulation uses the same P_serve and P_return for every game in a match — in reality these change with scoreline and psychological pressure
- β-coefficients in the logistic functions (scale=40) are principled guesses, not fitted to historical data
- Weights (40/20/30/10) are principled estimates, not empirically validated
- No source currently covers: clutch factor (9-9 win%), rubber/grip type, travel/altitude

### Trading
- Score-to-probability mapping is not backtested
- Kelly fraction (20%) is a conservative guess, not fitted to historical win rate
- All trading is paper mode; real execution costs (slippage, commissions) not modeled
- No earnings calendar or macro event filter — signals may be meaningless around news

### Global
- No backtesting framework yet — weights and parameters are theoretically motivated
- Calibration module exists (`models/calibration.py`) but requires enough recorded outcomes to be meaningful
- All models are pre-match only — no live/in-play updating
