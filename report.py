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
<title>Predicta — Report</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #0f172a; color: #e2e8f0; }}
  h1 {{ color: #38bdf8; }}
  .metrics {{ display: flex; gap: 2rem; margin: 1rem 0 2rem; }}
  .metric {{ background: #1e293b; padding: 1rem 1.5rem; border-radius: 8px; }}
  .metric .value {{ font-size: 2rem; font-weight: bold; color: #38bdf8; }}
  .metric .label {{ font-size: 0.8rem; color: #94a3b8; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th {{ background: #1e293b; padding: 0.5rem 0.75rem; text-align: left; color: #94a3b8; }}
  td {{ padding: 0.5rem 0.75rem; border-bottom: 1px solid #1e293b; vertical-align: top; }}
  tr:hover td {{ background: #1e293b44; }}
  .badge {{ background: #22c55e; color: #fff; padding: 2px 8px; border-radius: 12px; font-size: 0.75rem; }}
  .explanation {{ max-width: 400px; font-size: 0.78rem; color: #94a3b8; }}
  .paper-mode {{ background: #f59e0b22; border: 1px solid #f59e0b; padding: 0.5rem 1rem;
                  border-radius: 6px; color: #fbbf24; margin-bottom: 1.5rem; display: inline-block; }}
</style>
</head>
<body>
<h1>Predicta — Prediction Tracker</h1>
<div class="paper-mode">PAPER MODE — No real wagers tracked or placed.</div>
<div class="metrics">
  <div class="metric"><div class="value">{n}</div><div class="label">Resolved Predictions</div></div>
  <div class="metric"><div class="value">{brier_str}</div><div class="label">Brier Score (lower = better)</div></div>
  <div class="metric"><div class="value">{ll_str}</div><div class="label">Log Loss</div></div>
</div>
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
</body>
</html>"""
