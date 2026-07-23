"""
HTML report generator — accuracy dashboard + prediction history.
"""
from __future__ import annotations
from db.database import get_db
from models.calibration import compute_metrics_from_db


def _pct(v) -> str:
    if v is None:
        return "–"
    return f"{v*100:.1f}%"


def _sport_rows(by_sport: dict) -> str:
    html = ""
    sport_icons = {"tennis": "🎾", "table_tennis": "🏓", "soccer": "⚽", "baseball": "⚾",
                   "esports": "🎮", "rugby": "🏉", "ufc": "🥊"}
    for sport, s in sorted(by_sport.items()):
        icon = sport_icons.get(sport, "🎯")
        acc = _pct(s.get("accuracy"))
        bet_acc = _pct(s.get("bet_accuracy"))
        roi = s.get("roi")
        roi_str = f"{roi*100:+.1f}%" if roi is not None else "–"
        roi_color = "#2bee7a" if (roi or 0) >= 0 else "#ff5f5f"
        n = s.get("n", 0)
        bets = s.get("bets_placed", 0)
        html += f"""
        <tr>
          <td>{icon} {sport.replace("_", " ").title()}</td>
          <td style="font-family:var(--mono);font-weight:700">{acc}</td>
          <td style="font-family:var(--mono)">{bet_acc}</td>
          <td style="font-family:var(--mono);color:{roi_color}">{roi_str}</td>
          <td style="color:var(--muted)">{s.get("correct",0)}/{n} correct</td>
          <td style="color:var(--muted)">{s.get("bets_correct",0)}/{bets} bets won</td>
        </tr>"""
    return html


def _conf_rows(by_confidence: dict) -> str:
    html = ""
    conf_color = {"high": "#2bee7a", "medium": "#e8c36b", "low": "#ff5f5f", "unknown": "#84938a"}
    for conf, s in sorted(by_confidence.items()):
        color = conf_color.get(conf, "#84938a")
        acc = _pct(s.get("accuracy"))
        bet_acc = _pct(s.get("bet_accuracy"))
        n = s.get("n", 0)
        html += f"""
        <tr>
          <td><span style="color:{color};font-weight:700">{conf.upper()}</span></td>
          <td style="font-family:var(--mono)">{acc}</td>
          <td style="font-family:var(--mono)">{bet_acc}</td>
          <td style="color:var(--muted)">{n} predictions</td>
        </tr>"""
    return html


def _benchmark_rows(benchmarks: list) -> str:
    html = ""
    for b in benchmarks:
        acc = b.get("accuracy", 0)
        is_us = b["model"].startswith("Predicta")
        style = "color:var(--green);font-weight:700" if is_us else ""
        bar_w = int(acc * 200)  # scale: 50% = 100px, 100% = 200px
        bar_fill = "#2bee7a" if is_us else "#84938a"
        vs = b.get("vs_random", "")
        html += f"""
        <tr style="{style}">
          <td>{b["model"]}</td>
          <td style="font-family:var(--mono)">{acc*100:.1f}%</td>
          <td>
            <div style="background:rgba(255,255,255,.06);border-radius:4px;width:200px;height:8px;overflow:hidden">
              <div style="background:{bar_fill};width:{bar_w}px;height:8px;border-radius:4px"></div>
            </div>
          </td>
          <td style="font-size:.75rem;color:var(--muted)">{vs or b.get("note","")}</td>
        </tr>"""
    return html


