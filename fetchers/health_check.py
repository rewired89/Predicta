"""
Health check (2026-11-04, user request after the first run on her own PC: "the scans
don't work, it didn't scan anything" with no way to see why).

One page, plain English, that tests the things that make scans silently do nothing:
the clock (weekend?), the keys in .env, whether Alpaca and Finnhub actually answer,
whether the runners are alive, and whether any data has been recorded. Each check is
independent and never raises. Secrets are never shown, only "set" / "missing".
"""
from __future__ import annotations
import html
import os
from datetime import datetime, timedelta

OK, WARN, BAD = "ok", "warn", "bad"


def _c(status: str, title: str, detail: str) -> dict:
    return {"status": status, "title": title, "detail": detail}


def _safe(fn, title: str) -> list[dict]:
    try:
        out = fn()
        return out if isinstance(out, list) else [out]
    except Exception as exc:                                   # a check must never break the page
        return [_c(WARN, title, f"Could not run this check: {exc}")]


def _clock() -> list[dict]:
    from fetchers.high_value_runner import _et_now
    now = _et_now()
    day = now.strftime("%A")
    stamp = now.strftime("%A %-I:%M %p")
    if now.weekday() >= 5:
        days = (7 - now.weekday())
        nxt = (now + timedelta(days=days)).strftime("%A")
        return [_c(WARN, "Markets are closed today",
                   f"It is {stamp} in New York. Predicta only scans Monday to Friday, so nothing scanning right "
                   f"now is NORMAL. Low Value scans {nxt} at 8:00 AM New York time. To test right now, use the "
                   f"'Run Low Value scan now' button below.")]
    return [_c(OK, "Weekday", f"It is {stamp} in New York. Low Value scans at 8:00 AM New York time "
                              f"(and catches up later the same day if the computer was off). "
                              f"High Value runs after the close, 4:05 PM.")]


def _env() -> list[dict]:
    items = []
    env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    items.append(_c(OK if os.path.exists(env_file) else BAD, "Settings file (.env)",
                    f"Found at {env_file}" if os.path.exists(env_file)
                    else f"Not found. It must be here: {env_file}"))
    required = [("ALPACA_API_KEY", "Alpaca key", True), ("ALPACA_SECRET_KEY", "Alpaca secret", True),
                ("FINNHUB_API_KEY", "Finnhub key (Low Value finds no stocks without it)", True),
                ("ANTHROPIC_API_KEY", "Anthropic key (photo reading and AI features only)", False),
                ("SEC_EDGAR_USER_AGENT", "SEC contact line (insider data)", False),
                ("FRED_API_KEY", "FRED key (optional)", False)]
    for name, label, must in required:
        val = os.environ.get(name, "").strip()
        if val and val.lower() not in ("...", "your_key_here", "changeme"):
            items.append(_c(OK, label, "set"))
        else:
            if name == "FRED_API_KEY":
                items.append(_c(OK, label, "not set, and that is fine. Predicta runs without it."))
            else:
                items.append(_c(BAD if must else WARN, label,
                                f"MISSING. Add a line {name}=your_value to the .env file, save, and restart Predicta."))
    return items


def _alpaca() -> list[dict]:
    from fetchers import alpaca
    items = []
    try:
        acct = alpaca.get_account()
    except Exception as exc:
        return [_c(BAD, "Alpaca account", f"Could not even try: {exc}")]
    if isinstance(acct, dict) and acct.get("error"):
        code = acct.get("status_code")
        if code in (401, 403):
            msg = ("Alpaca REJECTED your keys. Make sure they are the PAPER trading keys (Alpaca dashboard, "
                   "Paper Trading, API Keys), with no spaces or quotes in the .env file.")
        else:
            msg = f"Alpaca did not answer properly: {acct['error']}"
        items.append(_c(BAD, "Alpaca account", msg))
    else:
        items.append(_c(OK, "Alpaca account", f"Connected (paper account, status {acct.get('status', 'ok')})."))
    bars = alpaca.get_daily_bars("SPY", days=10)
    if bars:
        items.append(_c(OK, "Alpaca price data", f"Got {len(bars)} recent daily prices for SPY (latest {str(bars[-1].get('t', ''))[:10]})."))
    else:
        items.append(_c(BAD, "Alpaca price data",
                        "Alpaca returned NO prices. Either the keys are wrong, or this network blocks "
                        "data.alpaca.markets. Without prices nothing can scan."))
    return items


def _finnhub() -> list[dict]:
    from fetchers.finnhub import _finnhub_get
    if not os.environ.get("FINNHUB_API_KEY", "").strip():
        return [_c(BAD, "Finnhub", "No key, so Low Value cannot look up company size and will find no stocks.")]
    data = _finnhub_get("stock/profile2", {"symbol": "AAPL"})
    if data and data.get("marketCapitalization"):
        return [_c(OK, "Finnhub", "Connected (looked up Apple's market size).")]
    return [_c(BAD, "Finnhub", "The key did not work. Check it for typos, or you hit the free limit (60 per minute). "
                               "Try again in a minute.")]


