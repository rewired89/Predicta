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

- **ESPN** is the primary active sports data source
- **FanGraphs** (via pybaseball) enriches MLB starters with SIERA/xFIP/CSW%/O-Swing%/barrel% and teams with real wRC+/wOBA/ISO; falls back to prior season (season-1) when current-year data is unavailable mid-season
- **Baseball Savant** (via pybaseball) enriches starters with barrel%_against/xwOBA_against and pitch arsenal (avg_fb_velo, fastball_pct, whiff%)
- SportsData.io API key is invalid (value = "1") — all SportsData.io calls fail silently; ESPN fallback always runs
- OpenWeatherMap weather data requires `OPENWEATHER_API_KEY` env var (free tier); model runs fine without it
- **PandaScore** (via `fetchers/esports.py`) provides e-sports team data, rankings, and H2H; requires `PANDASCORE_API_KEY` env var; falls back to Claude AI estimates when key is absent

## Active Sports Pipelines

| Sport | Pipeline | Model |
|-------|----------|-------|
| Baseball (MLB) | analyze_baseball.py | Split Poisson F5/L4 + 70/30 Elo blend + FanGraphs SIERA/Savant Statcast enrichment + market comparison layer |
| Tennis (ATP/WTA) | analyze_tennis.py | Nested Markov chain (points→games→sets→match) |
| Soccer | analyze_soccer.py | Dixon-Coles Poisson + Elo |
| Table Tennis (Ping Pong) | analyze_table_tennis.py | Logistic + Glicko2 + 5pp value gate + market comparison layer |
| E-Sports (CS2/LoL/Dota2/Valorant) | analyze_esports.py | 60% Elo (from world ranking) + 40% recent form blend + H2H adjustment + market comparison layer; PandaScore API primary, Claude AI fallback |

## ML Layer Status

- ML model (`models/ml_layer.py`) is **disconnected** — `predict()` is never called in any pipeline
- Will be activated when 100+ resolved predictions exist in DB (MIN_SAMPLES = 100)
- Sport-specific feature schemas are ready: BASEBALL_FEATURES, TENNIS_FEATURES, SOCCER_FEATURES, TABLE_TENNIS_FEATURES

## NRFI Daily Automation

- **Railway is the single writer to `main`** — GitHub Actions workflow was deleted (it raced with Railway's Contents API pushes)
- `tasks/nrfi_auto.py` scheduler runs in-process on Railway: predict 13:00 UTC (9 AM ET), capture 23:00 UTC (7 PM ET), resolve 05:00 UTC (1 AM ET)
- Results pushed to GitHub via Contents API (needs `GITHUB_TOKEN` + `GITHUB_REPO` env vars on Railway)
- `data/nrfi_latest.md` is overwritten every run — always holds the newest scan
- Manual trigger: `GET /nrfi-auto/run?job=predict` (browser-friendly) or POST
- Status: `GET /nrfi-auto/status`; odds debug: `GET /nrfi-auto/odds-diag`
- **Do NOT recreate** `.github/workflows/nrfi_daily.yml` — Railway handles everything

## Open Calibration Issues

- `logistic_scale = 40` in analyze_tennis.py may be too aggressive (see tests/validate_tennis_scale.py output — ~80pp hold-rate gap vs ATP reference of ~20-25pp); needs real match data to confirm
- Weather signals (temp_f, wind_mph, wind_factor, temp_factor, is_dome) are logged to DB and shown in ai_signals but NOT applied to the run model — enable after 50+ baseball predictions validate the effect
- Market comparison layer (`market_comparison` dict) is live in both baseball and TT pipelines; only meaningful when user supplies sportsbook odds in the query (e.g., "NYY -130 vs BOS +110 tonight")
- FanGraphs season-1 fallback active: if 2026 data is unavailable, `_load_fg_pitchers`/`_load_fg_batters` silently fetch 2025 stats; this is expected mid-season and valid for pitcher quality evaluation
- FanGraphs is **dead for automation** (Cloudflare 403 even from residential IPs) — advanced pitcher stats come from the precomputed feature store (`data/nrfi_feature_store.json`, built locally via `scripts/build_feature_store.py` using Baseball Savant). Optional FanGraphs CSV import available for SIERA/CSW%/O-Swing%
- TT 5pp value gate: when real odds provided, PASS recommendation fires unless model edge ≥ 5pp; call `GET /tt-performance` after 10+ resolved predictions to see if edge is real