def _match_rows(matches) -> str:
    html = ""
    for m in matches:
        result = m["result"]
        prob_a = m["prob_a"]
        prob_b = m["prob_b"]

        # Did model predict correctly?
        correct_badge = ""
        winner_name = ""
        if result and prob_a is not None:
            predicted = "a" if prob_a >= (prob_b or 1 - prob_a) else "b"
            correct = (predicted == result)
            winner_name = m["participant_a"] if result == "a" else (
                m["participant_b"] if result == "b" else "Draw")
            color = "#2bee7a" if correct else "#ff5f5f"
            label = "CORRECT" if correct else "WRONG"
            correct_badge = f'<span style="background:rgba(255,255,255,.06);color:{color};border:1px solid {color}44;padding:2px 8px;border-radius:99px;font-size:.68rem;font-weight:700;font-family:var(--mono)">{label}</span>'

        score_str = f'{m["score_a"]}-{m["score_b"]}' if m["score_a"] is not None else ""
        prob_str = ""
        if prob_a is not None:
            prob_str = f'{m["participant_a"]}: {prob_a*100:.1f}%'
            if prob_b is not None:
                prob_str += f' / {m["participant_b"]}: {prob_b*100:.1f}%'

        html += f"""
        <tr>
          <td style="color:var(--muted);font-family:var(--mono);font-size:.75rem">{m["id"]}</td>
          <td>{m["sport"].replace("_"," ").title()}</td>
          <td>{m["participant_a"]} <span style="color:var(--muted)">vs</span> {m["participant_b"]}</td>
          <td style="color:var(--muted);font-size:.78rem">{(m["scheduled_at"] or "")[:10]}</td>
          <td>{winner_name} {score_str}</td>
          <td style="font-size:.78rem;color:var(--muted)">{prob_str}</td>
          <td>{correct_badge}</td>
        </tr>"""
    return html


