"""
Predicta CLI — manage matches, signals, predictions, outcomes, and calibration.

Usage:
    python cli.py --help
    python cli.py init
    python cli.py add-match --sport soccer --a "Man City" --b "Arsenal" --date 2024-05-01T15:00:00
    python cli.py predict --match-id 1
    python cli.py add-signal --match-id 1 --name xg_for_avg5 --participant "Man City" --value 1.8
    python cli.py add-odds --match-id 1 --book bet365 --price-a 2.10 --price-b 3.40 --price-draw 3.20
    python cli.py record-outcome --match-id 1 --result a --score-a 2 --score-b 0
    python cli.py calibration
    python cli.py report
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from db.database import init_db, get_db
from engine import predict_match, record_outcome
from models.calibration import compute_metrics_from_db
from models.devig import devig_market
from fetchers.odds import log_manual_odds
from fetchers.signals import log_signal, get_signals_for_match
from report import generate_html_report


def cmd_init(args):
    init_db()
    print("Database initialised at predicta.db")


def cmd_add_match(args):
    init_db()
    if args.sport not in ("soccer", "table_tennis", "tennis"):
        print("ERROR: sport must be soccer, table_tennis, or tennis")
        sys.exit(1)
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO matches (sport, league, participant_a, participant_b,
                   scheduled_at, venue, neutral_site)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (args.sport, args.league, args.a, args.b,
             args.date, args.venue, int(args.neutral)),
        )
        print(f"Match created. ID: {cur.lastrowid}")


def cmd_list_matches(args):
    with get_db() as conn:
        query = "SELECT * FROM matches WHERE 1=1"
        params = []
        if args.sport:
            query += " AND sport=?"
            params.append(args.sport)
        if args.status:
            query += " AND status=?"
            params.append(args.status)
        rows = conn.execute(query, params).fetchall()
    for r in rows:
        print(f"[{r['id']}] {r['sport']} | {r['participant_a']} vs {r['participant_b']} | {r['scheduled_at']} | {r['status']}")


def cmd_add_signal(args):
    log_signal(args.match_id, args.name, args.participant, args.value, args.text, args.source)
    print(f"Signal '{args.name}' logged for match {args.match_id}.")


def cmd_list_signals(args):
    sigs = get_signals_for_match(args.match_id)
    print(json.dumps(sigs, indent=2))


def cmd_add_odds(args):
    row_id = log_manual_odds(
        args.match_id, args.book, args.market,
        args.price_a, args.price_b, args.price_draw
    )
    prices = {"a": args.price_a, "b": args.price_b}
    if args.price_draw:
        prices["draw"] = args.price_draw
    fair = devig_market(prices)
    print(f"Odds snapshot {row_id} saved. Vig-free probs: {json.dumps({k: f'{v*100:.1f}%' for k,v in fair.items()})}")


def cmd_predict(args):
    init_db()
    result = predict_match(args.match_id, args.method, args.bankroll, args.surface)
    if "error" in result:
        print(f"ERROR: {result['error']}")
        sys.exit(1)
    print(f"\n{'='*60}")
    print(f"PREDICTION — Match {result['match_id']}: {result['participant_a']} vs {result['participant_b']}")
    print(f"Sport: {result['sport']}")
    print(f"\nProbabilities:")
    print(f"  {result['participant_a']}: {result['prob_a']*100:.1f}%")
    if result['prob_draw'] is not None:
        print(f"  Draw:           {result['prob_draw']*100:.1f}%")
    print(f"  {result['participant_b']}: {result['prob_b']*100:.1f}%")
    print(f"\nExplanation:\n  {result['explanation']}")
    print(f"\nKelly (paper):\n  {result['kelly_note']}")
    if result['market']:
        fp = result['market']['fair_probs']
        print(f"\nMarket consensus ({result['market']['n_books']} snapshot(s)):")
        for k, v in fp.items():
            print(f"  {k}: {v*100:.1f}%")
    print(f"{'='*60}\n")


def cmd_record_outcome(args):
    result = record_outcome(
        args.match_id, args.result, args.score_a, args.score_b,
        not args.no_rating_update, args.importance, args.surface
    )
    if "error" in result:
        print(f"ERROR: {result['error']}")
        sys.exit(1)
    print(f"Outcome recorded: {json.dumps(result, indent=2)}")


def cmd_calibration(args):
    metrics = compute_metrics_from_db(args.method)
    if "error" in metrics:
        print(metrics["error"])
        return
    print(f"Calibration metrics (n={metrics['n']}):")
    print(f"  Brier Score: {metrics['brier_score']:.4f}")
    print(f"  Log Loss:    {metrics['log_loss']:.4f}")
    print(f"\nReliability curve:")
    for bucket in metrics.get("reliability_curve", []):
        bar_len = int(bucket['mean_actual'] * 20)
        print(
            f"  [{bucket['bin_lower']:.1f}–{bucket['bin_upper']:.1f}] "
            f"predicted={bucket['mean_predicted']:.2f}  actual={bucket['mean_actual']:.2f}  "
            f"n={bucket['count']}  {'█'*bar_len}"
        )


def cmd_report(args):
    init_db()
    html = generate_html_report()
    out = Path(args.output)
    out.write_text(html, encoding="utf-8")
    print(f"Report saved to {out.resolve()}")


def cmd_update_elo(args):
    from models.elo import EloModel
    m = EloModel()
    new_a, new_b = m.update(args.team_a, args.team_b, args.score_a, args.score_b, args.importance)
    print(f"Updated Elo: {args.team_a}={new_a:.1f}, {args.team_b}={new_b:.1f}")


def cmd_train_ml(args):
    from models.ml_layer import train
    result = train(use_gbm=args.gbm)
    print(json.dumps(result, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="predicta",
        description="Multi-sport prediction & calibration tracker [PAPER MODE]",
    )
    sub = parser.add_subparsers(dest="command")

    # init
    sub.add_parser("init", help="Initialise the SQLite database")

    # add-match
    p = sub.add_parser("add-match", help="Add a new match")
    p.add_argument("--sport", required=True, choices=["soccer", "table_tennis", "tennis"])
    p.add_argument("--a", required=True, dest="a", metavar="PARTICIPANT_A")
    p.add_argument("--b", required=True, dest="b", metavar="PARTICIPANT_B")
    p.add_argument("--date", required=True, help="ISO datetime e.g. 2024-05-01T15:00:00")
    p.add_argument("--league", default=None)
    p.add_argument("--venue", default=None)
    p.add_argument("--neutral", action="store_true")

    # list-matches
    p = sub.add_parser("list-matches", help="List matches")
    p.add_argument("--sport", default=None)
    p.add_argument("--status", default=None)

    # add-signal
    p = sub.add_parser("add-signal", help="Log a signal value for a match")
    p.add_argument("--match-id", required=True, type=int)
    p.add_argument("--name", required=True, help="Signal name e.g. xg_for_avg5")
    p.add_argument("--participant", default=None)
    p.add_argument("--value", type=float, default=None)
    p.add_argument("--text", default=None)
    p.add_argument("--source", default="manual")

    # list-signals
    p = sub.add_parser("list-signals", help="List signals for a match")
    p.add_argument("--match-id", required=True, type=int)

    # add-odds
    p = sub.add_parser("add-odds", help="Log an odds snapshot")
    p.add_argument("--match-id", required=True, type=int)
    p.add_argument("--book", required=True)
    p.add_argument("--market", default="moneyline")
    p.add_argument("--price-a", required=True, type=float)
    p.add_argument("--price-b", required=True, type=float)
    p.add_argument("--price-draw", type=float, default=None)

    # predict
    p = sub.add_parser("predict", help="Generate a prediction for a match")
    p.add_argument("--match-id", required=True, type=int)
    p.add_argument("--method", default="model_v1")
    p.add_argument("--bankroll", type=float, default=1000.0)
    p.add_argument("--surface", default="all", choices=["all", "hard", "clay", "grass"])

    # record-outcome
    p = sub.add_parser("record-outcome", help="Record the result of a match")
    p.add_argument("--match-id", required=True, type=int)
    p.add_argument("--result", required=True, choices=["a", "b", "draw"])
    p.add_argument("--score-a", type=int, default=None)
    p.add_argument("--score-b", type=int, default=None)
    p.add_argument("--no-rating-update", action="store_true")
    p.add_argument("--importance", default="default")
    p.add_argument("--surface", default="all")

    # calibration
    p = sub.add_parser("calibration", help="Show calibration metrics")
    p.add_argument("--method", default=None)

    # report
    p = sub.add_parser("report", help="Generate HTML report")
    p.add_argument("--output", default="report.html")

    # update-elo (utility)
    p = sub.add_parser("update-elo", help="Manually update Elo ratings after a match")
    p.add_argument("--team-a", required=True)
    p.add_argument("--team-b", required=True)
    p.add_argument("--score-a", required=True, type=int)
    p.add_argument("--score-b", required=True, type=int)
    p.add_argument("--importance", default="default")

    # train-ml
    p = sub.add_parser("train-ml", help="Train the ML calibration layer (requires 50+ outcomes)")
    p.add_argument("--gbm", action="store_true", help="Use gradient boosting instead of logistic regression")

    return parser


COMMANDS = {
    "init": cmd_init,
    "add-match": cmd_add_match,
    "list-matches": cmd_list_matches,
    "add-signal": cmd_add_signal,
    "list-signals": cmd_list_signals,
    "add-odds": cmd_add_odds,
    "predict": cmd_predict,
    "record-outcome": cmd_record_outcome,
    "calibration": cmd_calibration,
    "report": cmd_report,
    "update-elo": cmd_update_elo,
    "train-ml": cmd_train_ml,
}


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)
    fn = COMMANDS.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()
        sys.exit(1)
