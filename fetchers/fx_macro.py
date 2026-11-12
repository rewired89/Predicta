"""
Macro/risk data for the FX Forecast engine (read-only, FRED, free key).

Everything is made point-in-time: a value is only usable from its
`avail` date, which is the observation date plus a publication lag. Monthly
interest-rate series get 45 days (published mid next month), daily series 1 day.
Without this the test would quietly use numbers nobody had yet.
"""
import logging
import os

import pandas as pd
import requests

from db.database import get_db

log = logging.getLogger("fx_macro")

# series id -> (what it is, publication lag in days)
SERIES = {
    "IR3TIB01EZM156N": ("Euro area 3-month interest rate (monthly)", 45),
    "IR3TIB01GBM156N": ("UK 3-month interest rate (monthly)", 45),
    "IR3TIB01JPM156N": ("Japan 3-month interest rate (monthly)", 45),
    "IR3TIB01USM156N": ("US 3-month interest rate (monthly)", 45),
    "VIXCLS": ("VIX, market fear gauge (daily)", 1),
    "DCOILWTICO": ("Oil price WTI (daily)", 1),
    "DGS2": ("US 2-year yield (daily)", 1),
    "DGS10": ("US 10-year yield (daily)", 1),
    "DTWEXBGS": ("Broad US dollar index (daily)", 1),
}
# pair -> (rate series of first currency, rate series of second, sign): carry = r_a - r_b
CARRY = {
    "EUR_USD": ("IR3TIB01EZM156N", "IR3TIB01USM156N"),
    "GBP_USD": ("IR3TIB01GBM156N", "IR3TIB01USM156N"),
    "USD_JPY": ("IR3TIB01USM156N", "IR3TIB01JPM156N"),
}

_DDL = "CREATE TABLE IF NOT EXISTS fx_macro_series (series_id TEXT NOT NULL, date TEXT NOT NULL, value REAL, PRIMARY KEY (series_id, date))"


def parse_observations(payload: dict) -> list[tuple[str, float]]:
    out = []
    for o in payload.get("observations", []):
        try:
            if o.get("value") in (None, "."):
                continue
            out.append((o["date"], float(o["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def parse_csv(text: str) -> list[tuple[str, float]]:
    """FRED's public 'Download Data' CSV: header 'observation_date,SERIES' (or 'DATE,SERIES'); '.' or blank = missing."""
    out = []
    for i, line in enumerate(text.strip().splitlines()):
        parts = line.strip().split(",")
        if i == 0 or len(parts) < 2:
            continue
        try:
            if parts[1] in ("", "."):
                continue
            out.append((parts[0], float(parts[1])))
        except ValueError:
            continue
    return out


def fetch_series_csv(series_id: str, start: str = "2000-01-01") -> list[tuple[str, float]]:
    """No-key path: the same CSV the 'Download Data' button on a FRED chart page gives."""
    r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv", timeout=30,
                     params={"id": series_id, "cosd": start},
                     headers={"User-Agent": "Predicta-research/1.0"})
    r.raise_for_status()
    return parse_csv(r.text)


def _has_key() -> bool:
    k = os.environ.get("FRED_API_KEY", "")
    return bool(k) and not k.startswith("your_")


def fetch_series(series_id: str, start: str = "2000-01-01") -> list[tuple[str, float]]:
    if not _has_key():
        return fetch_series_csv(series_id, start)
    key = os.environ["FRED_API_KEY"]
    r = requests.get("https://api.stlouisfed.org/fred/series/observations", timeout=30,
                     params={"series_id": series_id, "api_key": key, "file_type": "json",
                             "observation_start": start})
    r.raise_for_status()
    return parse_observations(r.json())


def backfill() -> dict:
    import time
    res = {"_source": "FRED API key" if _has_key() else "FRED public CSV download (no key needed)"}
    for sid in SERIES:
        try:
            rows = fetch_series(sid)
            time.sleep(0.5)
            with get_db() as conn:
                conn.execute(_DDL)
                for d, v in rows:
                    conn.execute("INSERT OR REPLACE INTO fx_macro_series (series_id,date,value) VALUES (?,?,?)", (sid, d, v))
            res[sid] = {"saved": len(rows)} if rows else {"saved": 0, "note": "FRED returned no rows for this series"}
        except Exception as e:
            msg = str(e)
            key = os.environ.get("FRED_API_KEY", "")
            if key:
                msg = msg.replace(key, "***")
            msg = msg.split("api_key=")[0] + ("(key hidden)" if "api_key=" in msg else "")
            res[sid] = {"error": msg[:160], "hint": "If a key is set it may be wrong; remove FRED_API_KEY from .env to use the no-key download instead"}
    return res


def load_series(series_id: str) -> pd.DataFrame:
    """date, value, avail (first day the value could be known)."""
    with get_db() as conn:
        conn.execute(_DDL)
        rows = conn.execute("SELECT date, value FROM fx_macro_series WHERE series_id=? ORDER BY date", (series_id,)).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df["avail"] = df["date"] + pd.Timedelta(days=SERIES[series_id][1])
    return df.sort_values("avail")


def _asof(bars_dates: pd.Series, s: pd.DataFrame) -> pd.Series:
    """Latest value known on each bar date (never looks ahead)."""
    left = pd.DataFrame({"date": pd.to_datetime(bars_dates)}).sort_values("date")
    m = pd.merge_asof(left, s[["avail", "value"]].rename(columns={"avail": "date"}).sort_values("date"),
                      on="date", direction="backward")
    return pd.Series(m["value"].to_numpy(), index=bars_dates.index)


def attach(d: pd.DataFrame, pair: str, level: str, loader=load_series):
    """
    Add macro columns to the feature frame `d` (from forecast.build).
    level: "carry" (rate difference only) or "macro" (carry + risk/dollar/oil/yields).
    Returns (frame, new_feature_names, missing_series).
    """
    d = d.copy()
    feats, missing = [], []
    a, b = CARRY[pair]
    sa, sb = loader(a), loader(b)
    if sa.empty or sb.empty:
        missing += [x for x, s in ((a, sa), (b, sb)) if s.empty]
    else:
        d["carry"] = _asof(d["date"], sa) - _asof(d["date"], sb)
        d["carry_chg3m"] = d["carry"] - d["carry"].shift(63)
        feats += ["carry", "carry_chg3m"]
    if level == "macro":
        def get(sid):
            s = loader(sid)
            if s.empty:
                missing.append(sid)
                return None
            return _asof(d["date"], s)
        vix = get("VIXCLS")
        if vix is not None:
            d["vix"], d["vix_chg5"] = vix, vix / vix.shift(5) - 1
            feats += ["vix", "vix_chg5"]
        oil = get("DCOILWTICO")
        if oil is not None:
            prev = oil.shift(20)
            d["oil_ret20"] = (oil - prev) / prev.abs().clip(lower=10)
            feats += ["oil_ret20"]
        usd = get("DTWEXBGS")
        if usd is not None:
            d["usd_ret20"] = usd / usd.shift(20) - 1
            feats += ["usd_ret20"]
        y2, y10 = get("DGS2"), get("DGS10")
        if y2 is not None:
            d["us2y"] = y2
            feats += ["us2y"]
        if y2 is not None and y10 is not None:
            d["us_curve"] = y10 - y2
            feats += ["us_curve"]
    return d, feats, missing