def generate_html_report() -> str:
    with get_db() as conn:
        matches = conn.execute(
            """SELECT m.*, o.result, o.score_a, o.score_b,
                      p.prob_a, p.prob_b, p.prob_draw, p.explanation, p.method
               FROM matches m
               LEFT JOIN outcomes o ON o.match_id = m.id
               LEFT JOIN predictions p ON p.match_id = m.id
               ORDER BY m.scheduled_at DESC"""
        ).fetchall()

        pending_count = conn.execute("""
            SELECT COUNT(*) FROM matches m
            JOIN predictions p ON p.match_id = m.id
            LEFT JOIN outcomes o ON o.match_id = m.id
            WHERE o.match_id IS NULL
        """).fetchone()[0]

    metrics = compute_metrics_from_db()

    has_data = metrics.get("overall", {}).get("n", 0) > 0
    overall  = metrics.get("overall", {})
    n        = overall.get("n", 0)
    acc      = _pct(overall.get("accuracy"))
    bet_acc  = _pct(overall.get("bet_accuracy"))
    roi      = overall.get("roi")
    roi_str  = f"{roi*100:+.1f}%" if roi is not None else "–"
    roi_color = "#2bee7a" if (roi or 0) >= 0 else "#ff5f5f"
    bets     = overall.get("bets_placed", 0)
    brier    = metrics.get("brier_score")
    brier_str = f"{brier:.4f}" if brier is not None else "–"

    no_data_msg = ""
    if not has_data:
        no_data_msg = """
        <div style="background:rgba(232,195,107,.08);border:1px solid rgba(232,195,107,.25);border-radius:12px;padding:20px 24px;margin-bottom:28px">
          <div style="color:#e8c36b;font-weight:700;margin-bottom:6px">No resolved predictions yet</div>
          <div style="color:var(--muted);font-size:.85rem">
            Record match results via <code style="background:rgba(255,255,255,.08);padding:2px 6px;border-radius:4px">POST /matches/{id}/outcome</code>
            or use <code style="background:rgba(255,255,255,.08);padding:2px 6px;border-radius:4px">POST /outcomes/batch</code> for multiple at once.
            Once results are entered, accuracy stats will appear here.
          </div>
        </div>"""

    sport_rows = _sport_rows(metrics.get("by_sport", {})) if has_data else "<tr><td colspan='6' style='color:var(--muted);padding:20px'>No data yet</td></tr>"
    conf_rows  = _conf_rows(metrics.get("by_confidence", {})) if has_data else "<tr><td colspan='4' style='color:var(--muted);padding:20px'>No data yet</td></tr>"
    bench_rows = _benchmark_rows(metrics.get("benchmarks", [])) if has_data else ""
    match_rows = _match_rows(matches)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Predicta — Accuracy Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --green: #2bee7a; --gold: #e8c36b; --red: #ff5f5f;
    --bg: #050706; --text: #e9f1ec; --muted: #84938a;
    --panel: rgba(255,255,255,.025); --panel-br: rgba(255,255,255,.08);
    --sans: "Space Grotesk", system-ui, sans-serif;
    --mono: "Space Mono", ui-monospace, monospace;
  }}
  html {{ font-size: 15px; }}
  body {{ background: var(--bg); color: var(--text); font-family: var(--sans); min-height: 100vh; }}
  .bg-fx {{ position: fixed; inset: 0; pointer-events: none; z-index: 0; overflow: hidden; }}
  .blob {{ position: absolute; border-radius: 50%; filter: blur(80px); opacity: .13; animation: drift 20s ease-in-out infinite alternate; }}
  .b1 {{ width: 480px; height: 480px; background: #2bee7a; top: -160px; left: -140px; }}
  .b2 {{ width: 360px; height: 360px; background: #1a9e53; top: 50%; right: -100px; animation-delay: -7s; }}
  .b3 {{ width: 280px; height: 280px; background: #0d5c31; bottom: -50px; left: 40%; animation-delay: -14s; }}
  @keyframes drift {{ to {{ transform: translate(40px,30px) scale(1.08); }} }}
  .bg-grid {{
    position: fixed; inset: 0; pointer-events: none; z-index: 0;
    background-image: linear-gradient(rgba(43,238,122,.04) 1px,transparent 1px),linear-gradient(90deg,rgba(43,238,122,.04) 1px,transparent 1px);
    background-size: 48px 48px;
  }}
  nav {{
    position: relative; z-index: 10;
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 2rem; height: 60px;
    background: rgba(5,7,6,.85); backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--panel-br);
  }}
  .nav-left {{ display: flex; align-items: center; gap: 14px; }}
  .logo {{ font-family: var(--mono); font-size: 1.15rem; font-weight: 700; color: var(--green); letter-spacing: -.5px; text-decoration: none; }}
  .pm-pill {{ font-size: .65rem; font-weight: 700; letter-spacing: .8px; text-transform: uppercase; background: rgba(43,238,122,.12); color: var(--green); border: 1px solid rgba(43,238,122,.25); border-radius: 99px; padding: 3px 10px; }}
  .nav-links {{ display: flex; gap: 24px; }}
  .nav-links a {{ color: var(--muted); text-decoration: none; font-size: .88rem; font-weight: 500; }}
  .nav-links a:hover {{ color: var(--text); }}
  .nav-links a.active {{ color: var(--green); }}
  .content {{ position: relative; z-index: 1; max-width: 1160px; margin: 0 auto; padding: 40px 24px 80px; }}
  h1 {{ font-size: 1.85rem; font-weight: 700; color: var(--green); font-family: var(--mono); margin-bottom: 6px; }}
  .subtitle {{ color: var(--muted); font-size: .88rem; margin-bottom: 28px; }}
  h2 {{ font-size: 1rem; font-weight: 700; color: var(--text); margin: 32px 0 14px; font-family: var(--mono); text-transform: uppercase; letter-spacing: .5px; }}
  .paper-banner {{ font-size: .75rem; font-weight: 600; background: rgba(232,195,107,.08); color: var(--gold); border: 1px solid rgba(232,195,107,.25); border-radius: 8px; padding: 7px 16px; display: inline-block; margin-bottom: 24px; }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; margin-bottom: 32px; }}
  .kpi {{ background: var(--panel); border: 1px solid var(--panel-br); border-radius: 12px; padding: 18px 20px; }}
  .kpi .val {{ font-size: 2rem; font-weight: 700; color: var(--green); font-family: var(--mono); }}
  .kpi .lbl {{ font-size: .68rem; color: var(--muted); margin-top: 6px; text-transform: uppercase; letter-spacing: .5px; font-weight: 600; }}
  .kpi .sub {{ font-size: .75rem; color: var(--muted); margin-top: 4px; }}
  .pending-pill {{ display: inline-flex; align-items: center; gap: 6px; background: rgba(255,95,95,.08); color: #ff5f5f; border: 1px solid rgba(255,95,95,.25); border-radius: 99px; padding: 4px 12px; font-size: .75rem; font-weight: 600; margin-bottom: 24px; cursor: pointer; text-decoration: none; }}
  .section {{ background: var(--panel); border: 1px solid var(--panel-br); border-radius: 14px; overflow: hidden; margin-bottom: 24px; }}
  .section-header {{ padding: 14px 20px; border-bottom: 1px solid var(--panel-br); font-size: .8rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .83rem; }}
  th {{ padding: 10px 16px; text-align: left; color: var(--muted); font-size: .68rem; text-transform: uppercase; letter-spacing: .6px; font-weight: 700; border-bottom: 1px solid var(--panel-br); white-space: nowrap; }}
  td {{ padding: 10px 16px; border-bottom: 1px solid rgba(255,255,255,.04); vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: rgba(255,255,255,.02); }}
  code {{ background: rgba(255,255,255,.08); padding: 2px 6px; border-radius: 4px; font-family: var(--mono); font-size: .82em; }}
</style>
</head>
<body>
<div class="bg-fx"><div class="blob b1"></div><div class="blob b2"></div><div class="blob b3"></div></div>
<div class="bg-grid"></div>
<nav>
  <div class="nav-left">
    <a class="logo" href="/">Predicta</a>
    <span class="pm-pill">PAPER MODE</span>
  </div>
  <div class="nav-links">
    <a href="/">Sports</a>
    <a href="/trading">Trading</a>
    <a href="/report" class="active">Report</a>
    <a href="/docs">API</a>
  </div>
</nav>
<div class="content">
  <h1>Accuracy Report</h1>
  <div class="subtitle">Track model win rate, bet accuracy, ROI, and calibration over time.</div>
  <div class="paper-banner">PAPER MODE — No real wagers tracked or placed.</div>

  {no_data_msg}

  {"" if not pending_count else f'<a class="pending-pill" href="/pending-outcomes">⚠ {pending_count} predictions awaiting results — click to record outcomes</a>'}

  <div class="kpi-grid">
    <div class="kpi">
      <div class="val">{acc}</div>
      <div class="lbl">Prediction Accuracy</div>
      <div class="sub">Predicted winner == actual winner</div>
    </div>
    <div class="kpi">
      <div class="val">{bet_acc}</div>
      <div class="lbl">Bet Accuracy</div>
      <div class="sub">Non-PASS recommendations only</div>
    </div>
    <div class="kpi">
      <div class="val" style="color:{roi_color}">{roi_str}</div>
      <div class="lbl">ROI</div>
      <div class="sub">{bets} bets placed (flat -110 odds)</div>
    </div>
    <div class="kpi">
      <div class="val">{n}</div>
      <div class="lbl">Resolved Predictions</div>
      <div class="sub">{pending_count} pending results</div>
    </div>
    <div class="kpi">
      <div class="val">{brier_str}</div>
      <div class="lbl">Brier Score</div>
      <div class="sub">Random=0.25 · Perfect=0.0</div>
    </div>
  </div>

  <div class="section">
    <div class="section-header">Accuracy by Sport</div>
    <table>
      <thead><tr><th>Sport</th><th>Prediction Accuracy</th><th>Bet Accuracy</th><th>ROI</th><th>Overall</th><th>Bets</th></tr></thead>
      <tbody>{sport_rows}</tbody>
    </table>
  </div>

  <div class="section">
    <div class="section-header">Accuracy by Data Confidence Level</div>
    <table>
      <thead><tr><th>Confidence</th><th>Prediction Accuracy</th><th>Bet Accuracy</th><th>Count</th></tr></thead>
      <tbody>{conf_rows}</tbody>
    </table>
  </div>

  {"" if not has_data else f"""
  <div class="section">
    <div class="section-header">Benchmark Comparison</div>
    <table>
      <thead><tr><th>Model</th><th>Accuracy</th><th>Bar</th><th>Note</th></tr></thead>
      <tbody>{bench_rows}</tbody>
    </table>
  </div>"""}

  <h2>Prediction History</h2>
  <div class="section">
    <table>
      <thead><tr><th>#</th><th>Sport</th><th>Match</th><th>Date</th><th>Result</th><th>Model Probs</th><th>Verdict</th></tr></thead>
      <tbody>{match_rows}</tbody>
    </table>
  </div>

  <div style="margin-top:32px;padding:20px;background:var(--panel);border:1px solid var(--panel-br);border-radius:12px;font-size:.82rem;color:var(--muted)">
    <strong style="color:var(--text)">How to record results:</strong><br><br>
    Single result: <code>POST /matches/{{id}}/outcome</code> → <code>{{"result": "a"}}</code> (a=first player won, b=second, draw)<br>
    Batch results: <code>POST /outcomes/batch</code> → <code>[{{"match_id": 1, "result": "b"}}, {{"match_id": 2, "result": "a"}}]</code><br>
    Pending list:  <code>GET /pending-outcomes</code>
  </div>
</div>
</body>
</html>"""
