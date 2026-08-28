"""
Automaton engine — dashboard renderer (2026-08-28).

Separate page from Low Value's (fetchers/low_value_dashboard.py) — same
visual language, but leads with the two things that make this engine
different: it buys/sells on its own, and it shows what (if anything) it has
learned from its own win/loss history so far. See fetchers/automaton_runner.py
and models/trading/automaton/learning.py for the underlying logic.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

from db.database import get_db
from fetchers.automaton_runner import (
    get_runner_status, AUTOMATON_TARGET_PCT, AUTOMATON_STOP_PCT, AUTOMATON_MAX_HOLD_DAYS, ENGINE,
)
from fetchers.low_value_runner import _trading_days_elapsed, _et_now
from fetchers.finnhub import get_cached_company_name
from models.trading.automaton.learning import learning_summary

PHASE_TARGETS: dict[str, int] = {
    "insufficient": 20, "preliminary": 50, "dynamic_weights": 100, "empirical_sizing": 100,
}


def _display_name(symbol: str) -> str:
    name = get_cached_company_name(symbol)
    return f"{symbol} · {name}" if name else symbol


def _pnl_color(val) -> str:
    if val is None:
        return "#64748b"
    return "#22c55e" if val > 0 else ("#ef4444" if val < 0 else "#64748b")


def _pnl_sign(val) -> str:
    if val is None:
        return "—"
    return f"+${val:,.2f}" if val >= 0 else f"-${abs(val):,.2f}"


def _target_stop_prices(entry_price: float, side: str) -> dict:
    if side == "short":
        target = entry_price * (1 - AUTOMATON_TARGET_PCT)
        cap = entry_price * (1 + AUTOMATON_STOP_PCT)
    else:
        target = entry_price * (1 + AUTOMATON_TARGET_PCT)
        cap = entry_price * (1 - AUTOMATON_STOP_PCT)
    return {"target_price": round(target, 2), "catastrophe_cap_price": round(cap, 2)}


def _time_exit_progress(entry_time_iso: str) -> dict:
    days_held = min(_trading_days_elapsed(entry_time_iso, _et_now()), AUTOMATON_MAX_HOLD_DAYS)
    return {"days_held": days_held, "hold_limit_days": AUTOMATON_MAX_HOLD_DAYS, "days_remaining": max(AUTOMATON_MAX_HOLD_DAYS - days_held, 0)}


def _phase_and_verdict(n_total: int, win_rate, total_pnl) -> dict:
    """Same 20/50/100-trade phase cascade every paper-trading engine in this codebase uses (see low_value_dashboard.py: _phase_and_verdict)."""
    if n_total < 20:
        phase, phase_color, phase_label = "insufficient", "#f59e0b", "Watching"
        phase_desc = f"Automaton is scanning and trading on its own. You need at least 20 closed trades before even a preliminary read exists. You have {n_total} so far."
    elif n_total < 50:
        phase, phase_color, phase_label = "preliminary", "#f59e0b", "Preliminary Data"
        phase_desc = f"Enough for an early per-thesis-type read, but not enough for the learning loop to start reweighting yet. At 50 trades, weight recalibration unlocks. You have {n_total}."
    elif n_total < 100:
        phase, phase_color, phase_label = "dynamic_weights", "#38bdf8", "Learning Active"
        phase_desc = f"Automaton is now reweighting its 8 signals toward what has actually been winning (see 'What It Has Learned' below). You have {n_total}."
    else:
        phase, phase_color, phase_label = "empirical_sizing", "#a78bfa", "Mature"
        phase_desc = "Enough data collected for the learning loop to be well past its earliest, noisiest phase."

    phase_target = PHASE_TARGETS[phase]
    progress_pct = min(100, int(n_total / phase_target * 100)) if phase_target else 100

    if n_total < 20:
        verdict_color, verdict_icon, verdict_title = "#f59e0b", "🔴", "Not ready for real money"
        verdict_msg = f"You need {20 - n_total} more closed trades before even a preliminary read exists."
    elif win_rate is not None and win_rate < 0.50:
        verdict_color, verdict_icon, verdict_title = "#ef4444", "🔴", "Not ready — win rate too low"
        verdict_msg = f"Win rate of {win_rate*100:.0f}% means it's wrong more than it's right so far. It keeps trading and learning regardless — no kill switch — but this isn't graduation-ready yet."
    elif win_rate is not None and win_rate >= 0.55 and total_pnl is not None and total_pnl > 0 and n_total >= 50:
        verdict_color, verdict_icon, verdict_title = "#22c55e", "🟢", "Consider graduating to real money"
        verdict_msg = f"Win rate {win_rate*100:.0f}% with positive total P&L across {n_total} trades. Real markets are harder than paper — start small."
    elif win_rate is not None and win_rate >= 0.52:
        verdict_color, verdict_icon, verdict_title = "#38bdf8", "🔵", "Getting there"
        verdict_msg = f"Win rate {win_rate*100:.0f}% is a slight edge but not enough to be confident yet."
    else:
        verdict_color, verdict_icon, verdict_title = "#f59e0b", "🟡", "Too early to tell"
        verdict_msg = "Not enough closed trades yet, or the numbers are mixed. It keeps running either way."

    return {
        "phase": phase, "phase_color": phase_color, "phase_label": phase_label, "phase_desc": phase_desc,
        "phase_target": phase_target, "progress_pct": progress_pct,
        "verdict_color": verdict_color, "verdict_icon": verdict_icon, "verdict_title": verdict_title, "verdict_msg": verdict_msg,
    }


def render_automaton_dashboard() -> str:
    with get_db() as conn:
        closed_rows = conn.execute(
            """
            SELECT symbol, side, entry_time, exit_time, entry_price, exit_price, pnl_dollars, pnl_pct,
                   exit_reason, entry_score, lv_thesis_type
            FROM intraday_trades
            WHERE engine = ? AND exit_time IS NOT NULL
            ORDER BY exit_time DESC LIMIT 50
            """,
            (ENGINE,),
        ).fetchall()
        open_rows = conn.execute(
            """
            SELECT id, symbol, side, entry_time, entry_price, entry_score, lv_thesis_type,
                   is_hypothetical, alpaca_order_id, qty
            FROM intraday_trades
            WHERE engine = ? AND exit_time IS NULL
            ORDER BY entry_time DESC
            """,
            (ENGINE,),
        ).fetchall()

    closed = [dict(r) for r in closed_rows]
    open_t = [dict(r) for r in open_rows]
    n_closed = len(closed)

    win_rate = total_pnl = None
    if n_closed:
        wins = sum(1 for t in closed if (t.get("pnl_dollars") or 0) > 0)
        win_rate = round(wins / n_closed, 3)
        total_pnl = round(sum(t.get("pnl_dollars") or 0 for t in closed), 2)

    learning = learning_summary()
    readiness = learning["readiness"]
    pv = _phase_and_verdict(readiness["total_closed_trades"], win_rate, total_pnl)
    runner_status = get_runner_status()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    runner_dot = "#22c55e" if runner_status.get("active") else "#ef4444"
    runner_lbl = "Running" if runner_status.get("active") else "Stopped"
    sprint_lbl = (
        f" &nbsp;·&nbsp; <span style=\"color:#f59e0b;\">SPRINT MODE (logging bar {runner_status['sprint_min_score']:.0f}, real threshold {runner_status.get('entry_threshold', 40):.0f})</span>"
        if runner_status.get("data_collection_sprint_mode") else ""
    )

    scan_banner_html = ""
    if runner_status.get("scan_in_progress"):
        scan_banner_html = f'<div class="card" style="border-color:#f59e0b;"><div class="card-title" style="color:#f59e0b;">Scan In Progress</div><div style="font-size:.85rem;">Started {runner_status.get("last_scan_started_at")}. Refresh in a few minutes.</div></div>'
    elif runner_status.get("last_scan_error"):
        scan_banner_html = f'<div class="card" style="border-color:#ef4444;"><div class="card-title" style="color:#ef4444;">Last Scan Failed</div><div style="font-size:.85rem;">{runner_status["last_scan_error"]}</div></div>'

    def _action_html(t: dict) -> str:
        tid = t.get("id")
        if not t.get("is_hypothetical", 1):
            return (
                f'<span class="real-badge">🤖 AUTO-EXECUTED — real paper order</span> '
                f'<button class="trade-btn sell-btn" onclick="predictaAction(\'/trade/automaton/close/{tid}\', \'SELL — close this position now\')">Sell Now</button>'
            )
        return (
            f'<span class="real-badge" style="color:#f59e0b;">⚠️ AUTO-EXECUTION FAILED</span> '
            f'<button class="trade-btn buy-btn" onclick="predictaAction(\'/trade/automaton/execute/{tid}\', \'Retry placing this order\')">Retry Order</button>'
        )

    open_rows_html = "".join(
        f"""<div class="trade-row">
              <div class="trade-icon">{"🟢" if t['side']=='long' else "🔴"}</div>
              <div class="trade-info">
                <span class="trade-sym">{"BUY" if t['side']=='long' else "SHORT"} {_display_name(t['symbol'])}</span>
                <span class="trade-detail">Bought at ${t['entry_price']:,.2f}/share · score {t.get('entry_score') or '—'}/100 · thesis: {(t.get('lv_thesis_type') or 'UNKNOWN').replace('_',' ').lower()}</span>
                <span class="trade-detail">Target ${_target_stop_prices(t['entry_price'], t['side'])['target_price']:,.2f} · Catastrophe cap ${_target_stop_prices(t['entry_price'], t['side'])['catastrophe_cap_price']:,.2f} · {_time_exit_progress(t['entry_time'])['days_remaining']} of {AUTOMATON_MAX_HOLD_DAYS} trading days remaining</span>
                <span class="trade-time">opened {t['entry_time'][:16]} UTC — fixed-size paper position</span>
                <div style="margin-top:8px;">{_action_html(t)}</div>
              </div>
            </div>"""
        for t in open_t
    ) or '<div class="muted-note">No open Automaton positions right now — nothing crossed the entry bar on the last scan.</div>'

    closed_rows_html = "".join(
        f"""<div class="exit-block">
              <div class="exit-item" style="border-bottom:none; padding:0;">
                <span>{"BUY" if t.get('side')=='long' else "SHORT"} {_display_name(t['symbol'])} · {(t.get('lv_thesis_type') or 'UNKNOWN').replace('_',' ').lower()} · {(t.get('exit_reason') or '').replace('_',' ').lower()}</span>
                <span style="color:{_pnl_color(t.get('pnl_dollars'))}">P&amp;L: {_pnl_sign(t.get('pnl_dollars'))}</span>
              </div>
              <div style="font-size:.76rem; color:var(--muted); margin-top:2px;">
                ${t['entry_price']:,.2f} → ${t['exit_price']:,.2f}/share ({t.get('pnl_pct') or 0:+.1f}%)
              </div>
            </div>"""
        for t in closed[:15]
    ) or '<div class="muted-note">No closed Automaton trades yet.</div>'

    thesis_rows_html = "".join(
        f"""<div class="exit-item"><span>{row['thesis_type']}</span><span class="exit-count">n={row['n']}{f" · {row['win_rate']:.0%} win" if row.get('win_rate') is not None else ''}</span></div>"""
        for row in learning.get("by_thesis_type", {}).get("by_thesis_type", [])
    ) or '<div class="muted-note">No thesis-type data yet.</div>'

    static_w = learning["static_starting_weights"]
    current_w = learning["current_weights"]
    weight_rows_html = "".join(
        f"""<div class="exit-item"><span>{sig}</span><span class="exit-count">{current_w.get(sig,0)*100:.1f}%{f" (started at {static_w.get(sig,0)*100:.1f}%)" if learning['is_learned'] else ""}</span></div>"""
        for sig in static_w
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Predicta — Automaton Monitor</title>
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
  .trade-btn {{ background: var(--blue); color: #04121f; border: none; border-radius: 8px; padding: 8px 14px; font-size: .82rem; font-weight: 700; cursor: pointer; }}
  .trade-btn:hover {{ filter: brightness(1.1); }}
  .buy-btn {{ background: var(--green); }}
  .sell-btn {{ background: var(--red); color: #fff; }}
  .real-badge {{ display: inline-block; font-size: .7rem; font-weight: 700; color: var(--red); margin-right: 8px; }}
  .exit-item {{ display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid var(--border); font-size: .83rem; }}
  .exit-item:last-child {{ border-bottom: none; }}
  .exit-block {{ padding: 7px 0; border-bottom: 1px solid var(--border); }}
  .exit-block:last-child {{ border-bottom: none; }}
  .exit-count {{ color: var(--muted); }}
  .muted-note {{ font-size: .82rem; color: var(--muted); font-style: italic; }}
  footer {{ text-align: center; font-size: .75rem; color: var(--muted); padding: 20px; }}
  a {{ color: var(--blue); }}
  .nav-row {{ display: flex; gap: 10px; align-items: center; font-size: .8rem; color: var(--muted); flex-wrap: wrap; }}
  .nav-row a {{ color: var(--muted); text-decoration: none; }}
  .nav-row a:hover {{ color: var(--blue); text-decoration: underline; }}
  .phase-banner {{ border-radius: 12px; padding: 20px; border: 2px solid {pv['phase_color']}; background: color-mix(in srgb, {pv['phase_color']} 8%, var(--surface)); }}
  .phase-name {{ font-size: 1.4rem; font-weight: 800; color: {pv['phase_color']}; margin-bottom: 4px; }}
  .phase-desc {{ font-size: .9rem; line-height: 1.55; }}
  .progress-wrap {{ background: var(--border); border-radius: 99px; height: 8px; margin-top: 16px; overflow: hidden; }}
  .progress-bar {{ height: 100%; border-radius: 99px; background: {pv['phase_color']}; width: {pv['progress_pct']}%; transition: width .4s; }}
  .progress-label {{ font-size: .72rem; color: var(--muted); margin-top: 6px; text-align: right; }}
  .verdict-card {{ border: 2px solid {pv['verdict_color']}; border-radius: 12px; padding: 20px; background: color-mix(in srgb, {pv['verdict_color']} 6%, var(--surface)); }}
  .verdict-icon {{ font-size: 2rem; margin-bottom: 8px; }}
  .verdict-title {{ font-size: 1.15rem; font-weight: 700; color: {pv['verdict_color']}; margin-bottom: 10px; }}
  .verdict-body {{ font-size: .88rem; line-height: 1.6; }}
</style>
</head>
<body>
<header>
  <div class="logo">Predicta · Automaton Monitor</div>
  <div class="header-meta"><span class="runner-dot"></span>{runner_lbl} &nbsp;·&nbsp; {now_str}{sprint_lbl}</div>
</header>
<main>

  <div class="nav-row">
    <a href="/">← Home</a><span>·</span>
    <a href="/trading">Stock Market</a><span>·</span>
    <a href="/trade/low-value/dashboard">Low Value dashboard</a><span>·</span>
    <a href="/trade/dashboard">High Value dashboard</a>
  </div>

  {scan_banner_html}

  <div class="card" style="border-color:var(--purple);">
    <div class="card-title" style="color:var(--purple);">What Automaton Is</div>
    <div style="font-size:.85rem; line-height:1.6;">
      This engine scans for underrated stocks daily, buys and sells them <strong>on its own — no click required</strong> — using Alpaca's real paper-trading account (fake money, real order mechanics), and holds up to {AUTOMATON_MAX_HOLD_DAYS} trading days (~6 months) looking for a bigger move than the 5-day Low Value engine targets.
      It never places a real-money order by itself — that stays 100% human-gated, unchanged, on every other engine in this app.
      It has <strong>no kill switch</strong> (a losing streak doesn't stop it — it keeps trading and learning) and <strong>does not clone or scale itself</strong> — by explicit design choice, not because either would be hard to build.
    </div>
  </div>

  <div class="phase-banner">
    <div class="phase-name">{pv['phase_label']}</div>
    <div class="phase-desc">{pv['phase_desc']}</div>
    <div class="progress-wrap"><div class="progress-bar"></div></div>
    <div class="progress-label">{readiness['total_closed_trades']} / {pv['phase_target']} trades · {pv['progress_pct']}%</div>
  </div>

  <div class="verdict-card">
    <div class="verdict-icon">{pv['verdict_icon']}</div>
    <div class="verdict-title">{pv['verdict_title']}</div>
    <div class="verdict-body">{pv['verdict_msg']}</div>
  </div>

  <div class="card">
    <div class="card-title">What It Has Learned</div>
    <div style="font-size:.85rem; margin-bottom:10px;">{learning['note']}</div>
    {weight_rows_html}
    <button class="trade-btn" style="margin-top:14px;" onclick="predictaScanNow()">Scan Now</button>
  </div>

  <div class="card">
    <div class="card-title">Closed P&amp;L ({n_closed} trades)</div>
    <div class="stat-grid">
      <div class="stat-box"><div class="stat-value" style="color:{_pnl_color(total_pnl)}">{_pnl_sign(total_pnl)}</div><div class="stat-label">Total P&amp;L</div></div>
      <div class="stat-box"><div class="stat-value">{f"{win_rate:.0%}" if win_rate is not None else "—"}</div><div class="stat-label">Win rate</div></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Open Positions ({len(open_t)} / {runner_status['max_positions']})</div>
    {open_rows_html}
  </div>

  <div class="card">
    <div class="card-title">Recent Closed Trades</div>
    {closed_rows_html}
  </div>

  <div class="card">
    <div class="card-title">Win Rate by Thesis Type</div>
    {thesis_rows_html}
  </div>

</main>
<footer>Auto-refreshes every 5 minutes &nbsp;·&nbsp; <a href="/trade/low-value/dashboard">Low Value dashboard</a></footer>
<script>
function predictaErrorText(data) {{
  if (typeof data.detail === 'string') return data.detail;
  if (data.detail) return JSON.stringify(data.detail);
  return JSON.stringify(data);
}}
async function predictaAction(url, label) {{
  if (!confirm('Confirm: ' + label + '?\\n\\nThis places (or closes) a real Alpaca paper order.')) return;
  const passcode = prompt('Trade passcode (leave blank if none is set):') || '';
  try {{
    const res = await fetch(url, {{ method: 'POST', headers: {{ 'X-Trade-Passcode': passcode }} }});
    const data = await res.json();
    if (!res.ok) {{ alert('Failed: ' + predictaErrorText(data)); return; }}
    alert('Done.\\n' + JSON.stringify(data, null, 2));
    location.reload();
  }} catch (e) {{ alert('Request failed: ' + e); }}
}}
async function predictaScanNow() {{
  try {{
    await fetch('/trade/automaton/scan-now', {{ method: 'POST' }});
    alert('Scan started in the background — this can take a few minutes. Reloading now; refresh again shortly to see results.');
    location.reload();
  }} catch (e) {{ alert('Scan failed: ' + e); }}
}}
</script>
</body>
</html>"""
    return html
