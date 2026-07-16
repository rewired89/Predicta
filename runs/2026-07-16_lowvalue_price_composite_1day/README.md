# Low Value Price-Composite Backtest

Backtests the price/technical half of the Low Value engine's entry composite
(`models/trading/low_value/thesis_tracker.py`) against real historical Alpaca
daily bars, using the `alpaca-trading-backtest` skill's CLI-driven workflow.

**Read `strategy_spec.json` first** — it documents exactly which of the
production engine's 8 signals this backtest can and cannot reproduce, and
why (short version: insider trading / short interest / cash burn / news
sentiment have no historical point-in-time API in this codebase, so only the
4 price-based signals — 55% of the production weight — are backtested here).

## Prerequisites

1. Alpaca CLI installed and authenticated:
   ```bash
   go install github.com/alpacahq/cli/cmd/alpaca@latest   # or: brew install alpacahq/tap/cli
   alpaca profile login --api-key --key $ALPACA_API_KEY --secret $ALPACA_SECRET_KEY
   alpaca doctor   # must pass before continuing
   ```
2. Predicta's Python dependencies installed (`pip install -r requirements.txt`
   from the repo root) — `run.py` imports the real production scoring
   functions, so it needs the same environment the app runs in.
3. For step 1 below only (universe snapshot): the same env vars the live app
   uses — `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, and optionally
   `FINNHUB_API_KEY` / `SEC_EDGAR_USER_AGENT` for full market-cap/bankruptcy
   coverage (the scanner fails-open and still works without them, just with
   a smaller/less-filtered universe — see CLAUDE.md's Finnhub-key incident
   for what a silently-missing key looks like).

## Run it (from the Predicta repo root)

```bash
# 1. Snapshot today's real qualifying Low Value universe (one-time, live data)
python runs/2026-07-16_lowvalue_price_composite_1day/fetch_universe.py

# 2. Run the historical backtest over that universe
cd runs/2026-07-16_lowvalue_price_composite_1day
python run.py --start 2024-01-01 --end 2026-07-15
```

Override any assumption in `config.json` via flags, e.g.
`--target-pct 0.30 --stop-pct -0.20 --max-hold-trading-days 10`.

## Output

`report.md` (Teaching Five + performance table), `summary.json`, `trades.csv`
/ `round_trips.csv` (note: the last row per still-open-at-window-end position
is tagged `exit_reason=window_end_mark` — a mark-to-market for the equity
curve, not a real fill; exclude it from win-rate/trade-count analysis done
outside `summary.json`, which already excludes it), `equity.csv`,
`benchmark_equity.csv` (SPY buy-and-hold), `data_fingerprint.json`,
`warnings.json`, `fee_source.json`, and a regenerated `notes.md` with the
exact parameters and disclosures for that run.

Re-running with different flags on the same universe snapshot is a fair
comparison (same data fingerprint components); re-running `fetch_universe.py`
on a different day gives a different universe and is a different experiment
— note the change in a new run folder rather than overwriting this one, per
the skill's run-lineage convention.
