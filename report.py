"""
HTML report generator — single-page summary of matches, predictions, and calibration.
"""
from __future__ import annotations
from db.database import get_db
from models.calibration import compute_metrics_from_db


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

    metrics = compute_metrics_from_db()
    brier = metrics.get("brier_score", "n/a")
    ll = metrics.get("log_loss", "n/a")
    n = metrics.get("n", 0)

    rows_html = ""
    for m in matches:
        result_badge = ""
        if m["result"]:
            winner = m["participant_a"] if m["result"] == "a" else (
                m["participant_b"] if m["result"] == "b" else "Draw"
            )
            result_badge = f'<span class="badge">{winner}</span>'
        score_str = f'{m["score_a"]}-{m["score_b"]}' if m["score_a"] is not None else "-"
        prob_str = ""
        if m["prob_a"] is not None:
            prob_str = f'{m["participant_a"]}: {m["prob_a"]*100:.1f}%'
            if m["prob_draw"] is not None:
                prob_str += f' / Draw: {m["prob_draw"]*100:.1f}%'
            if m["prob_b"] is not None:
                prob_str += f' / {m["participant_b"]}: {m["prob_b"]*100:.1f}%'
        rows_html += f"""
        <tr>
            <td>{m["id"]}</td>
            <td>{m["sport"]}</td>
            <td>{m["league"] or "-"}</td>
            <td>{m["participant_a"]} vs {m["participant_b"]}</td>
            <td>{m["scheduled_at"][:10]}</td>
            <td>{score_str} {result_badge}</td>
            <td>{prob_str}</td>
            <td class="explanation">{m["explanation"] or "-"}</td>
        </tr>"""

    brier_str = f"{brier:.4f}" if isinstance(brier, float) else str(brier)
    ll_str = f"{ll:.4f}" if isinstance(ll, float) else str(ll)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Predicta — Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --green:    #2bee7a;
    --gold:     #e8c36b;
    --red:      #ff5f5f;
    --bg:       #050706;
    --text:     #e9f1ec;
    --muted:    #84938a;
    --panel:    rgba(255,255,255,.025);
    --panel-br: rgba(255,255,255,.08);
    --sans:     "Space Grotesk", system-ui, sans-serif;
    --mono:     "Space Mono", ui-monospace, monospace;
  }}
  html {{ font-size: 15px; }}
  body {{ background: var(--bg); color: var(--text); font-family: var(--sans); min-height: 100vh; overflow-x: hidden; }}

  .bg-fx {{ position: fixed; inset: 0; pointer-events: none; z-index: 0; overflow: hidden; }}
  .blob {{
    position: absolute; border-radius: 50%;
    filter: blur(80px); opacity: .14;
    animation: drift 18s ease-in-out infinite alternate;
  }}
  .b1 {{ width: 480px; height: 480px; background: #2bee7a; top: -160px; left: -140px; animation-duration: 22s; }}
  .b2 {{ width: 360px; height: 360px; background: #1a9e53; top: 50%; right: -100px; animation-duration: 17s; animation-delay: -6s; }}
  .b3 {{ width: 280px; height: 280px; background: #0d5c31; bottom: -50px; left: 40%; animation-duration: 25s; animation-delay: -12s; }}
  @keyframes drift {{ from {{ transform: translate(0,0) scale(1); }} to {{ transform: translate(40px,30px) scale(1.08); }} }}
  .bg-grid {{
    position: fixed; inset: 0; pointer-events: none; z-index: 0;
    background-image: linear-gradient(rgba(43,238,122,.04) 1px, transparent 1px),
                      linear-gradient(90deg, rgba(43,238,122,.04) 1px, transparent 1px);
    background-size: 48px 48px;
  }}

  nav {{
    position: relative; z-index: 10;
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 2rem; height: 60px;
    background: rgba(5,7,6,.85); backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--panel-br);
  }}
  .nav-left  {{ display: flex; align-items: center; gap: 14px; }}
  .logo      {{ font-family: var(--mono); font-size: 1.15rem; font-weight: 700; color: var(--green); letter-spacing: -.5px; text-decoration: none; }}
  .pm-pill   {{
    font-size: .65rem; font-weight: 700; letter-spacing: .8px; text-transform: uppercase;
    background: rgba(43,238,122,.12); color: var(--green);
    border: 1px solid rgba(43,238,122,.25); border-radius: 99px; padding: 3px 10px;
  }}
  .nav-links {{ display: flex; gap: 24px; }}
  .nav-links a {{ color: var(--muted); text-decoration: none; font-size: .88rem; font-weight: 500; transition: color .2s; }}
  .nav-links a:hover {{ color: var(--text); }}
  .nav-links a.active {{ color: var(--green); }}

  .content {{ position: relative; z-index: 1; max-width: 1100px; margin: 0 auto; padding: 40px 24px 80px; }}

  h1 {{ font-size: 1.9rem; font-weight: 700; color: var(--green); margin-bottom: 24px; font-family: var(--mono); }}

  .paper-banner {{
    font-size: .75rem; font-weight: 600; letter-spacing: .4px;
    background: rgba(232,195,107,.08); color: var(--gold);
    border: 1px solid rgba(232,195,107,.25); border-radius: 8px;
    padding: 7px 16px; display: inline-block; margin-bottom: 24px;
  }}

  .metrics {{ display: flex; gap: 16px; margin-bottom: 32px; flex-wrap: wrap; }}
  .metric {{
    background: var(--panel); border: 1px solid var(--panel-br);
    border-radius: 12px; padding: 18px 24px; min-width: 160px;
  }}
  .metric .value {{ font-size: 2rem; font-weight: 700; color: var(--green); font-family: var(--mono); }}
  .metric .label {{ font-size: .72rem; color: var(--muted); margin-top: 6px; text-transform: uppercase; letter-spacing: .5px; font-weight: 600; }}

  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .83rem; }}
  th {{
    background: var(--panel); padding: 10px 14px; text-align: left;
    color: var(--muted); font-size: .68rem; text-transform: uppercase;
    letter-spacing: .6px; font-weight: 700; border-bottom: 1px solid var(--panel-br);
    white-space: nowrap;
  }}
  td {{ padding: 10px 14px; border-bottom: 1px solid rgba(255,255,255,.04); vertical-align: top; }}
  tr:hover td {{ background: rgba(255,255,255,.025); }}
  .badge {{
    background: rgba(43,238,122,.18); color: var(--green);
    border: 1px solid rgba(43,238,122,.25);
    padding: 2px 9px; border-radius: 99px; font-size: .72rem; font-weight: 700;
    font-family: var(--mono); white-space: nowrap;
  }}
  .explanation {{ max-width: 360px; font-size: .76rem; color: var(--muted); line-height: 1.5; }}
</style>
</head>
<body>

<div class="bg-fx">
  <div class="blob b1"></div>
  <div class="blob b2"></div>
  <div class="blob b3"></div>
</div>
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
  <h1>Prediction Tracker</h1>
  <div class="paper-banner">⚠ PAPER MODE — No real wagers tracked or placed.</div>

  <div class="metrics">
    <div class="metric">
      <div class="value">{n}</div>
      <div class="label">Resolved Predictions</div>
    </div>
    <div class="metric">
      <div class="value">{brier_str}</div>
      <div class="label">Brier Score (lower = better)</div>
    </div>
    <div class="metric">
      <div class="value">{ll_str}</div>
      <div class="label">Log Loss</div>
    </div>
  </div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>ID</th><th>Sport</th><th>League</th><th>Match</th>
          <th>Date</th><th>Result</th><th>Model Probs</th><th>Explanation</th>
        </tr>
      </thead>
      <tbody>
{rows_html}
      </tbody>
    </table>
  </div>
</div>

</body>
</html>"""
