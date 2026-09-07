"""
Copy Trades — dashboard renderer (2026-09-03, user-requested).

Two pages, same style/CSS convention as fetchers/low_value_dashboard.py:
  render_copy_trades_page() — browse recent insider filings, pick one to copy
  render_portfolio_page()   — your copied paper positions, Buy More / Sell

Both are standalone HTML strings; app.py's GET /trade/copy-trades and
GET /trade/portfolio just call these and wrap the result in an HTMLResponse.

Every action here is a PAPER position only (no Alpaca order, no passcode
gate — see fetchers/copy_trading.py's module docstring for why that's the
right gate to skip: nothing real-money happens until a human separately
promotes a specific position through the existing execute_high_value_trade
flow, which already requires the trade passcode).
"""
from __future__ import annotations

from fetchers.copy_trading import list_copy_trade_candidates, get_portfolio_with_unrealized_pnl
from fetchers.trading_logger import get_closed_copy_trades

_STYLE = """
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0b1120; --surface: #131d30; --border: #1e2d47;
    --blue: #38bdf8; --green: #22c55e; --amber: #f59e0b;
    --red: #ef4444; --purple: #a78bfa; --text: #e2e8f0; --muted: #64748b;
  }
  html { font-size: 15px; }
  body { background: var(--bg); color: var(--text); font-family: 'Inter', system-ui, sans-serif; min-height: 100vh; padding: 0 0 60px; }
  header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 14px 28px; display: flex; align-items: center; gap: 12px; }
  header .logo { font-size: 1.3rem; font-weight: 700; color: var(--blue); letter-spacing: -.5px; }
  header nav { margin-left: auto; display: flex; gap: 16px; }
  header nav a { color: var(--muted); text-decoration: none; font-size: .85rem; }
  header nav a:hover { color: var(--text); }
  main { max-width: 900px; margin: 0 auto; padding: 32px 24px; }
  h1 { font-size: 1.5rem; margin-bottom: 6px; }
  .sub { color: var(--muted); font-size: .88rem; margin-bottom: 24px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px; }
  .card-title { font-weight: 700; font-size: .95rem; margin-bottom: 10px; }
  .muted-note { color: var(--muted); font-size: .85rem; }
  .warn-banner { font-size: .8rem; background: #1a1000; border: 1px solid var(--amber); color: var(--amber); padding: 10px 14px; border-radius: 8px; margin-bottom: 20px; }
  .trade-row { display: flex; gap: 12px; padding: 14px 0; border-bottom: 1px solid var(--border); }
  .trade-row:last-child { border-bottom: none; }
  .trade-icon { font-size: 1.3rem; line-height: 1; }
  .trade-info { flex: 1; display: flex; flex-direction: column; gap: 4px; }
  .trade-sym { font-weight: 700; font-size: .95rem; }
  .trade-detail { font-size: .8rem; color: var(--muted); }
  .trade-time { font-size: .72rem; color: var(--muted); }
  .badge { font-size: .68rem; font-weight: 700; padding: 2px 8px; border-radius: 99px; letter-spacing: .4px; }
  .badge-buy { background: #062a17; color: var(--green); }
  .badge-sell { background: #2a0606; color: var(--red); }
  .trade-btn { border: none; border-radius: 6px; padding: 7px 14px; font-size: .78rem; font-weight: 700; cursor: pointer; }
  .buy-btn { background: var(--green); color: #04150a; }
  .sell-btn { background: var(--red); color: #1a0303; }
  .qty-input { width: 70px; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; color: var(--text); padding: 6px 8px; font-size: .8rem; margin-right: 6px; }
  footer { text-align: center; color: var(--muted); font-size: .78rem; margin-top: 20px; }
"""

