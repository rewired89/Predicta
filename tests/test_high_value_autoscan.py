# Run from repo root: python3 tests/test_high_value_autoscan.py
# Drives High Value's real _runner_loop with a fake clock and fake scan/close functions.
import sys, os, tempfile
os.environ["PREDICTA_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "hv.db")
sys.path.insert(0, ".")
from datetime import datetime
from zoneinfo import ZoneInfo
from db.database import init_db, get_db
init_db()
import fetchers.high_value_runner as HV

ET = ZoneInfo("America/New_York")
clock = {"now": None}
calls = []
HV._et_now = lambda: clock["now"]
HV._et_minutes = lambda: clock["now"].hour * 60 + clock["now"].minute
HV.check_review_queue_timeouts = lambda: None
HV._is_market_open = lambda: clock["now"].weekday() < 5 and 570 <= HV._et_minutes() < 960
HV.run_open_scan = lambda symbols=None, min_score=0: (calls.append("scan") or [])
HV.check_and_close_positions = lambda force_close=False: (calls.append("close_eod" if force_close else "check") or {})
HV.check_real_position_exits = lambda force_close=False: (calls.append("real_eod" if force_close else "real_check") or {"checked": 0})
HV._check_intraday_regime_escalation = lambda: None
HV.HV_SCAN_MODE = "morning"      # sections 1-7 test the older morning schedule; section 8 tests after-close

def run_ticks(times, fresh=True):
    """Run the real loop, one tick per fake time."""
    it = iter(times)
    def fake_sleep(_):
        try: clock["now"] = next(it)
        except StopIteration: HV._runner_active = False
    HV.time.sleep = fake_sleep
    clock["now"] = next(it); HV._runner_active = True
    HV._runner_loop(["AAA"], 40)

mon = lambda h, m: datetime(2026, 11, 2, h, m, tzinfo=ET)       # a Monday
fails = []
def chk(name, got, want):
    if got != want: fails.append(name); print(f"FAIL {name}: got {got} want {want}")

# 1. normal day: scans once at the open, not again on later ticks
calls.clear(); run_ticks([mon(9, 36), mon(9, 40), mon(9, 50)])
chk("scans exactly once", calls.count("scan"), 1)
# log_scan_event stamps real wall-clock time; move the row onto the fake clock's day
from datetime import timezone
with get_db() as c: c.execute("UPDATE trade_scan_log SET scan_time = ?", (mon(9, 36).astimezone(timezone.utc).isoformat(),))
# 2. restart mid-day AFTER a scan already logged today -> does not scan again
calls.clear(); run_ticks([mon(11, 0), mon(11, 5)])
chk("restart does not rescan (scan was logged in DB)", calls.count("scan"), 0)
# 3. fresh DB day, app starts late (11:00) -> catch-up scan happens
with get_db() as c: c.execute("DELETE FROM trade_scan_log")
calls.clear(); run_ticks([mon(11, 0), mon(11, 1)])
chk("late start catches up and scans", calls.count("scan"), 1)
# 4. after 14:00 with no scan -> no new scan (too late for same-day ideas)
with get_db() as c: c.execute("DELETE FROM trade_scan_log")
calls.clear(); run_ticks([mon(14, 30), mon(14, 31)])
chk("no scan after 14:00", calls.count("scan"), 0)
# 5. end-of-day force close fires once at 15:50, even if no scan happened today
calls.clear(); run_ticks([mon(15, 51), mon(15, 52)])
chk("EOD force-close runs (hypothetical)", calls.count("close_eod"), 1)
chk("EOD force-close runs (real positions)", calls.count("real_eod"), 1)
# 6. weekends and closed hours do nothing
sat = datetime(2026, 11, 7, 10, 0, tzinfo=ET)
calls.clear(); run_ticks([sat, sat])
chk("weekend does nothing", calls, [])
calls.clear(); run_ticks([mon(8, 0), mon(8, 1)])
chk("before the open does nothing", calls, [])
# 7. scheduled scans are logged (so the monthly report counts them)
with get_db() as c: c.execute("DELETE FROM trade_scan_log")
run_ticks([mon(10, 0), mon(10, 1)])
with get_db() as c: rows = [dict(r) for r in c.execute("select engine, triggered_by from trade_scan_log")]
chk("scheduled scan logged to trade_scan_log", rows, [{"engine": "high_value", "triggered_by": "scheduled"}])

# ---------------- 8. AFTER-CLOSE mode (the default): ONE run a day, after 16:05 ET ----------------
HV.HV_SCAN_MODE = "after_close"
bar_today = {"has": True}
HV.get_daily_bars = lambda sym, days=5: ([{"t": mon(0, 0).strftime("%Y-%m-%dT05:00:00Z")}] if bar_today["has"] else [{"t": "2026-10-30T04:00:00Z"}])
def clean(): 
    with get_db() as c: c.execute("DELETE FROM trade_scan_log")
clean(); calls.clear(); run_ticks([mon(10, 0), mon(12, 0), mon(15, 55), mon(16, 0)])
chk("after-close: nothing during the session or before 16:05", calls, [])
clean(); calls.clear(); run_ticks([mon(16, 6), mon(16, 7), mon(18, 0)])
chk("after-close: exactly one scan", calls.count("scan"), 1)
chk("after-close: open positions settled first (hypothetical + real)", (calls.count("close_eod"), calls.count("real_eod")), (1, 1))
chk("after-close: settle happens BEFORE the scan", calls[:3], ["close_eod", "real_eod", "scan"])
chk("after-close: never does 30-minute checks", ("check" in calls or "real_check" in calls), False)
with get_db() as c: c.execute("UPDATE trade_scan_log SET scan_time = ?", (mon(16, 6).astimezone(timezone.utc).isoformat(),))
calls.clear(); run_ticks([mon(19, 0), mon(19, 1)])
chk("after-close: restart does not repeat the run", calls, [])
clean()
from fetchers.trading_logger import log_scan_event as _lse
_lse(engine="high_value", triggered_by="manual", symbols_scanned=1, trade_ids_logged=[])
with get_db() as c: c.execute("UPDATE trade_scan_log SET scan_time = ?", (mon(12, 0).astimezone(timezone.utc).isoformat(),))
calls.clear(); run_ticks([mon(16, 10), mon(16, 11)])
chk("after-close: a manual noon click does not cancel the daily run", calls.count("scan"), 1)
clean(); bar_today["has"] = False; calls.clear()
run_ticks([mon(16, 10), mon(16, 20), mon(16, 30)])
chk("after-close: no bar for today yet (before 16:40) -> waits, no scan", calls, [])
calls.clear(); run_ticks([mon(16, 45), mon(16, 50), mon(17, 30)])
chk("after-close: still no bar at 16:45 -> treated as a holiday, no scan", calls, [])
bar_today["has"] = True; clean(); calls.clear(); run_ticks([sat.replace(hour=17), sat.replace(hour=18)])
chk("after-close: weekend does nothing", calls, [])
print("PASS: all checks agree" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
