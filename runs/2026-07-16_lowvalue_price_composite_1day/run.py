"""
Low Value engine — price-composite backtest (partial reproduction).

Reuses production scoring code from models/trading/low_value/thesis_tracker.py
(the exact signal functions, weights, and entry threshold the live engine
uses) and models/trading/shared/kelly.py (exact position-sizing math). Only
the DATA FETCH and the SIMULATION LOOP are new, run-specific code, per the
alpaca-trading-backtest skill's code-generation rules.

See strategy_spec.json for the full formalized strategy (including every
deviation from the live production engine) and config.json for run
parameters. Run fetch_universe.py first to produce universe.json /
sector_map.json.

Usage (from this run folder, or pass --repo-root explicitly):
    python run.py --start 2024-01-01 --end 2026-07-15 \\
        --universe-file universe.json --sector-map sector_map.json

Requires: the `alpaca` CLI on PATH, authenticated (`alpaca profile login` or
ALPACA_API_KEY/ALPACA_SECRET_KEY env vars), and `alpaca doctor` passing.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean, stdev

RUN_DIR = Path(__file__).resolve().parent
REPO_ROOT = RUN_DIR.parents[1]
sys.path.insert(0, str(REPO_ROOT))

from models.trading.low_value.thesis_tracker import (   # noqa: E402
    SIGNAL_WEIGHTS,
    ENTRY_THRESHOLD as PROD_ENTRY_THRESHOLD,
    label_for_score,
    _score_price_vs_20d_low,
    _score_rsi_14,
    _score_volume_spike,
    _score_sector_relative_strength,
)
from models.trading.shared.kelly import low_value_position_size  # noqa: E402

PRICE_SIGNAL_NAMES = [
    "price_vs_20d_low",
    "rsi_14",
    "volume_spike",
    "sector_relative_strength",
]

DISCLOSURE = """> **Important disclosure**
> This backtest is a hypothetical historical simulation and does not represent actual trading performance. Backtested results do not guarantee future results. Results depend on market-data quality, data feed selection, corporate-action handling, fees, slippage, liquidity, taxes, execution assumptions, and implementation details. This material is for research and educational purposes only and is not investment advice, a recommendation, an offer, or a solicitation to buy or sell securities, options, cryptocurrencies, or any other financial product. All investments involve risk and may lose value. Review Alpaca's disclosures and agreements at [alpaca.markets/disclosures](https://alpaca.markets/disclosures).
>
> Paper trading is a simulated environment. It does not involve real money or actual securities transactions. Paper results may differ from live trading because of fill assumptions, market impact, liquidity, latency, data differences, order handling, fees, and other market conditions."""


def run_cli(args: list[str]) -> str:
    if shutil.which("alpaca") is None:
        raise RuntimeError("`alpaca` CLI not found on PATH. Install per the skill's prerequisites "
                            "(go install github.com/alpacahq/cli/cmd/alpaca@latest) and run `alpaca doctor`.")
    proc = subprocess.run(["alpaca", "--quiet", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"alpaca {' '.join(args)} failed (exit {proc.returncode}):\n{proc.stderr}")
    return proc.stdout


def fetch_daily_bars(symbol: str, start: str, end: str, feed: str, adjustment: str, raw_dir: Path) -> list[dict]:
    """Fetch daily bars via the Alpaca CLI, paginating, and save the raw responses."""
    all_bars: list[dict] = []
    page_token = None
    page_num = 0
    raw_pages = []
    while True:
        args = ["data", "bars", "--symbol", symbol, "--start", start, "--end", end,
                "--timeframe", "1Day", "--feed", feed, "--adjustment", adjustment, "--sort", "asc"]
        if page_token:
            args += ["--page-token", page_token]
        stdout = run_cli(args)
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Could not parse `alpaca data bars` output for {symbol} as JSON — the CLI's response "
                f"shape may have changed. Run `alpaca data bars --schema` and update this parser. "
                f"Raw output:\n{stdout[:500]}"
            ) from e
        raw_pages.append(data)
        bars = data.get("bars") or []
        all_bars.extend(bars)
        page_token = data.get("next_page_token")
        page_num += 1
        if not page_token:
            break

    (raw_dir / f"bars_{symbol}.json").write_text(json.dumps(raw_pages, indent=2))

    normalized = []
    for b in all_bars:
        missing = [k for k in ("t", "o", "h", "l", "c", "v") if k not in b]
        if missing:
            raise RuntimeError(
                f"{symbol}: bar object missing expected field(s) {missing} — CLI schema drift. "
                f"Run `alpaca data bars --schema` and update fetch_daily_bars(). Bar was: {b}"
            )
        normalized.append({
            "t": b["t"], "o": float(b["o"]), "h": float(b["h"]),
            "l": float(b["l"]), "c": float(b["c"]), "v": float(b["v"]),
            "vw": float(b.get("vw", b["c"])),
        })
    return normalized


def write_normalized_csv(symbol: str, bars: list[dict], normalized_dir: Path) -> None:
    path = normalized_dir / f"bars_{symbol}.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["t", "o", "h", "l", "c", "v", "vw"])
        w.writeheader()
        for b in bars:
            w.writerow(b)


def data_fingerprint(raw_dir: Path, params: dict) -> dict:
    h = hashlib.sha256()
    for p in sorted(raw_dir.glob("*.json")):
        h.update(p.read_bytes())
    h.update(json.dumps(params, sort_keys=True).encode())
    return {
        "sha256": h.hexdigest(),
        "params": params,
        "computed_at": datetime.utcnow().isoformat() + "Z",
    }


def price_composite(bars_window: list[dict], sector_window: list[dict]) -> tuple[float | None, dict]:
    """Reimplements ONLY compute_thesis_score's weighting/redistribution algorithm,
    restricted to the 4 price-based signals (see strategy_spec.json). Does not call
    compute_thesis_score() itself, because that function also calls LIVE insider/
    short-interest/cash-burn lookups with no date parameter — calling it here would
    leak today's data into every historical day."""
    raw = {
        "price_vs_20d_low": _score_price_vs_20d_low(bars_window),
        "rsi_14": _score_rsi_14(bars_window),
        "volume_spike": _score_volume_spike(bars_window),
        "sector_relative_strength": _score_sector_relative_strength(bars_window, sector_window),
    }
    available = {k: v for k, v in raw.items() if v is not None}
    if not available:
        return None, {"missing_signals": PRICE_SIGNAL_NAMES}
    weight_sum = sum(SIGNAL_WEIGHTS[k] for k in available)
    composite = sum(SIGNAL_WEIGHTS[k] * score for k, (score, _d) in available.items()) / weight_sum
    detail = {k: {"score": s, "detail": d} for k, (s, d) in available.items()}
    detail["missing_signals"] = [k for k in raw if k not in available]
    return round(composite, 2), detail