_JS = """
function predictaErrorText(data) {
  if (typeof data.detail === 'string') return data.detail;
  if (data.detail) return JSON.stringify(data.detail);
  return JSON.stringify(data);
}
async function predictaCopyPost(url, body, confirmLabel) {
  if (confirmLabel && !confirm(confirmLabel + '\\n\\nThis is a PAPER position — no real money, no Alpaca order.')) return;
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) { alert('Failed: ' + predictaErrorText(data)); return; }
    alert('Done.\\n' + JSON.stringify(data, null, 2));
    location.reload();
  } catch (e) { alert('Request failed: ' + e); }
}
function copyThisTrade(symbol, tradeType, insiderName, insiderTitle, company, filingDate) {
  const qty = prompt('How many shares of ' + symbol + ' do you want to copy this ' + tradeType + ' with?', '10');
  if (!qty || isNaN(qty) || Number(qty) <= 0) return;
  predictaCopyPost('/trade/copy/execute', {
    symbol: symbol, trade_type: tradeType, qty: Number(qty),
    insider_name: insiderName, insider_title: insiderTitle,
    company: company, filing_date: filingDate,
  }, 'Copy ' + tradeType + ' ' + symbol + ' x' + qty + '?');
}
function buyMore(tradeId, symbol) {
  const qty = prompt('How many MORE shares of ' + symbol + ' do you want to add?', '10');
  if (!qty || isNaN(qty) || Number(qty) <= 0) return;
  predictaCopyPost('/trade/portfolio/buy-more/' + tradeId, { qty: Number(qty) }, 'Add ' + qty + ' shares of ' + symbol + '?');
}
function sellPosition(tradeId, symbol) {
  predictaCopyPost('/trade/portfolio/sell/' + tradeId, {}, 'Sell your entire ' + symbol + ' copy-trade position now?');
}
async function scanForNewFilings() {
  const btn = document.getElementById('scan-filings-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Scanning...'; }
  try {
    const res = await fetch('/trade/copy-trades/scan-now', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) { alert('Scan failed: ' + predictaErrorText(data)); return; }
    alert(data.count + ' insider filing(s) found in the last 7 days. Reloading.');
    location.reload();
  } catch (e) {
    alert('Scan failed: ' + e);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Scan Now'; }
  }
}
"""

_NAV = """
<header>
  <div class="logo">Predicta</div>
  <nav>
    <a href="/trading/high-value">High Value</a>
    <a href="/trade/dashboard">Dashboard</a>
    <a href="/trade/copy-trades">Copy Trades</a>
    <a href="/trade/portfolio">Portfolio</a>
  </nav>
</header>
"""


def _pnl_color(val) -> str:
    if val is None:
        return "#64748b"
    return "#22c55e" if val > 0 else ("#ef4444" if val < 0 else "#64748b")


def _pnl_sign(val) -> str:
    if val is None:
        return "—"
    return f"+${val:,.2f}" if val >= 0 else f"-${abs(val):,.2f}"


def _money(val) -> str:
    return f"${val:,.2f}" if val is not None else "—"


def render_copy_trades_page() -> str:
    candidates = list_copy_trade_candidates(limit=40, days=7)

    rows_html = ""
    for f in candidates:
        is_buy = f["trade_type"] == "Purchase"
        badge = '<span class="badge badge-buy">BUY (Insider Purchase)</span>' if is_buy else '<span class="badge badge-sell">SELL (Insider Sale)</span>'
        live = _money(f.get("live_price"))
        filed = _money(f.get("filed_price"))
        args = ", ".join(
            "'" + str(x).replace("'", "\\'") + "'"
            for x in (f["ticker"], f["trade_type"], f["insider_name"], f["title"], f["company"], f["filing_date"])
        )
        rows_html += f"""<div class="trade-row">
              <div class="trade-icon">{"🟢" if is_buy else "🔴"}</div>
              <div class="trade-info">
                <span class="trade-sym">{f['ticker']} · {f['company']}</span>
                <span class="trade-detail">{f['insider_name']} — {f['title']} {badge}</span>
                <span class="trade-detail">Filed price: {filed} · Live price now: {live} · Qty on filing: {f.get('qty') or '—'}</span>
                <span class="trade-time">Filed {f['filing_date']} (trade date {f.get('trade_date') or '—'})</span>
                <div style="margin-top:8px;">
                  <button class="trade-btn {'buy-btn' if is_buy else 'sell-btn'}" onclick="copyThisTrade({args})">Copy this trade</button>
                </div>
              </div>
            </div>"""

    if not rows_html:
        rows_html = '<div class="muted-note">No recent insider filings loaded — either nothing new filed in the last 7 days, or the live fetch from openinsider.com failed. Refresh in a bit, or check /copy-trades-diag.</div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="600">
