import os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PREDICTA_DB_PATH"] = tempfile.mkdtemp() + "/t.db"
import db.database as d
d.init_db()
from db.database import get_db
from fetchers.order_ledger import guarded_order
with get_db() as c:
    c.execute("INSERT INTO intraday_trades (id,symbol,engine,is_hypothetical,logged_at,entry_time,entry_price,side,qty) VALUES (1,'ABC','low_value',1,'2026-01-01','2026-01-01',5,'long',2)")
calls = []
def send():
    calls.append(1); time.sleep(0.2); return {"id": "o1"}
out = []
ts = [threading.Thread(target=lambda: out.append(guarded_order("low_value", 1, "ABC", "buy", 2, "entry", send))) for _ in range(2)]
[t.start() for t in ts]; [t.join() for t in ts]
assert len(calls) == 2  # row not yet promoted -> both allowed, but serialized
with get_db() as c:
    c.execute("UPDATE intraday_trades SET alpaca_order_id='o1' WHERE id=1")
r = guarded_order("low_value", 1, "ABC", "buy", 2, "entry", send)
assert "error" in r and len(calls) == 2
with get_db() as c:
    st = [x["status"] for x in c.execute("SELECT status FROM order_ledger ORDER BY id")]
assert st == ["SENT", "SENT", "REFUSED_DUPLICATE"], st
print("order ledger OK", st)