@dataclass
class Position:
    symbol: str
    entry_date: str
    entry_price: float
    shares: float
    entry_index: int  # index into the trading-date calendar


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    shares: float
    exit_reason: str

    @property
    def pnl(self) -> float:
        return round((self.exit_price - self.entry_price) * self.shares, 4)

    @property
    def pnl_pct(self) -> float:
        return round((self.exit_price - self.entry_price) / self.entry_price * 100, 4)


def simulate(
    symbols: list[str],
    bars_by_symbol: dict[str, list[dict]],
    sector_bars_by_symbol: dict[str, list[dict]],
    spy_bars: list[dict],
    cfg: dict,
    start: str,
    end: str,
) -> dict:
    calendar = [b["t"][:10] for b in spy_bars if start <= b["t"][:10] <= end]
    calendar = sorted(set(calendar))
    date_to_idx = {d: i for i, d in enumerate(calendar)}

    bars_idx = {sym: {b["t"][:10]: b for b in bars} for sym, bars in bars_by_symbol.items()}
    closes_by_date = {sym: {} for sym in symbols}

    open_positions: dict[str, Position] = {}
    pending_entries: dict[str, list[str]] = {d: [] for d in calendar}   # date -> symbols to fill at open
    pending_exits: dict[str, list[tuple[str, str]]] = {d: [] for d in calendar}  # date -> (symbol, reason)
    scheduled_exit_for: dict[str, str] = {}  # symbol -> date already scheduled, avoid double-scheduling

    trades: list[Trade] = []
    equity_rows: list[dict] = []
    warnings: list[str] = []
    slippage = cfg["slippage_bps"] / 10000.0
    cash = cfg["initial_cash"]

    for i, today in enumerate(calendar):
        # 1. Fill entries scheduled for today's open
        for sym in pending_entries[today]:
            bar = bars_idx.get(sym, {}).get(today)
            if not bar:
                warnings.append(f"{today}: no open bar for {sym}, entry skipped (missing data)")
                continue
            fill_price = bar["o"] * (1 + slippage)
            sized = low_value_position_size(fill_price)
            if "error" in sized:
                warnings.append(f"{today}: sizing error for {sym}: {sized['error']}")
                continue
            shares = sized["shares"]
            open_positions[sym] = Position(sym, today, fill_price, shares, i)

        # 2. Fill exits scheduled for today's open
        for sym, reason in pending_exits[today]:
            pos = open_positions.pop(sym, None)
            if pos is None:
                continue
            bar = bars_idx.get(sym, {}).get(today)
            if not bar:
                warnings.append(f"{today}: no open bar for {sym}, exit skipped (missing data) — position stays open")
                open_positions[sym] = pos
                continue
            fill_price = bar["o"] * (1 - slippage)
            trades.append(Trade(sym, pos.entry_date, pos.entry_price, today, fill_price, pos.shares, reason))
            scheduled_exit_for.pop(sym, None)

        # 3. Mark-to-market equity using today's close
        equity = cash
        for sym, pos in open_positions.items():
            bar = bars_idx.get(sym, {}).get(today)
            close = bar["c"] if bar else pos.entry_price
            closes_by_date[sym][today] = close
            equity += close * pos.shares
        equity_rows.append({"date": today, "equity": round(equity, 4), "open_positions": len(open_positions)})

        next_date = calendar[i + 1] if i < len(calendar) - 1 else None

        # 4. Evaluate exits for currently-open positions using today's close
        if next_date:
            for sym, pos in list(open_positions.items()):
                if sym in scheduled_exit_for:
                    continue
                bar = bars_idx.get(sym, {}).get(today)
                if not bar:
                    continue
                unrealized_pct = (bar["c"] - pos.entry_price) / pos.entry_price
                held_days = i - pos.entry_index
                reason = None
                if unrealized_pct <= cfg["stop_pct"]:
                    reason = "stop_loss"
                elif unrealized_pct >= cfg["target_pct"]:
                    reason = "target_hit"
                elif held_days >= cfg["max_hold_trading_days"]:
                    reason = "time_exit"
                if reason:
                    pending_exits[next_date].append((sym, reason))
                    scheduled_exit_for[sym] = next_date

            # 5. Evaluate entries for symbols not held/pending, in deterministic (alphabetical) order
            slots_free = cfg["max_concurrent_positions"] - len(open_positions) - len(pending_entries[next_date])
            if slots_free > 0:
                for sym in sorted(symbols):
                    if slots_free <= 0:
                        break
                    if sym in open_positions or sym in pending_entries[next_date]:
                        continue
                    bar_hist = bars_idx.get(sym, {})
                    window = [b for d, b in sorted(bar_hist.items()) if d <= today]
                    if len(window) < 20:
                        continue
                    sector_hist = sector_bars_by_symbol.get(sym, [])
                    sector_window = [b for b in sector_hist if b["t"][:10] <= today]
                    composite, _detail = price_composite(window, sector_window)
                    if composite is None or composite < cfg["entry_threshold"]:
                        continue
                    pending_entries[next_date].append(sym)
                    slots_free -= 1

    # Close any still-open positions at the end of the window (mark-to-market exit, not a real fill)
    for sym, pos in open_positions.items():
        bar = bars_idx.get(sym, {}).get(calendar[-1])
        if bar:
            trades.append(Trade(sym, pos.entry_date, pos.entry_price, calendar[-1], bar["c"], pos.shares, "window_end_mark"))

    return {
        "trades": trades,
        "equity_rows": equity_rows,
        "warnings": warnings,
        "calendar": calendar,
    }