<title>Predicta — Copy Trades</title>
<style>{_STYLE}</style>
</head>
<body>
{_NAV}
<main>
  <h1>Copy Trades</h1>
  <div class="sub">Real SEC Form 4 filings from corporate insiders (CEOs, execs, board members) — public record, disclosed within 2 business days by law. Pick one and copy it as a paper position.</div>

  <div class="warn-banner">
    This is corporate-insider data, not congressional/politician trades — real politician trade data needs a paid API and lags up to 45 days by law, so it wouldn't actually be "today's trades." "Copy this trade" only logs a paper position here — no real money, no Alpaca order. Selling to copy an insider's Sale means going short — that's a real, different risk than the insider going out of a position they already hold.
  </div>

  <div class="card">
    <div class="card-title" style="display:flex;align-items:center;justify-content:space-between;">
      <span>Recent Insider Filings ({len(candidates)})</span>
      <button id="scan-filings-btn" class="trade-btn buy-btn" onclick="scanForNewFilings()">Scan Now</button>
    </div>
    {rows_html}
  </div>
</main>
<footer>Auto-refreshes every 10 minutes &nbsp;·&nbsp; <a href="/trade/portfolio" style="color:var(--blue);">View your Portfolio</a></footer>
<script>{_JS}</script>
</body>
</html>"""
    return html


def render_portfolio_page() -> str:
    open_positions = get_portfolio_with_unrealized_pnl()
    closed = get_closed_copy_trades(days=60)

    open_html = ""
    for p in open_positions:
        side_lbl = "BUY" if p["side"] == "long" else "SHORT"
        open_html += f"""<div class="trade-row">
              <div class="trade-icon">{"🟢" if p['side']=='long' else "🔴"}</div>
              <div class="trade-info">
                <span class="trade-sym">{side_lbl} {p['symbol']} · {p.get('qty')} shares</span>
                <span class="trade-detail">Copied from {p.get('copy_source_name') or 'an insider filing'} ({p.get('copy_source_title') or '—'}) at {p.get('copy_source_company') or p['symbol']}, filed {p.get('copy_filing_date') or '—'}</span>
                <span class="trade-detail">Avg entry: {_money(p.get('entry_price'))} · Live: {_money(p.get('live_price'))} · Unrealized: <span style="color:{_pnl_color(p.get('unrealized_pnl'))}">{_pnl_sign(p.get('unrealized_pnl'))} ({p.get('unrealized_pnl_pct') if p.get('unrealized_pnl_pct') is not None else '—'}%)</span></span>
                <span class="trade-time">opened {p['entry_time'][:16]} UTC</span>
                <div style="margin-top:8px;">
                  <button class="trade-btn buy-btn" onclick="buyMore({p['id']}, '{p['symbol']}')">Buy More</button>
                  <button class="trade-btn sell-btn" onclick="sellPosition({p['id']}, '{p['symbol']}')">Sell Now</button>
                </div>
              </div>
            </div>"""
    if not open_html:
        open_html = '<div class="muted-note">No open copy-trade positions yet — go to <a href="/trade/copy-trades" style="color:var(--blue);">Copy Trades</a> and pick one to copy.</div>'

    closed_html = ""
    for t in closed[:15]:
        side_lbl = "BUY" if t.get("side") == "long" else "SHORT"
        closed_html += f"""<div class="trade-row">
              <div class="trade-info">
                <span class="trade-sym">{side_lbl} {t['symbol']} · {t.get('qty')} shares</span>
                <span class="trade-detail">{_money(t.get('entry_price'))} → {_money(t.get('exit_price'))} · <span style="color:{_pnl_color(t.get('pnl_dollars'))}">{_pnl_sign(t.get('pnl_dollars'))} ({t.get('pnl_pct') or 0:+.1f}%)</span></span>
                <span class="trade-time">closed {t['exit_time'][:16]} UTC</span>
              </div>
            </div>"""
    if not closed_html:
        closed_html = '<div class="muted-note">No closed copy trades yet.</div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Predicta — Portfolio</title>
<style>{_STYLE}</style>
</head>
<body>
{_NAV}
<main>
  <h1>Portfolio</h1>
  <div class="sub">Your copied insider trades — all paper positions, no real money involved.</div>

  <div class="card">
    <div class="card-title">Open Positions ({len(open_positions)})</div>
    {open_html}
  </div>

  <div class="card">
    <div class="card-title">Closed (last 60 days)</div>
    {closed_html}
  </div>
</main>
<footer>Auto-refreshes every 5 minutes &nbsp;·&nbsp; <a href="/trade/copy-trades" style="color:var(--blue);">Find more trades to copy</a></footer>
<script>{_JS}</script>
</body>
</html>"""
    return html
