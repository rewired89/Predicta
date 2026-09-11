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

import html
import json

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
  .predicta-modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.6); display: flex; align-items: center; justify-content: center; z-index: 1000; padding: 16px; }
  .predicta-modal { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; max-width: 380px; width: 100%; }
  .predicta-modal p { font-size: .85rem; margin-bottom: 12px; white-space: pre-wrap; color: var(--text); }
  .predicta-modal input { width: 100%; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; color: var(--text); padding: 8px; font-size: .85rem; margin-bottom: 12px; }
  .predicta-modal-actions { display: flex; gap: 8px; justify-content: flex-end; }
  .predicta-toast { position: fixed; bottom: 20px; right: 20px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; font-size: .82rem; max-width: 360px; z-index: 1000; white-space: pre-wrap; color: var(--text); }
"""

_JS = """
function predictaErrorText(data) {
  if (typeof data.detail === 'string') return data.detail;
  if (data.detail) return JSON.stringify(data.detail);
  return JSON.stringify(data);
}
// Fixed 2026-09-11 (real user report — Buy/Sell "only works once" on the
// other dashboards, same root cause here): every action used chained native
// browser popups (prompt() then confirm(), sometimes both). Browsers
// silently suppress ALL further popups on a page after enough have fired in
// a short session (an anti-spam feature, no visible warning once tripped).
// predictaModal/predictaToast are plain in-page DOM elements, immune to
// that throttling, replacing every confirm/prompt/alert in this file.
function predictaModal(message, opts) {
  opts = opts || {};
  return new Promise(function(resolve) {
    const backdrop = document.createElement('div');
    backdrop.className = 'predicta-modal-backdrop';
    const box = document.createElement('div');
    box.className = 'predicta-modal';
    const p = document.createElement('p');
    p.textContent = message;
    box.appendChild(p);
    let input = null;
    if (opts.needsInput) {
      input = document.createElement('input');
      input.type = opts.inputType || 'text';
      input.placeholder = opts.inputPlaceholder || '';
      if (opts.inputDefault) input.value = opts.inputDefault;
      box.appendChild(input);
    }
    const actions = document.createElement('div');
    actions.className = 'predicta-modal-actions';
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'trade-btn sell-btn';
    cancelBtn.textContent = 'Cancel';
    const okBtn = document.createElement('button');
    okBtn.className = 'trade-btn buy-btn';
    okBtn.textContent = opts.okLabel || 'Confirm';
    actions.appendChild(cancelBtn);
    actions.appendChild(okBtn);
    box.appendChild(actions);
    backdrop.appendChild(box);
    document.body.appendChild(backdrop);
    if (input) input.focus();
    function cleanup(result) { document.body.removeChild(backdrop); resolve(result); }
    cancelBtn.onclick = function() { cleanup(null); };
    okBtn.onclick = function() { cleanup({ value: input ? input.value : true }); };
    backdrop.onclick = function(e) { if (e.target === backdrop) cleanup(null); };
    if (input) input.onkeydown = function(e) { if (e.key === 'Enter') { e.preventDefault(); okBtn.onclick(); } };
  });
}
function predictaToast(message) {
  const t = document.createElement('div');
  t.className = 'predicta-toast';
  t.textContent = message;
  document.body.appendChild(t);
  setTimeout(function() { t.remove(); }, 6000);
}
async function predictaCopyPost(url, body, confirmLabel) {
  if (confirmLabel) {
    const result = await predictaModal(confirmLabel + '\\n\\nThis is a PAPER position — no real money, no Alpaca order.', { okLabel: 'Confirm' });
    if (!result) return;
  }
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) { predictaToast('Failed: ' + predictaErrorText(data)); return; }
    predictaToast('Done: ' + (data.status || JSON.stringify(data)));
    setTimeout(function() { location.reload(); }, 1500);
  } catch (e) { predictaToast('Request failed: ' + e); }
}
function copyThisTradeFromButton(btn) {
  const d = JSON.parse(btn.dataset.copy);
  copyThisTrade(d.ticker, d.trade_type, d.insider_name, d.title, d.company, d.filing_date);
}
async function copyThisTrade(symbol, tradeType, insiderName, insiderTitle, company, filingDate) {
  const result = await predictaModal('How many shares of ' + symbol + ' do you want to copy this ' + tradeType + ' with?', { needsInput: true, inputType: 'number', inputDefault: '10', okLabel: 'Copy this trade' });
  if (!result) return; // user hit Cancel — no action, no error needed
  const qty = result.value;
  if (!qty || isNaN(qty) || Number(qty) <= 0) { predictaToast('Enter a whole number of shares greater than 0.'); return; }
  predictaCopyPost('/trade/copy/execute', {
    symbol: symbol, trade_type: tradeType, qty: Number(qty),
    insider_name: insiderName, insider_title: insiderTitle,
    company: company, filing_date: filingDate,
  }, 'Copy ' + tradeType + ' ' + symbol + ' x' + qty + '?');
}
async function buyMore(tradeId, symbol) {
  const result = await predictaModal('How many MORE shares of ' + symbol + ' do you want to add?', { needsInput: true, inputType: 'number', inputDefault: '10', okLabel: 'Add' });
  if (!result) return;
  const qty = result.value;
  if (!qty || isNaN(qty) || Number(qty) <= 0) { predictaToast('Enter a whole number of shares greater than 0.'); return; }
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
    if (!res.ok) { predictaToast('Scan failed: ' + predictaErrorText(data)); return; }
    predictaToast(data.count + ' insider filing(s) found in the last 7 days. Reloading.');
    setTimeout(function() { location.reload(); }, 1500);
  } catch (e) {
    predictaToast('Scan failed: ' + e);
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
        # Fixed 2026-09-10 (real user report: "I clicked and nothing
        # happened") — this used to build the onclick handler by
        # hand-interpolating each scraped field into a single-quoted JS
        # string, escaping only apostrophes. openinsider.com's raw scraped
        # text (insider_name/title/company) can carry embedded newlines or
        # backslashes from multi-line table cells, and a literal newline
        # inside a plain '...' JS string is a SyntaxError — silently
        # breaking that one row's onclick with no visible error, so the
        # button just does nothing when clicked. Now passes the row as a
        # properly JSON-encoded, HTML-escaped data attribute instead, which
        # is safe for any scraped text content.
        row_data = html.escape(json.dumps({
            "ticker": f["ticker"], "trade_type": f["trade_type"],
            "insider_name": f["insider_name"], "title": f["title"],
            "company": f["company"], "filing_date": f["filing_date"],
        }), quote=True)
        rows_html += f"""<div class="trade-row">
              <div class="trade-icon">{"🟢" if is_buy else "🔴"}</div>
              <div class="trade-info">
                <span class="trade-sym">{f['ticker']} · {f['company']}</span>
                <span class="trade-detail">{f['insider_name']} — {f['title']} {badge}</span>
                <span class="trade-detail">Filed price: {filed} · Live price now: {live} · Qty on filing: {f.get('qty') or '—'}</span>
                <span class="trade-time">Filed {f['filing_date']} (trade date {f.get('trade_date') or '—'})</span>
                <div style="margin-top:8px;">
                  <button class="trade-btn {'buy-btn' if is_buy else 'sell-btn'}" data-copy="{row_data}" onclick="copyThisTradeFromButton(this)">Copy this trade</button>
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