def benchmark_curve(spy_bars: list[dict], calendar: list[str], initial_cash: float) -> list[dict]:
    by_date = {b["t"][:10]: b for b in spy_bars}
    first = by_date.get(calendar[0])
    if not first:
        return []
    shares = initial_cash / first["o"]
    rows = []
    for d in calendar:
        b = by_date.get(d)
        if not b:
            continue
        rows.append({"date": d, "equity": round(shares * b["c"], 4)})
    return rows


def compute_metrics(equity_rows: list[dict], trades: list[Trade], initial_cash: float) -> dict:
    if not equity_rows:
        return {"error": "no equity data"}
    values = [r["equity"] for r in equity_rows]
    total_return_pct = round((values[-1] - initial_cash) / initial_cash * 100, 3)
    n_days = len(values)
    years = max(n_days / 252.0, 1e-9)
    ann_return_pct = round(((values[-1] / initial_cash) ** (1 / years) - 1) * 100, 3) if values[-1] > 0 else -100.0

    peak = values[0]
    max_dd = 0.0
    for v in values:
        peak = max(peak, v)
        dd = (v - peak) / peak if peak else 0.0
        max_dd = min(max_dd, dd)

    daily_returns = []
    for a, b in zip(values, values[1:]):
        if a:
            daily_returns.append((b - a) / a)
    if len(daily_returns) >= 2 and stdev(daily_returns) > 0:
        sharpe = round((mean(daily_returns) / stdev(daily_returns)) * (252 ** 0.5), 3)
    else:
        sharpe = None

    real_trades = [t for t in trades if t.exit_reason != "window_end_mark"]
    wins = [t for t in real_trades if t.pnl > 0]
    losses = [t for t in real_trades if t.pnl <= 0]
    win_rate = round(len(wins) / len(real_trades) * 100, 2) if real_trades else None
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    # None (not float("inf")) when there's no loss to divide by, to keep summary.json valid JSON.
    profit_factor = round(gross_win / gross_loss, 3) if gross_loss > 0 else None

    return {
        "total_return_pct": total_return_pct,
        "annualized_return_pct": ann_return_pct,
        "max_drawdown_pct": round(max_dd * 100, 3),
        "sharpe_daily_annualized": sharpe,
        "num_trades": len(real_trades),
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "final_equity": values[-1],
        "gross_win": round(gross_win, 4),
        "gross_loss": round(gross_loss, 4),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe-file", default="universe.json")
    ap.add_argument("--sector-map", default="sector_map.json")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--warmup-calendar-days", type=int, default=40)
    ap.add_argument("--feed", default="iex")
    ap.add_argument("--adjustment", default="split")
    ap.add_argument("--entry-threshold", type=float, default=PROD_ENTRY_THRESHOLD)
    ap.add_argument("--target-pct", type=float, default=0.50)
    ap.add_argument("--stop-pct", type=float, default=-0.50)
    ap.add_argument("--max-hold-trading-days", type=int, default=5)
    ap.add_argument("--max-concurrent-positions", type=int, default=3)
    ap.add_argument("--slippage-bps", type=float, default=5.0)
    ap.add_argument("--initial-cash", type=float, default=100.0)
    ap.add_argument("--benchmark-symbol", default="SPY")
    args = ap.parse_args()

    raw_dir = RUN_DIR / "raw"
    normalized_dir = RUN_DIR / "normalized"
    raw_dir.mkdir(exist_ok=True)
    normalized_dir.mkdir(exist_ok=True)

    universe_data = json.loads((RUN_DIR / args.universe_file).read_text())
    symbols = universe_data["symbols"]
    sector_map = json.loads((RUN_DIR / args.sector_map).read_text())

    from datetime import timedelta
    fetch_start = (datetime.strptime(args.start, "%Y-%m-%d") - timedelta(days=args.warmup_calendar_days)).strftime("%Y-%m-%d")

    print(f"[run] fetching bars for {len(symbols)} symbols + benchmark + sector ETFs, {fetch_start} -> {args.end}")
    bars_by_symbol = {}
    for sym in symbols:
        bars_by_symbol[sym] = fetch_daily_bars(sym, fetch_start, args.end, args.feed, args.adjustment, raw_dir)
        write_normalized_csv(sym, bars_by_symbol[sym], normalized_dir)

    unique_etfs = sorted(set(sector_map.get(s, "SPY") for s in symbols) | {args.benchmark_symbol})
    etf_bars = {}
    for etf in unique_etfs:
        etf_bars[etf] = fetch_daily_bars(etf, fetch_start, args.end, args.feed, args.adjustment, raw_dir)
        write_normalized_csv(etf, etf_bars[etf], normalized_dir)

    sector_bars_by_symbol = {sym: etf_bars[sector_map.get(sym, "SPY")] for sym in symbols}
    spy_bars = etf_bars[args.benchmark_symbol]

    cfg = {
        "entry_threshold": args.entry_threshold,
        "target_pct": args.target_pct,
        "stop_pct": args.stop_pct,
        "max_hold_trading_days": args.max_hold_trading_days,
        "max_concurrent_positions": args.max_concurrent_positions,
        "slippage_bps": args.slippage_bps,
        "initial_cash": args.initial_cash,
    }

    print("[run] simulating...")
    result = simulate(symbols, bars_by_symbol, sector_bars_by_symbol, spy_bars, cfg, args.start, args.end)
    bench_rows = benchmark_curve(spy_bars, result["calendar"], args.initial_cash)

    metrics = compute_metrics(result["equity_rows"], result["trades"], args.initial_cash)
    bench_metrics = compute_metrics(bench_rows, [], args.initial_cash) if bench_rows else {}

    # --- artifacts ---
    with (RUN_DIR / "trades.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "entry_date", "entry_price", "exit_date", "exit_price", "shares", "pnl", "pnl_pct", "exit_reason"])
        for t in result["trades"]:
            w.writerow([t.symbol, t.entry_date, t.entry_price, t.exit_date, t.exit_price, t.shares, t.pnl, t.pnl_pct, t.exit_reason])
    shutil.copy(RUN_DIR / "trades.csv", RUN_DIR / "round_trips.csv")

    with (RUN_DIR / "equity.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "equity", "open_positions"])
        w.writeheader()
        w.writerows(result["equity_rows"])

    with (RUN_DIR / "benchmark_equity.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "equity"])
        w.writeheader()
        w.writerows(bench_rows)

    fingerprint = data_fingerprint(raw_dir, {
        "start": args.start, "end": args.end, "feed": args.feed,
        "adjustment": args.adjustment, "symbols": symbols,
    })
    (RUN_DIR / "data_fingerprint.json").write_text(json.dumps(fingerprint, indent=2))
    (RUN_DIR / "warnings.json").write_text(json.dumps(result["warnings"], indent=2))
    (RUN_DIR / "fee_source.json").write_text(json.dumps({
        "modeled": False,
        "pdf": "https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf",
        "pdf_revision_checked": None,
        "note": "Could not fetch/verify the current SEC Section 31 fee / FINRA TAF rates from this "
                "environment. At $25 notional per trade these fees are sub-cent and are excluded, not "
                "guessed. Verify the PDF's current rates before relying on this for capital-scale decisions.",
    }, indent=2))

    summary = {
        "strategy": metrics,
        "benchmark": bench_metrics,
        "config": {**cfg, "start": args.start, "end": args.end, "symbols_count": len(symbols)},
        "data_fingerprint_sha256": fingerprint["sha256"],
    }
    (RUN_DIR / "summary.json").write_text(json.dumps(summary, indent=2))

    report = f"""# Low Value Price-Composite Backtest — Report

**Performance vs Benchmark**

| | Total Return | Ann. Return | Max Drawdown | Sharpe | Final Equity |
|---|---:|---:|---:|---:|---:|
| **Strategy** | {metrics.get('total_return_pct')}% | {metrics.get('annualized_return_pct')}% | {metrics.get('max_drawdown_pct')}% | {metrics.get('sharpe_daily_annualized')} | ${metrics.get('final_equity')} |
| Benchmark ({args.benchmark_symbol} buy&hold) | {bench_metrics.get('total_return_pct')}% | {bench_metrics.get('annualized_return_pct')}% | {bench_metrics.get('max_drawdown_pct')}% | {bench_metrics.get('sharpe_daily_annualized')} | ${bench_metrics.get('final_equity')} |

- Symbols in universe: {len(symbols)}
- Window: {args.start} -> {args.end}
- Trades: {metrics.get('num_trades')}, win rate {metrics.get('win_rate_pct')}%, profit factor {metrics.get('profit_factor')}
- Fees: not modeled (see fee_source.json)
- Data fingerprint: {fingerprint['sha256'][:16]}...

This is a PARTIAL reproduction of the production Low Value engine (55% of its
signal weight only — see strategy_spec.json for exactly which signals were
excluded and why). It does not include insider-buying, short-interest,
cash-burn, or news-sentiment signals, and does not include the production
engine's news-driven early-exit path.

{DISCLOSURE}
"""
    (RUN_DIR / "report.md").write_text(report)

    notes = f"""# Notes — Low Value Price-Composite Backtest

## Original request
Backtest the Low Value engine's thesis using Alpaca historical data (partial
scope, confirmed with the user: only the 4 price-based signals computable
from daily bars; universe snapshotted live once, held fixed across history).

## Confirmed strategy interpretation
See strategy_spec.json (full formalized rules) and config.json (run
parameters). Summary: long-only entries at composite >= {args.entry_threshold}
(4-signal reweighted subset of production's 8-signal composite), $25 fixed
sizing, max {args.max_concurrent_positions} concurrent positions, exit at
+{args.target_pct*100:.0f}% / {args.stop_pct*100:.0f}% / {args.max_hold_trading_days} trading days, next-open fills with
{args.slippage_bps}bps slippage both directions.

## Data feed and adjustment
Alpaca CLI, feed={args.feed}, adjustment={args.adjustment}, timeframe=1Day.

## Assumptions and deviations from production
See strategy_spec.json's `deviation_from_production`, `universe.assumption`,
`sector_etf_mapping.assumption`, `exit_rule.deviation_from_production`,
`look_ahead_bias_controls`, and `known_biases_not_controlled` fields — all of
that content applies verbatim to this executed run and is not repeated here.

## Data fingerprint
{fingerprint['sha256']}

## Fee schedule
Not modeled — see fee_source.json.

## Caveats
- Survivorship bias from the live-snapshot universe (see strategy_spec.json).
- No walk-forward split; entry/exit constants are production's existing v1
  values, not fit to this window.
- {len(result['warnings'])} data warnings — see warnings.json.

{DISCLOSURE}
"""
    (RUN_DIR / "notes.md").write_text(notes)

    print("\n=== Teaching Five ===")
    print(f"1. Total return vs benchmark: {metrics.get('total_return_pct')}% vs {bench_metrics.get('total_return_pct')}%")
    print(f"2. Max drawdown: {metrics.get('max_drawdown_pct')}%")
    print(f"3. Number of trades: {metrics.get('num_trades')}")
    print(f"4. Win rate: {metrics.get('win_rate_pct')}%")
    print(f"5. Sharpe vs benchmark: {metrics.get('sharpe_daily_annualized')} vs {bench_metrics.get('sharpe_daily_annualized')}")
    print(f"\nArtifacts written to {RUN_DIR}")


if __name__ == "__main__":
    main()
