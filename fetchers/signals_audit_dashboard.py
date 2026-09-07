"""
Signals Audit dashboard (added 2026-09-07, direct user request).

User's own framing: "we need a database to see only signals that passed
the scan and were put as possible tradings ... that way we can also see
what Automaton is doing ... audit it and make improvements." Automaton in
particular places real (paper) orders with no human click at all, so
there was previously no way to browse *why* it acted on a given symbol
without reading raw DB rows by hand.

This does NOT add a new table — every row a scan logs as a candidate
(is_hypothetical=1, or later promoted to real via promote_trade_to_real)
already lives in intraday_trades, one row per engine (high_value /
low_value / automaton), engine column added in the v7 Automaton migration.
This just makes that existing data browsable: filter by engine and by
open/closed status, see the entry score and (for Low Value/Automaton) the
thesis type + which of the 8 signals fired, and expand a row to see every
per-signal score column that was non-null at entry time — the same
"which signals actually worked" audit trail /trade/low-value/calibration
already does in aggregate, but per-trade and across all three engines in
one place.

Copy Trades rows (is_copy_trade=1) are excluded — those come from copying
a disclosed insider filing, not from a scan signal, so they're not
"signals that passed the scan" in the sense asked for here; they already
have their own page at /trade/portfolio.
"""
from __future__ import annotations
from datetime import datetime, timezone

from db.database import get_db

# Per-signal / regime-tag columns worth showing in a row's expandable detail
# view when non-null. Deliberately excludes bookkeeping columns (id, notes,
# alpaca_order_id, etc.) that don't help a human decide "was this a good
# signal" — this is an audit view, not a full row dump (raw JSON dump is
# already available at /trade/paper-data for anyone who wants every column).
_SIGNAL_DETAIL_COLUMNS: list[str] = [
    "composite_raw", "vwap_score", "or_score", "rsi_score", "relvol_score",
    "gap_score", "trend_score", "bollinger_score", "volsurge_score",
    "ngram_signal", "ngram_confidence",
    "lv_thesis_type", "lv_news_flags", "lv_news_sentiment", "lv_headline_count",
    "spy_gap_pct", "xlk_change_pct", "market_regime",
    "spy_realized_vol_pct", "market_vol_regime", "macro_event_today",
]

_ENGINE_LABELS = {"high_value": "High Value", "low_value": "Low Value", "automaton": "Automaton"}


def _fetch_rows(engine: str, status: str, limit: int) -> list[dict]:
    where = ["is_copy_trade = 0"]
    params: list = []
    if engine != "all":
        where.append("engine = ?")
        params.append(engine)
    if status == "open":
        where.append("exit_time IS NULL")
    elif status == "closed":
        where.append("exit_time IS NOT NULL")
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM intraday_trades
            WHERE {' AND '.join(where)}
            ORDER BY entry_time DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def _pnl_color(val) -> str:
    if val is None:
        return "#64748b"
    return "#22c55e" if val > 0 else ("#ef4444" if val < 0 else "#64748b")


def _pnl_text(t: dict) -> str:
    if not t.get("exit_time"):
        return "—"
    pnl = t.get("pnl_dollars")
    if pnl is None:
        return "—"
    sign = "+" if pnl >= 0 else "-"
    return f"{sign}${abs(pnl):,.2f}"


def _status_badge(t: dict) -> str:
    if not t.get("exit_time"):
        return '<span style="color:#38bdf8">OPEN</span>'
    return '<span style="color:#64748b">CLOSED</span>'


def _real_badge(t: dict) -> str:
    if t.get("is_hypothetical"):
        return '<span style="color:#64748b;font-size:.72rem">signal only</span>'
    return '<span style="color:#ef4444;font-weight:700;font-size:.72rem">REAL</span>'


def _detail_html(t: dict) -> str:
    present = {k: t[k] for k in _SIGNAL_DETAIL_COLUMNS if t.get(k) is not None}
    if not present:
        return '<span class="muted-note">No per-signal detail logged for this row.</span>'
    parts = [f"<div><b>{k}</b>: {v}</div>" for k, v in present.items()]
    return "".join(parts)


