"""
Weekly maintenance script — run every Sunday before market open.

Tasks:
  1. Rebuild n-gram frequency tables for the trading watchlist
  2. Scan CANDIDATE_PAIRS for cointegration and print current status

Usage:
    python scripts/weekly_build.py

Or via cron (Sunday 8 AM ET):
    0 8 * * 0 cd /path/to/Predicta && python scripts/weekly_build.py >> logs/weekly_build.log 2>&1
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import init_db

NGRAM_WATCHLIST = [
    "AAPL", "MSFT", "SPY", "QQQ", "IWM",
    "NVDA", "TSLA", "META", "AMZN", "GOOG",
    "XOM",  "CVX",  "JPM",  "BAC",  "PEP",
    "KO",   "WMT",  "TGT",
]

NGRAM_MONTHS = 6   # 6 months of 5-min bars (~10k bars per symbol)
PAIRS_DAYS   = 90  # 90 trading days for cointegration test


def build_ngrams():
    from models.trading.high_value.ngram import build_ngram_from_alpaca

    print(f"\n=== N-gram rebuild ({len(NGRAM_WATCHLIST)} symbols, {NGRAM_MONTHS} months) ===")
    ok_count = 0
    for sym in NGRAM_WATCHLIST:
        try:
            ok = build_ngram_from_alpaca(sym, months=NGRAM_MONTHS)
            status = "OK" if ok else "SKIP (insufficient data)"
            if ok:
                ok_count += 1
        except Exception as e:
            status = f"ERROR: {e}"
        print(f"  {sym:6s}  {status}")

    print(f"\n  Built {ok_count}/{len(NGRAM_WATCHLIST)} tables.")


def scan_pairs():
    from models.trading.pairs import find_all_pairs, generate_pair_signal, CANDIDATE_PAIRS

    print(f"\n=== Pairs scan ({len(CANDIDATE_PAIRS)} candidates, {PAIRS_DAYS} days) ===")
    pairs = find_all_pairs(days=PAIRS_DAYS, min_half_life=1.0, max_half_life=30.0)

    if not pairs:
        print("  No cointegrated pairs found within half-life filter.")
        return

    for p in pairs:
        sig  = generate_pair_signal(p)
        action = sig.get("action", "NONE")
        z      = p["current_zscore"]
        hl     = p["half_life_days"]
        pval   = p["pvalue"]
        print(
            f"  {p['sym1']:5s}/{p['sym2']:5s}  "
            f"p={pval:.3f}  hl={hl}d  z={z:+.2f}  → {action}"
        )

    print(f"\n  {len(pairs)} cointegrated pair(s) found.")


if __name__ == "__main__":
    print("Initialising database…")
    init_db()

    try:
        build_ngrams()
    except Exception as e:
        print(f"N-gram build failed: {e}")

    try:
        scan_pairs()
    except Exception as e:
        print(f"Pairs scan failed: {e}")

    print("\nWeekly build complete.")