def _runners() -> list[dict]:
    items = []
    try:
        from fetchers.low_value_runner import get_runner_status as lv
        s = lv()
        if not s.get("active"):
            items.append(_c(BAD, "Low Value scheduler", "NOT running. Restart Predicta and watch the terminal for errors."))
        else:
            done = s.get("scheduled_scan_completed_today")
            err = s.get("last_scan_error") or s.get("last_universe_build_error")
            items.append(_c(OK, "Low Value scheduler", "Running."))
            items.append(_c(OK if done else WARN, "Low Value scan today",
                            "Done today." if done else "Not done yet today. That is normal on weekends, before 8:00 AM "
                                                       "New York time, or if you only just started Predicta."))
            size = s.get("universe_size", 0)
            items.append(_c(OK if size else WARN, "Low Value stock list",
                            f"{size} stocks in today's list." if size else
                            "Empty so far. It is built during the first scan of the day."))
            if err:
                items.append(_c(BAD, "Low Value last error", str(err)))
    except Exception as exc:
        items.append(_c(WARN, "Low Value scheduler", f"Could not read status: {exc}"))
    for label, modpath in (("Automaton scheduler", "fetchers.automaton_runner"), ("High Value scheduler", "fetchers.high_value_runner")):
        try:
            mod = __import__(modpath, fromlist=["get_runner_status"])
            s = mod.get_runner_status()
            items.append(_c(OK if s.get("active") else WARN, label, "Running." if s.get("active") else "Not running."))
        except Exception as exc:
            items.append(_c(WARN, label, f"Could not read status: {exc}"))
    return items


def _data() -> list[dict]:
    from db.database import get_db
    items = []
    with get_db() as conn:
        bars = conn.execute("SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(bar_date) FROM market_daily_bars").fetchone()
        trades = conn.execute("SELECT engine, COUNT(*) FROM intraday_trades GROUP BY engine").fetchall()
        scans = conn.execute("SELECT engine, COUNT(*), MAX(scan_time) FROM trade_scan_log GROUP BY engine").fetchall()
    items.append(_c(OK if bars[0] else WARN, "Saved price history",
                    f"{bars[0]:,} daily prices for {bars[1]} stocks (latest {bars[2]})." if bars[0] else
                    "Nothing saved yet. Press 'Backfill 3 years of prices' below, once. The Horizon Forecast page "
                    "needs this before it can say anything."))
    items.append(_c(OK if trades else WARN, "Trades logged so far",
                    ", ".join(f"{e}: {n}" for e, n in trades) if trades else "None yet. Normal on a fresh install."))
    items.append(_c(OK if scans else WARN, "Scans recorded",
                    ", ".join(f"{e}: {n} (last {str(t)[:16]} UTC)" for e, n, t in scans) if scans else
                    "No scans recorded yet on this computer."))
    return items


def run_health_check() -> dict:
    checks: list[dict] = []
    for fn, title in ((_clock, "Clock"), (_env, "Settings"), (_alpaca, "Alpaca"), (_finnhub, "Finnhub"),
                      (_runners, "Schedulers"), (_data, "Data")):
        checks += _safe(fn, title)
    bad = [c for c in checks if c["status"] == BAD]
    warn = [c for c in checks if c["status"] == WARN]
    if bad:
        headline = f"{len(bad)} problem(s) to fix first: " + "; ".join(c["title"] for c in bad)
    elif warn:
        headline = "No broken parts. Some items are waiting on time or data (see the yellow lines)."
    else:
        headline = "Everything checks out."
    return {"headline": headline, "checks": checks, "generated_at": datetime.now().isoformat(timespec="seconds")}


_ICON = {OK: "✅", WARN: "🟡", BAD: "❌"}


def render_health_page(result: dict) -> str:
    rows = "".join(
        f'<li class="{c["status"]}"><span class="ic">{_ICON[c["status"]]}</span>'
        f'<div><b>{html.escape(c["title"])}</b><br><span class="d">{html.escape(c["detail"])}</span></div></li>'
        for c in result["checks"])
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Predicta Health Check</title>
<style>
:root{{--bg:#fff;--fg:#1c2330;--muted:#5b6678;--line:#e3e7ee;--card:#f7f9fc;--ok:#1a7f4b;--warn:#a06a00;--bad:#c0262d}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1420;--fg:#e8edf6;--muted:#9aa6bb;--line:#26304a;--card:#161d2e;--ok:#4cc38a;--warn:#e5b04a;--bad:#ff6b73}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:24px 16px}}
h1{{margin:0 0 4px}} .head{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:14px 0}}
ul{{list-style:none;padding:0;margin:0}} li{{display:flex;gap:10px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;margin:8px 0;background:var(--card)}}
li.bad{{border-color:var(--bad)}} li.warn{{border-color:var(--warn)}} .d{{color:var(--muted);font-size:.92rem}}
button{{padding:9px 12px;margin:4px 6px 4px 0;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;font-size:.95rem}}
#out{{white-space:pre-wrap;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;margin-top:10px;font-size:.88rem;color:var(--muted)}}
a{{color:inherit}}
</style></head><body><main>
<h1>Predicta health check</h1>
<div class="head"><b>{html.escape(result["headline"])}</b><br><span class="d">Checked {html.escape(result["generated_at"])}. Reload this page to check again.</span></div>
<ul>{rows}</ul>
<h2>Actions</h2>
<button onclick="act('/trade/low-value/scan-now','Low Value scan started. Reload this page in a few minutes.')">Run Low Value scan now</button>
<button onclick="act('/trade/market-data/record','Recording today\\'s prices...')">Record today's prices</button>
<button onclick="act('/trade/horizon/backfill?years=3','Backfilling 3 years of prices (runs in the background, a few minutes)...')">Backfill 3 years of prices</button>
<div id="out">Buttons start work in the background. Nothing here places a trade.</div>
<p class="d"><a href="/trading">Back to Predicta</a></p>
<script>
async function act(url,msg){{const o=document.getElementById('out');o.textContent=msg;
try{{const r=await fetch(url,{{method:'POST'}});const t=await r.text();o.textContent=msg+'\\n\\nAnswer: '+t.slice(0,600);}}
catch(e){{o.textContent='Could not reach Predicta: '+e;}}}}
</script></main></body></html>"""
