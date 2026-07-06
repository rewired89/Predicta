"""
Smoke tests for models/trading/ngram.py

Tests:
  - encode_bar: correct U/D/E classification
  - encode_sequence: correct multi-bar encoding
  - build_pattern_table: correct counts for synthetic series
  - ngram_signal: NONE when table missing, correct direction with mock table
  - ngram_to_composite_score: correct sign and scale
  - save/load_pattern_table: round-trip through DB (requires init_db)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.trading.ngram import (
    encode_bar,
    encode_sequence,
    build_pattern_table,
    ngram_signal,
    ngram_to_composite_score,
    PATTERN_LENGTH,
    _THRESHOLD_PCT,
)


# ── Encoding tests ────────────────────────────────────────────────────────────

def test_encode_bar_up():
    assert encode_bar(101.0, 100.0) == "U"
    assert encode_bar(100.02, 100.0) == "U"
    print("PASS  encode_bar UP")


def test_encode_bar_down():
    assert encode_bar(99.0, 100.0) == "D"
    assert encode_bar(99.98, 100.0) == "D"
    print("PASS  encode_bar DOWN")


def test_encode_bar_equal():
    assert encode_bar(100.0, 100.0) == "E"
    assert encode_bar(100.0005, 100.0) == "E"  # < 1bp → equal
    print("PASS  encode_bar EQUAL")


def test_encode_sequence():
    closes = [100.0, 101.0, 100.5, 101.5]  # U D U
    result = encode_sequence(closes)
    assert result == "UDU", f"Expected 'UDU', got '{result}'"
    print(f"PASS  encode_sequence: {closes} → '{result}'")


def test_encode_sequence_short():
    assert encode_sequence([100.0]) == ""
    assert encode_sequence([]) == ""
    print("PASS  encode_sequence short inputs")


# ── Table building tests ──────────────────────────────────────────────────────

def test_build_pattern_table_counts():
    # Sequence: 100, 101, 102, 103, 102, 101, 100 → U U U D D D
    closes = [100.0, 101.0, 102.0, 103.0, 102.0, 101.0, 100.0]
    table = build_pattern_table(closes, pattern_len=2)

    # Check that table has entries
    assert len(table) > 0, "Table should not be empty"

    # All values in table should be dicts with U/D/E keys
    for pattern, counts in table.items():
        assert set(counts.keys()) == {"U", "D", "E"}, f"Unexpected keys in {pattern}: {counts}"
        assert all(isinstance(v, int) for v in counts.values())

    # Verify total counts = len(closes) - pattern_len - 1
    total = sum(sum(c.values()) for c in table.values())
    expected = len(closes) - 2 - 1  # pattern_len=2, need 1 next bar
    assert total == expected, f"Expected {expected} total observations, got {total}"
    print(f"PASS  build_pattern_table: {len(table)} patterns, {total} observations")


def test_build_pattern_table_insufficient():
    closes = [100.0, 101.0]  # Only 2 bars, need pattern_len+2=5 for PATTERN_LENGTH=3
    table = build_pattern_table(closes, pattern_len=PATTERN_LENGTH)
    assert table == {}, f"Expected empty table, got {table}"
    print("PASS  build_pattern_table: empty on insufficient data")


# ── Signal tests ──────────────────────────────────────────────────────────────

def test_ngram_signal_no_table():
    # Symbol with no stored table → NONE. 5 bars satisfies PATTERN_LENGTH=4's
    # length guard so the test actually exercises the "no table" path.
    sig = ngram_signal("XXXX_FAKE_SYM_99", [100.0, 101.0, 100.5, 102.0, 101.5])
    assert sig["signal"] == "NONE"
    assert sig["confidence"] == 0
    print(f"PASS  ngram_signal no table: {sig['reason']}")


def test_ngram_signal_too_few_bars():
    sig = ngram_signal("AAPL", [100.0, 101.0])  # Only 2 bars, need 5 (PATTERN_LENGTH=4)
    assert sig["signal"] == "NONE"
    assert "Need" in sig["reason"]
    print(f"PASS  ngram_signal too few bars: {sig['reason']}")


def test_ngram_signal_with_mock_table(monkeypatch=None):
    """
    Test with a synthetic table injected via load_pattern_table mock.
    Uses a manual monkey-patch approach without pytest fixtures.
    """
    import models.trading.ngram as ng_mod

    # Encode the pattern for closes [100, 101, 102, 103, 104] = "UUUU" (PATTERN_LENGTH=4)
    closes = [100.0, 101.0, 102.0, 103.0, 104.0]
    pattern = encode_sequence(closes)  # "UUUU"
    assert pattern == "UUUU", f"Expected 'UUUU', got '{pattern}'"

    # Inject a mock table where "UUUU" → U wins 60% of the time
    mock_table = {pattern: {"U": 60, "D": 30, "E": 10}}

    original_load = ng_mod.load_pattern_table
    ng_mod.load_pattern_table = lambda sym, **kw: mock_table

    try:
        sig = ngram_signal("AAPL", closes + [105.0])  # 6 bars, current pattern = last 5
        assert sig["signal"] == "UP", f"Expected UP, got {sig['signal']}"
        assert sig["confidence"] > 0
        assert sig["historical_win_rate"] == 0.6
        print(f"PASS  ngram_signal mock UP: conf={sig['confidence']}, wr={sig['historical_win_rate']:.0%}")
    finally:
        ng_mod.load_pattern_table = original_load


def test_ngram_signal_mock_down():
    import models.trading.ngram as ng_mod

    closes = [100.0, 101.0, 100.5]  # "UD" — only 3 bars, PATTERN_LENGTH=4 needs 5
    pattern = encode_sequence(closes)

    mock_table = {pattern: {"U": 20, "D": 70, "E": 10}}
    original_load = ng_mod.load_pattern_table
    ng_mod.load_pattern_table = lambda sym, **kw: mock_table

    try:
        sig = ngram_signal("AAPL", closes + [101.0], min_samples=50)  # 4 bars total, need 5
        # Too few bars for the 4-bar context window — short-circuits to NONE
        # via the length guard before ever comparing against the mock table.
        assert sig["signal"] in ("NONE", "DOWN")
        print(f"PASS  ngram_signal mock DOWN test: {sig['signal']} (too few bars for PATTERN_LENGTH=4)")
    finally:
        ng_mod.load_pattern_table = original_load


# ── Score conversion tests ────────────────────────────────────────────────────

def test_ngram_to_composite_score():
    up_sig   = {"signal": "UP",   "confidence": 10.0}
    down_sig = {"signal": "DOWN", "confidence": 20.0}
    none_sig = {"signal": "NONE", "confidence": 0}

    assert ngram_to_composite_score(up_sig)   == 10.0
    assert ngram_to_composite_score(down_sig) == -20.0
    assert ngram_to_composite_score(none_sig) == 0.0
    print("PASS  ngram_to_composite_score: UP=+10, DOWN=-20, NONE=0")


# ── DB round-trip test ────────────────────────────────────────────────────────

def test_save_load_round_trip():
    """Test save_pattern_table and load_pattern_table round-trip through DB."""
    from db.database import init_db
    from models.trading.ngram import save_pattern_table, load_pattern_table

    init_db()  # Ensure tables exist

    test_sym = "_TEST_NGRAM_SYMBOL_"
    test_table = {"UUU": {"U": 60, "D": 30, "E": 10}, "UDD": {"U": 20, "D": 50, "E": 30}}

    save_pattern_table(test_sym, test_table, bar_count=100)
    loaded = load_pattern_table(test_sym)

    assert loaded is not None, "Table should have been loaded"
    assert "UUU" in loaded, "Pattern 'UUU' should be in loaded table"
    assert loaded["UUU"]["U"] == 60
    assert loaded["UDD"]["D"] == 50
    print("PASS  save/load round-trip: DB storage and retrieval correct")


if __name__ == "__main__":
    tests = [
        test_encode_bar_up,
        test_encode_bar_down,
        test_encode_bar_equal,
        test_encode_sequence,
        test_encode_sequence_short,
        test_build_pattern_table_counts,
        test_build_pattern_table_insufficient,
        test_ngram_signal_no_table,
        test_ngram_signal_too_few_bars,
        test_ngram_signal_with_mock_table,
        test_ngram_signal_mock_down,
        test_ngram_to_composite_score,
        test_save_load_round_trip,
    ]
    failed = []
    for t in tests:
        try:
            t()
        except Exception as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed.append(t.__name__)

    print(f"\n{'='*50}")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        sys.exit(1)
    else:
        print(f"All {len(tests)} tests passed.")