def render_signals_audit(engine: str = "all", status: str = "all", limit: int = 150) -> str:
    if engine not in ("all", "high_value", "low_value", "automaton"):
        engine = "all"
    if status not in ("all", "open", "closed"):
        status = "all"
    limit = max(1, min(limit, 500))

    rows = _fetch_rows(engine, status, limit)

    total = len(rows)
    real_count = sum(1 for r in rows if not r.get("is_hypothetical"))
    closed = [r for r in rows if r.get("exit_time")]
    wins = sum(1 for r in closed if (r.get("pnl_dollars") or 0) > 0)
    win_rate_str = f"{wins / len(closed):.0%}" if closed else "—"

    def _tab(label: str, param: str, value: str, current: str) -> str:
        active = "background:#38bdf8;color:#04121f;font-weight:700;" if value == current else "color:#94a3b8;"
        href = f"/trade/signals-audit?engine={engine if param!='engine' else value}&status={status if param!='status' else value}&limit={limit}"
        return f'<a href="{href}" style="padding:6px 12px;border-radius:8px;text-decoration:none;font-size:.8rem;{active}">{label}</a>'

    engine_tabs = "".join([
        _tab("All Engines", "engine", "all", engine),
        _tab("High Value", "engine", "high_value", engine),
        _tab("Low Value", "engine", "low_value", engine),
        _tab("Automaton", "engine", "automaton", engine),
    ])
    status_tabs = "".join([
        _tab("All", "status", "all", status),
        _tab("Open", "status", "open", status),
        _tab("Closed", "status", "closed", status),
    ])

    row_html_parts = []
    for t in rows:
        eng_lbl = _ENGINE_LABELS.get(t.get("engine"), t.get("engine") or "?")
        thesis = t.get("lv_thesis_type")
        thesis_html = f'<div style="font-size:.72rem;color:#a78bfa">{thesis}</div>' if thesis else ""
        row_html_parts.append(f"""
        <details class="sig-row">
          <summary>
            <span class="sig-time">{(t.get("entry_time") or "")[:16].replace("T", " ")}</span>
            <span class="sig-engine">{eng_lbl}</span>
            <span class="sig-sym">{t.get("symbol")}</span>
            <span class="sig-side">{(t.get("side") or "").upper()}</span>
            <span class="sig-score">score {t.get("entry_score")}</span>
            {_status_badge(t)}
            {_real_badge(t)}
            <span class="sig-pnl" style="color:{_pnl_color(t.get('pnl_dollars'))}">{_pnl_text(t)}</span>
          </summary>
          <div class="sig-detail">
            {thesis_html}
            <div>Entry ${t.get('entry_price')} → Target ${t.get('target1_price') or t.get('target_price')} · Stop ${t.get('stop_price')}</div>
            {f"<div>Exit ${t.get('exit_price')} · reason: {t.get('exit_reason')}</div>" if t.get("exit_time") else ""}
            <div style="margin-top:8px;font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;color:#64748b">Signals at entry</div>
            {_detail_html(t)}
          </div>
        </details>""")
    rows_html = "".join(row_html_parts) if row_html_parts else '<p class="muted-note">No candidates logged for this filter yet.</p>'

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Predicta — Signals Audit</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{ --bg: #0b1120; --surface: #131d30; --border: #1e2d47; --text: #e2e8f0; --muted: #64748b; }}
  body {{ background: var(--bg); color: var(--text); font-family: system-ui, -apple-system, sans-serif; padding-bottom: 60px; }}
  header {{ background: var(--surface); border-bottom: 1px solid var(--border); padding: 16px 20px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
  .logo {{ font-size: 1.2rem; font-weight: 700; color: #38bdf8; }}
  .header-meta {{ margin-left: auto; font-size: .78rem; color: var(--muted); }}
  main {{ max-width: 920px; margin: 0 auto; padding: 20px 16px; display: flex; flex-direction: column; gap: 16px; }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }}
  .card-title {{ font-size: .7rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); margin-bottom: 14px; }}
  .stat-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }}
  .stat-box {{ background: var(--bg); border: 1px solid var(--border); border-radius: 10px; padding: 12px; text-align: center; }}
  .stat-value {{ font-size: 1.5rem; font-weight: 800; }}
  .stat-label {{ font-size: .68rem; color: var(--muted); text-transform: uppercase; }}
  .tab-row {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  .nav-row {{ display: flex; gap: 10px; align-items: center; font-size: .8rem; color: var(--muted); flex-wrap: wrap; }}
  .nav-row a {{ color: var(--muted); text-decoration: none; }}
  .nav-row a:hover {{ color: #38bdf8; text-decoration: underline; }}
  .muted-note {{ font-size: .82rem; color: var(--muted); font-style: italic; }}
  details.sig-row {{ border-bottom: 1px solid var(--border); padding: 8px 0; }}
  details.sig-row:last-child {{ border-bottom: none; }}
  details.sig-row summary {{ cursor: pointer; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; font-size: .82rem; list-style: none; }}
  details.sig-row summary::-webkit-details-marker {{ display: none; }}
  .sig-time {{ color: var(--muted); font-size: .74rem; width: 110px; flex-shrink: 0; }}
  .sig-engine {{ font-size: .7rem; padding: 2px 6px; border-radius: 5px; background: var(--bg); border: 1px solid var(--border); color: #94a3b8; }}
  .sig-sym {{ font-weight: 700; }}
  .sig-side {{ color: var(--muted); font-size: .74rem; }}
  .sig-score {{ color: var(--muted); font-size: .76rem; }}
  .sig-pnl {{ margin-left: auto; font-weight: 700; font-size: .8rem; }}
  .sig-detail {{ margin-top: 8px; padding: 10px 12px; background: var(--bg); border-radius: 8px; font-size: .78rem; line-height: 1.7; }}
  footer {{ text-align: center; font-size: .75rem; color: var(--muted); padding: 20px; }}
  a {{ color: #38bdf8; }}
</style>
</head>
<body>
<header>
  <div class="logo">Predicta · Signals Audit</div>
  <div class="header-meta">{now_str}</div>
</header>
<main>

  <div class="nav-row">
    <a href="/">← Home</a><span>·</span>
    <a href="/trading">Stock Market</a><span>·</span>
    <a href="/trade/dashboard">High Value</a><span>·</span>
    <a href="/trade/low-value/dashboard">Low Value</a><span>·</span>
    <a href="/trade/automaton/dashboard">Automaton</a>
  </div>

  <div class="card">
    <div class="card-title">Every signal that passed a scan and was logged as a possible trade</div>
    <div style="font-size:.82rem;color:var(--muted);margin-bottom:14px;">
      One row per candidate an engine's scan flagged — not just the ones a human (or, for Automaton, the engine itself) actually acted on. Expand a row to see the per-signal scores it was logged with, so a losing pattern can actually be traced back to which signal(s) drove it.
    </div>
    <div class="stat-grid">
      <div class="stat-box"><div class="stat-value">{total}</div><div class="stat-label">Shown</div></div>
      <div class="stat-box"><div class="stat-value">{real_count}</div><div class="stat-label">Became real orders</div></div>
      <div class="stat-box"><div class="stat-value">{len(closed)}</div><div class="stat-label">Closed</div></div>
      <div class="stat-box"><div class="stat-value">{win_rate_str}</div><div class="stat-label">Win rate (closed)</div></div>
    </div>
  </div>

  <div class="card">
    <div style="display:flex;flex-direction:column;gap:10px;">
      <div class="tab-row">{engine_tabs}</div>
      <div class="tab-row">{status_tabs}</div>
    </div>
  </div>

  <div class="card">
    {rows_html}
  </div>

</main>
<footer>Showing up to {limit} rows &nbsp;·&nbsp; <a href="/trade/paper-data?days=60&include_open=true">Raw JSON export</a></footer>
</body>
</html>"""
