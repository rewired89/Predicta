# Predicta — Claude Code Instructions

## CodeMap Protocol

- On session start: read CODEMAP.md before touching any code
- After any function or variable change: update its entry in CODEMAP.md
- After adding anything new: add its entry
- After deleting anything: remove its entry
- CODEMAP.md must be committed in the same commit as the code change

## Git Rules

- **Always commit to `main`** — NEVER create a new branch under any circumstance
- Push after every completed task: `git push -u origin main`
- Commit CODEMAP.md in the same commit as any code change

## Data Sources

- **ESPN** is the only active sports data source
- SportsData.io API key is invalid (value = "1") — all SportsData.io calls fail silently; ESPN fallback always runs
- OpenWeatherMap weather data requires `OPENWEATHER_API_KEY` env var (free tier); model runs fine without it

## Active Sports Pipelines

| Sport | Pipeline | Model |
|-------|----------|-------|
| Baseball (MLB) | analyze_baseball.py | Split Poisson F5/L4 + 70/30 Elo blend |
| Tennis (ATP/WTA) | analyze_tennis.py | Nested Markov chain (points→games→sets→match) |
| Soccer | analyze_soccer.py | Dixon-Coles Poisson + Elo |
| Table Tennis | analyze_table_tennis.py | Logistic + Glicko2 |

## ML Layer Status

- ML model (`models/ml_layer.py`) is **disconnected** — `predict()` is never called in any pipeline
- Will be activated when 100+ resolved predictions exist in DB (MIN_SAMPLES = 100)
- Sport-specific feature schemas are ready: BASEBALL_FEATURES, TENNIS_FEATURES, SOCCER_FEATURES, TABLE_TENNIS_FEATURES

## Open Calibration Issues

- `logistic_scale = 40` in analyze_tennis.py may be too aggressive (see tests/validate_tennis_scale.py output — ~80pp hold-rate gap vs ATP reference of ~20-25pp); needs real match data to confirm
- Weather signals (temp_f, wind_mph, wind_factor, temp_factor, is_dome) are logged but NOT applied to the run model — enable after 50+ baseball predictions validate the effect
