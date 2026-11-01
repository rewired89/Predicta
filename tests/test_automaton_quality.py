# Run from repo root: python3 tests/test_automaton_quality.py
import sys, os, tempfile, random
os.environ["PREDICTA_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "aq.db")
sys.path.insert(0, ".")
from models.trading.automaton import quality as Q

fails = []
def chk(name, got, want, tol=1e-6):
    ok = abs(got - want) <= tol * max(1, abs(want)) if isinstance(got, (int, float)) and not isinstance(got, bool) else got == want
    if not ok: fails.append(name); print(f"FAIL {name}: got {got} want {want}")

# band: hand-calculated points
chk("band mid", Q._band(5, 1, 5, 12), 0.0); chk("band good", Q._band(12, 1, 5, 12), 100.0); chk("band bad", Q._band(1, 1, 5, 12), -100.0)
chk("band halfway up (8.5 between 5 and 12)", Q._band(8.5, 1, 5, 12), 100 * 3.5 / 7)
chk("band halfway down (3 between 1 and 5)", Q._band(3, 1, 5, 12), -50.0)
chk("band reversed (lower is better) D/E 0.6 between 1.0 and 0.2", Q._band(0.6, 3.0, 1.0, 0.2), 50.0)
chk("band missing", Q._band(None, 1, 5, 12), None)

def bars(start, drift, n=300, noise=0.0, seed=1):
    random.seed(seed); p = start; out = []
    for _ in range(n):
        p *= 1 + drift + random.gauss(0, noise); out.append({"c": p})
    return out

good = {"peTTM": 10, "pfcfShareTTM": 12, "roeTTM": 25, "operatingMarginTTM": 25, "grossMarginTTM": 60,
        "revenueGrowthTTMYoy": 25, "revenueGrowth3Y": 20, "epsGrowthTTMYoy": 30,
        "totalDebt/totalEquityQuarterly": 0.2, "currentRatioQuarterly": 3.0}
spy = bars(400, 0.0003)
strong = bars(50, 0.0009)
r = Q.score_medium_term(good, strong, spy)
chk("pe 10 -> earnings yield 10%", r["groups"]["value"]["inputs"]["pe"], 10)
# value = mean(band(10%,1,5,12), band(100/12=8.33%,0,4,10)) -> (71.43 + 72.2)/2 hand-calculated below
ey = 100 * (10 - 5) / (12 - 5); fy = 100 * ((100 / 12) - 4) / (10 - 4)
chk("value group by hand", r["groups"]["value"]["score"], round((ey + fy) / 2, 1), 1e-3)
chk("quality group = +100 (all inputs at 'good' edge)", r["groups"]["quality"]["score"], 100.0)
chk("growth group = +100", r["groups"]["growth"]["score"], 100.0)
chk("safety group = +100", r["groups"]["safety"]["score"], 100.0)
chk("all five groups present", r["coverage"], 5)
chk("composite is weighted mean (hand)", r["composite"], round(sum(r["groups"][g]["score"] * Q.GROUP_WEIGHTS[g] for g in Q.GROUP_WEIGHTS) / sum(Q.GROUP_WEIGHTS.values()), 1), 1e-3)
chk("strong stock passes long gate", Q.passes_gate(r, "long")[0], True)
chk("strong stock fails short gate", Q.passes_gate(r, "short")[0], False)

bad = {"peTTM": -5, "roeTTM": -10, "operatingMarginTTM": -8, "revenueGrowthTTMYoy": -20,
       "totalDebt/totalEquityQuarterly": 4.0, "currentRatioQuarterly": 0.6}
rb = Q.score_medium_term(bad, bars(50, -0.0010), spy)
chk("loss-making pe<0 -> value -60", rb["groups"]["value"]["score"], -60.0)
chk("weak company composite negative", rb["composite"] < -25, True)
chk("weak stock passes SHORT gate", Q.passes_gate(rb, "short")[0], True)
chk("weak stock blocked from long", Q.passes_gate(rb, "long")[0], False)

# abstain rules
chk("no fundamentals -> abstain even with momentum", Q.score_medium_term({}, strong, spy)["abstain"], True)
chk("abstain never passes the gate", Q.passes_gate(Q.score_medium_term({}, strong, spy), "long")[0], False)
two = Q.score_medium_term({"roeTTM": 20, "peTTM": 12}, [], [])      # value + quality only = 2 groups
chk("only 2 groups -> abstain", two["abstain"], True)
three = Q.score_medium_term({"roeTTM": 20, "peTTM": 12, "revenueGrowthTTMYoy": 15}, [], [])
chk("3 groups incl. fundamentals -> scores", three["abstain"], False)
# missing group is excluded and weights re-scaled (not filled with 0)
w = {g: three["groups"][g] for g in ("value", "quality", "growth")}
want = sum(w[g]["score"] * Q.GROUP_WEIGHTS[g] for g in w) / sum(Q.GROUP_WEIGHTS[g] for g in w)
chk("weights re-scaled over available groups", three["composite"], round(want, 1), 1e-3)
# percent-style D/E (45 = 0.45x) is detected
chk("D/E given as percent (45) treated as 0.45x", Q.score_medium_term({"totalDebt/totalEquityQuarterly": 45, "currentRatioQuarterly": 1.5, "roeTTM": 12, "peTTM": 20}, strong, spy)["groups"]["safety"]["inputs"]["debt_to_equity"], 0.45)
# momentum by hand: 6m relative = stock 126d return - spy 126d return
s6 = strong[-1]["c"] / strong[-127]["c"] - 1; m6 = spy[-1]["c"] / spy[-127]["c"] - 1
chk("6-month relative momentum by hand", r["groups"]["momentum"]["inputs"]["rel_6m_pct"], (s6 - m6) * 100, 1e-6)
s12 = strong[-22]["c"] / strong[-253]["c"] - 1; m12 = spy[-22]["c"] / spy[-253]["c"] - 1
chk("12-minus-1-month relative momentum by hand", r["groups"]["momentum"]["inputs"]["rel_12m_skip1m_pct"], (s12 - m12) * 100, 1e-6)

# ---- end-to-end: fake scan through the real run_automaton_scan ----
import fetchers.automaton_runner as AR
from db.database import init_db; init_db()
GOOD, BAD, NODATA = "GOODCO", "BADCO", "NODATACO"
def fake_bars(sym, days=25):
    if sym == "SPY": return spy
    return (strong if sym == GOOD else bars(50, -0.001)) if days >= 300 else [{"c": 5.0, "h": 5.1, "l": 4.9, "o": 5.0, "v": 1000}] * 25
AR.get_daily_bars = fake_bars
AR.get_snapshots = lambda s: {}
AR.scan_universe_news = lambda syms, *a, **k: {}
AR.scan_universe_social = lambda syms, *a, **k: {}
AR.get_daily_universe = lambda *a, **k: [GOOD, BAD, NODATA]
AR.get_top_movers = lambda *a, **k: []; AR.get_most_active = lambda *a, **k: []
AR.compute_thesis_score = lambda *a, **k: {"composite": 55.0, "signals": {}}      # every stock "looks like a long" on the old timing signal
AR.dominant_thesis_type = lambda *a, **k: "TECHNICAL_OVERSOLD"
AR._auto_execute_entry = lambda *a, **k: {"status": "ok"}
AR.sector_etf_for_symbol = lambda s: "SPY"
import fetchers.finnhub as FH
FH.get_basic_financials = lambda sym: {GOOD: good, BAD: bad}.get(sym, {})
ids = AR.run_automaton_scan([GOOD, BAD, NODATA])
from db.database import get_db
with get_db() as c: rows = [dict(x) for x in c.execute("select symbol, side, lv_signals_json from intraday_trades where engine='automaton'")]
chk("only the high-quality stock was bought", [x["symbol"] for x in rows], [GOOD])
import json
sig = json.loads(rows[0]["lv_signals_json"]).get("medium_term_quality") if rows else None
chk("quality inputs recorded on the trade", bool(sig and sig["detail"]["groups"]), True)
evs = [e for e in AR._run_log if e.get("event") == "QUALITY_SKIP"]
chk("weak and no-data stocks logged as QUALITY_SKIP", sorted(e["sym"] for e in evs), sorted([BAD, NODATA]))
chk("no-data stock reason says abstain", any("abstain" in e["note"] for e in evs if e["sym"] == NODATA), True)
chk("funnel counts", (AR._quality_funnel["checked"], AR._quality_funnel["passed"], AR._quality_funnel["blocked"]), (3, 1, 2))
print("\nPASS: all checks agree" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
