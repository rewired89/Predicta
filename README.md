# Predicta — Multi-Sport Prediction & Calibration Tracker

Local-first, paper-mode prediction tracker for **Soccer**, **Table Tennis**, and **Tennis**.

## Features

- **Elo ratings** (soccer) with goal-difference multiplier and match-importance scaling
- **Glicko-2 ratings** (tennis/table tennis) per surface
- **Dixon-Coles Poisson model** for soccer score probability matrices with low-score correction
- **Signal store** — open key-value table for any signal (xG, form, H2H, injuries, fatigue…)
- **De-vig market odds** — compares model output against vig-free closing line
- **Kelly Criterion** stake sizing in paper mode (quarter-Kelly, never auto-bets)
- **Calibration metrics** — Brier score, log-loss, reliability curve
- **ML layer** — logistic regression / gradient boosting once 50+ outcomes are recorded
- **Plain-language explanations** on every prediction
- **FastAPI** local REST API + single-page HTML report

## Setup (PowerShell)

```powershell
git clone <repo-url>
cd Predicta
.\setup.ps1
```

## Quick Start

```powershell
# Activate venv
.\.venv\Scripts\Activate.ps1

# Add a match
python cli.py add-match --sport soccer --a "Man City" --b "Arsenal" --date 2024-05-12T15:00:00 --league "Premier League"

# Add signals
python cli.py add-signal --match-id 1 --name xg_for_avg5 --participant "Man City" --value 2.1
python cli.py add-signal --match-id 1 --name xg_against_avg5 --participant "Man City" --value 0.9
python cli.py add-signal --match-id 1 --name form_weighted10 --participant "Man City" --value 0.78

# Add market odds (decimal)
python cli.py add-odds --match-id 1 --book bet365 --price-a 1.85 --price-b 4.20 --price-draw 3.50

# Run prediction
python cli.py predict --match-id 1

# Record outcome
python cli.py record-outcome --match-id 1 --result a --score-a 3 --score-b 1

# Calibration
python cli.py calibration

# HTML report
python cli.py report
```

## API

```powershell
uvicorn app:app --reload
# Docs: http://127.0.0.1:8000/docs
# Report: http://127.0.0.1:8000/report
```

## Signals Reference

| Sport | Signal | Description |
|---|---|---|
| Soccer | `elo_rating` | Team Elo rating |
| Soccer | `xg_for_avg5` | Rolling 5-match xG for |
| Soccer | `xg_against_avg5` | Rolling 5-match xG against |
| Soccer | `form_weighted10` | Exp-decayed form, last 10 |
| Soccer | `rest_days` | Days since last match |
| Soccer | `stakes_dead_rubber_flag` | 1 if match is meaningless |
| Soccer | `h2h_decayed` | H2H win rate (time-decayed) |
| Soccer | `key_player_out_flag` | 1 if key player absent |
| Soccer | `neutral_site_flag` | 1 if neutral venue |
| TT | `glicko2_rating` | Glicko-2 rating |
| TT | `glicko2_rd` | Rating deviation |
| TT | `style_matchup_flag` | Looper/chopper/blocker tag |
| TT | `serve_win_pct_diff` | Serve win % differential |
| TT | `fatigue_flag` | Matches played in prior 24h |
| Tennis | `glicko2_rating` | Per-surface Glicko-2 rating |
| Tennis | `first_serve_win_pct` | First serve win % |
| Tennis | `break_point_conversion_pct` | Break point conversion % |
| Tennis | `fatigue_flag` | Travel/schedule fatigue |

## Data Sources

- **Soccer xG**: Understat / FBref (verify ToS before automating)
- **Soccer odds**: [The Odds API](https://the-odds-api.com) — set `ODDS_API_KEY` env var
- **Table Tennis**: ITTF / TableTennisDaily (style tags: manual entry)
- **Tennis**: ATP/WTA stats, tennisabstract.com for Elo/Glicko reference

> Always verify each source's terms of service before wiring up an automated scraper.

## Paper Mode

All wager sizing is computed for tracking only. No real bets are placed or assumed. Any real-money action requires a separate, explicitly flagged manual step.
