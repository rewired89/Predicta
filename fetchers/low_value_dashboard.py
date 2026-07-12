"""
Low Value engine — dashboard renderer (Kimi review round 6 follow-up).

Separate from the High Value dashboard (app.py's trade_dashboard()) by
design — "Two tabs, two engines. No mixing." Returns a standalone HTML page;
app.py's GET /trade/low-value/dashboard just calls render_low_value_dashboard()
and wraps it in an HTMLResponse.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

from db.database import get_db
from fetchers.low_value_runner import get_runner_status
from fetchers.trading_logger import get_universe_snapshots
from models.trading.shared.signal_calibration import thesis_type_calibration_report, low_value_calibration_readiness


def _pnl_color(val) -> str:
    if val is None:
        return "#64748b"
    return "#22c55e" if val > 0 else ("#ef4444" if val < 0 else "#64748b")


def _pnl_sign(val) -> str:
    if val is None:
        return "—"
    return f"+${val:,.2f}" if val >= 0 else f"-${abs(val):,.2f}"


def get_low_value_brief(days: int = 7) -> dict:
    """
    Read-only recap for the "give me a brief" / "this week" / "yesterday"
    query box on the Low Value page — same DB-only philosophy as the
    dashboard, never triggers a live scan. days controls the closed-trade
    lookback window (1 for "yesterday", 7 for "this week"/default).
    """
    with get_db() as conn:
        closed_rows = conn.execute(
            """
            SELECT symbol, exit_reason, pnl_dollars, lv_thesis_type
            FROM intraday_trades
            WHERE engine = 'low_value' AND is_hypothetical = 1 AND exit_time IS NOT NULL
              AND exit_time >= datetime('now', ? || ' days')
            ORDER BY exit_time DESC
            """,
            (f"-{days}",),
        ).fetchall()
        open_count = conn.execute(
            "SELECT COUNT(*) FROM intraday_trades WHERE engine='low_value' AND is_hypothetical=1 AND exit_time IS NULL"
        ).fetchone()[0]

    closed = [dict(r) for r in closed_rows]
    n_closed = len(closed)
    win_rate = total_pnl = None
    if n_closed:
        wins = sum(1 for t in closed if (t.get("pnl_dollars") or 0) > 0)
        win_rate = round(wins / n_closed, 3)
        total_pnl = round(sum(t.get("pnl_dollars") or 0 for t in closed), 2)

    recent_snapshots = get_universe_snapshots(days=1)
    universe_size = recent_snapshots[0]["symbol_count"] if recent_snapshots else 0

    return {
        "mode": "brief",
        "days": days,
        "universe_size": universe_size,
        "open_positions": open_count,
        "closed_trades": n_closed,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "recent_trades": [
            {
                "symbol": t["symbol"],
                "thesis_type": t.get("lv_thesis_type") or "UNKNOWN",
                "exit_reason": t.get("exit_reason") or "—",
                "pnl_dollars": t.get("pnl_dollars") or 0.0,
            }
            for t in closed[:10]
        ],
    }


def render_low_value_dashboard() -> str:
    with get_db() as conn:
        closed_rows = conn.execute(
            """
            SELECT symbol, side, entry_time, exit_time, pnl_dollars, pnl_pct,
                   exit_reason, entry_score, lv_thesis_type
            FROM intraday_trades
            WHERE engine = 'low_value' AND is_hypothetical = 1 AND exit_time IS NOT NULL
            ORDER BY exit_time DESC LIMIT 50
            """
        ).fetchall()
        open_rows = conn.execute(
            """
            SELECT symbol, side, entry_time, entry_price, entry_score, lv_thesis_type, lv_news_flags
            FROM intraday_trades
            WHERE engine = 'low_value' AND is_hypothetical = 1 AND exit_time IS NULL
            ORDER BY entry_time DESC
            """
        ).fetchall()

    closed = [dict(r) for r in closed_rows]
    open_t = [dict(r) for r in open_rows]
    n_closed = len(closed)

    win_rate = avg_pnl = total_pnl = None
    if n_closed:
        wins = sum(1 for t in closed if (t.get("pnl_dollars") or 0) > 0)
        win_rate = round(wins / n_closed, 3)
        total_pnl = round(sum(t.get("pnl_dollars") or 0 for t in closed), 2)
        avg_pnl = round(total_pnl / n_closed, 2)

    thesis_report = thesis_type_calibration_report(min_trades=3)
    readiness = low_value_calibration_readiness()
    runner_status = get_runner_status()
    # Read-only: the most recently LOGGED universe snapshot, never a live
    # rescan (a rescan is a slow, multi-API-call operation that belongs to
    # the runner's scheduled job or a manual /trade/low-value/scan-now call,
    # not to loading a dashboard page).
    recent_snapshots = get_universe_snapshots(days=1)
    universe_size = recent_snapshots[0]["symbol_count"] if recent_snapshots else 0
    stagnant_filtered = 0
    scan_timing_note = ""
    funnel_note = ""
    data_source_warning = ""
    if recent_snapshots and recent_snapshots[0].get("filter_stats_json"):
        try:
            fstats = json.loads(recent_snapshots[0]["filter_stats_json"])
            stagnant_filtered = fstats.get("stagnant_filtered_count", 0)
            if "elapsed_sec" in fstats:
                scan_timing_note = (
                    f" · last scan took {fstats['elapsed_sec']:.0f}s, evaluated {fstats.get('candidates_evaluated', 0)} candidates"
                    + (" (TIME BUDGET EXCEEDED — partial result)" if fstats.get("time_budget_exceeded") else "")
                )
            if "volume_filtered_count" in fstats:
                funnel_note = (
                    f"Funnel: {fstats.get('candidates_evaluated', 0)} evaluated → "
                    f"{fstats.get('no_bar_data_count', 0)} no-bar-data, "
                    f"{stagnant_filtered} stagnant, {fstats.get('volume_filtered_count', 0)} low-volume, "
                    f"{fstats.get('market_cap_unavailable_count', 0)} cap-unavailable, "
                    f"{fstats.get('market_cap_too_small_count', 0)} cap-too-small, "
                    f"{fstats.get('bankruptcy_filtered_count', 0)} bankruptcy → {universe_size} passed"
                )
                evaluated = fstats.get("candidates_evaluated", 0)
                no_bar_data = fstats.get("no_bar_data_count", 0)
                if evaluated > 0 and no_bar_data / evaluated > 0.2:
                    data_source_warning += (
                        f'<div class="card" style="border-color:#f59e0b;">'
                        f'<div class="card-title" style="color:#f59e0b;">No Bar Data Warning</div>'
                        f'<div style="font-size:.85rem;">{no_bar_data}/{evaluated} candidates ({no_bar_data/evaluated:.0%}) '
                        f'had no daily-bar history from Alpaca — its free-tier feed is IEX only (not SIP), which has thin '
                        f'coverage for illiquid/small-cap names. This silently shrank every scan before this counter existed '
                        f'(2026-07-12) — it is a data-source coverage gap, not a real filter result.</div>'
                        f'</div>'
                    )
                cap_unavailable = fstats.get("market_cap_unavailable_count", 0)
                if evaluated > 0 and cap_unavailable / evaluated > 0.5:
                    data_source_warning += (
                        f'<div class="card" style="border-color:#f59e0b;">'
                        f'<div class="card-title" style="color:#f59e0b;">Data Source Warning</div>'
                        f'<div style="font-size:.85rem;">Market cap was unavailable for {cap_unavailable}/{evaluated} '
                        f'candidates ({cap_unavailable/evaluated:.0%}) — check FINNHUB_API_KEY is set in Railway and that '
                        f'Yahoo Finance isn\'t blocking Railway\'s IP. This is a data-source problem, not a real filter result.</div>'
                        f'</div>'
                    )
        except Exception:
            stagnant_filtered = 0
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    runner_dot = "#22c55e" if runner_status.get("active") else "#ef4444"
    runner_lbl = "Running" if runner_status.get("active") else "Stopped"
    sprint_lbl = (
        f" &nbsp;·&nbsp; <span style=\"color:#f59e0b;\">SPRINT MODE (logging bar {runner_status['sprint_min_score']:.0f}, real threshold {runner_status.get('entry_threshold', 40):.0f})</span>"
        if runner_status.get("data_collection_sprint_mode") else ""
    )

    scan_banner_html = ""
    if runner_status.get("scan_in_progress") or runner_status.get("universe_build_in_progress"):
        started = runner_status.get("last_scan_started_at") or runner_status.get("last_universe_build_started_at")
        scan_banner_html = (
            f'<div class="card" style="border-color:#f59e0b;">'
            f'<div class="card-title" style="color:#f59e0b;">Scan In Progress</div>'
            f'<div style="font-size:.85rem;">Started {started}. This page does not auto-update — refresh in a few minutes.</div>'
            f'</div>'
        )
    elif runner_status.get("last_scan_error"):
        scan_banner_html = (
            f'<div class="card" style="border-color:#ef4444;">'
            f'<div class="card-title" style="color:#ef4444;">Last Scan Failed</div>'
            f'<div style="font-size:.85rem;">{runner_status["last_scan_error"]}</div>'
            f'</div>'
        )
    elif runner_status.get("last_universe_build_error"):
        scan_banner_html = (
            f'<div class="card" style="border-color:#ef4444;">'
            f'<div class="card-title" style="color:#ef4444;">Last Universe Build Failed</div>'
            f'<div style="font-size:.85rem;">{runner_status["last_universe_build_error"]}</div>'
            f'</div>'
        )

    open_rows_html = "".join(
        f"""<div class="trade-row">
              <div class="trade-icon">{"🟢" if t['side']=='long' else "🔴"}</div>
              <div class="trade-info">
                <span class="trade-sym">{t['symbol']}</span>
                <span class="trade-detail">{t['side'].upper()} · score {t.get('entry_score') or '—'} · {t.get('lv_thesis_type') or 'UNKNOWN'}</span>
                <span class="trade-time">since {t['entry_time'][:16]}</span>
              </div>
            </div>"""
        for t in open_t
    ) or '<div class="muted-note">No open Low Value positions.</div>'

    closed_rows_html = "".join(
        f"""<div class="exit-item">
              <span>{t['symbol']} · {t.get('lv_thesis_type') or 'UNKNOWN'} · {t.get('exit_reason') or '—'}</span>
              <span style="color:{_pnl_color(t.get('pnl_dollars'))}">{_pnl_sign(t.get('pnl_dollars'))}</span>
            </div>"""
        for t in closed[:15]
    ) or '<div class="muted-note">No closed Low Value trades yet.</div>'

    thesis_rows_html = "".join(
        f"""<div class="exit-item">
              <span>{row['thesis_type']}</span>
              <span class="exit-count">n={row['n']}{f" · {row['win_rate']:.0%} win" if row.get('win_rate') is not None else ''}</span>
            </div>"""
        for row in thesis_report.get("by_thesis_type", [])
    ) or '<div class="muted-note">No thesis-type data yet.</div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Predicta — Low Value Monitor</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg: #0b1120; --surface: #131d30; --border: #1e2d47;
    --blue: #38bdf8; --green: #22c55e; --amber: #f59e0b;
    --red: #ef4444; --purple: #a78bfa; --text: #e2e8f0; --muted: #64748b;
  }}
  body {{ background: var(--bg); color: var(--text); font-family: system-ui, -apple-system, sans-serif; min-height: 100vh; padding-bottom: 60px; }}
  header {{ background: var(--surface); border-bottom: 1px solid var(--border); padding: 16px 20px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
  .logo {{ font-size: 1.2rem; font-weight: 700; color: var(--purple); }}
  .header-meta {{ margin-left: auto; font-size: .78rem; color: var(--muted); }}
  .runner-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: {runner_dot}; margin-right: 4px; vertical-align: middle; }}
  main {{ max-width: 680px; margin: 0 auto; padding: 20px 16px; display: flex; flex-direction: column; gap: 16px; }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }}
  .card-title {{ font-size: .7rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); margin-bottom: 14px; }}
  .stat-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  .stat-box {{ background: var(--bg); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }}
  .stat-value {{ font-size: 2rem; font-weight: 800; line-height: 1; margin-bottom: 4px; }}
  .stat-label {{ font-size: .7rem; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }}
  .trade-row {{ display: flex; align-items: center; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--border); }}
  .trade-row:last-child {{ border-bottom: none; }}
  .trade-icon {{ font-size: 1.1rem; flex-shrink: 0; width: 22px; text-align: center; }}
  .trade-info {{ flex: 1; min-width: 0; display: flex; flex-direction: column; }}
  .trade-sym {{ font-weight: 700; font-size: .95rem; }}
  .trade-detail {{ font-size: .8rem; color: var(--muted); }}
  .trade-time {{ font-size: .72rem; color: var(--muted); margin-top: 2px; }}
  .exit-item {{ display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid var(--border); font-size: .83rem; }}
  .exit-item:last-child {{ border-bottom: none; }}
  .exit-count {{ color: var(--muted); }}
  .muted-note {{ font-size: .82rem; color: var(--muted); font-style: italic; }}
  footer {{ text-align: center; font-size: .75rem; color: var(--muted); padding: 20px; }}
  a {{ color: var(--blue); }}
</style>
</head>
<body>
<header>
  <div class="logo">Predicta · Low Value Monitor</div>
  <div class="header-meta">
    <span class="runner-dot"></span>{runner_lbl} &nbsp;·&nbsp; {now_str}{sprint_lbl}
  </div>
</header>
<main>

  {scan_banner_html}
  {data_source_warning}

  <div class="card">
    <div class="card-title">Today's Universe</div>
    <div class="stat-grid">
      <div class="stat-box"><div class="stat-value">{universe_size}</div><div class="stat-label">Symbols scanned</div></div>
      <div class="stat-box"><div class="stat-value">{len(open_t)}</div><div class="stat-label">Open positions (max 3)</div></div>
    </div>
    <div style="font-size:.78rem; color:var(--muted); margin-top:10px;">{stagnant_filtered} excluded as stagnant (volatility floor){scan_timing_note}</div>
    {f'<div style="font-size:.76rem; color:var(--muted); margin-top:6px;">{funnel_note}</div>' if funnel_note else ''}
  </div>

  <div class="card">
    <div class="card-title">Closed P&amp;L ({n_closed} trades)</div>
    <div class="stat-grid">
      <div class="stat-box"><div class="stat-value" style="color:{_pnl_color(total_pnl)}">{_pnl_sign(total_pnl)}</div><div class="stat-label">Total P&amp;L</div></div>
      <div class="stat-box"><div class="stat-value">{f"{win_rate:.0%}" if win_rate is not None else "—"}</div><div class="stat-label">Win rate</div></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Open Positions</div>
    {open_rows_html}
  </div>

  <div class="card">
    <div class="card-title">Recent Closed Trades</div>
    {closed_rows_html}
  </div>

  <div class="card">
    <div class="card-title">Win Rate by Thesis Type ({readiness['calibration_quality']})</div>
    {thesis_rows_html}
  </div>

</main>
<footer>Auto-refreshes every 5 minutes &nbsp;·&nbsp; <a href="/trade/dashboard">High Value dashboard</a></footer>
</body>
</html>"""
    return html
