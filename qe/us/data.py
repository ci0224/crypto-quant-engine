"""美股数据层:Nasdaq 免费接口,10 年已复权日线。

实测(2026-11-12):一次请求返回 2,513 根,含 OHLCV,无需 API key。
注意:免费接口无幸存者偏差校正 —— 只包含"今天还在"的股票。
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

import pandas as pd

CACHE = Path(__file__).resolve().parent.parent.parent / "data" / "us"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0",
      "Referer": "https://www.nasdaq.com/", "Accept": "application/json"}
BASE = "https://api.nasdaq.com/api/quote"


def _num(s):
    if s is None:
        return float("nan")
    return float(str(s).replace("$", "").replace(",", "").strip())


def _one(sym, start):
    url = f"{BASE}/{sym}/historical?assetclass=stocks&fromdate={start}&limit=9999"
    import subprocess
    raw = subprocess.run(
        ["curl", "-s", "-m", "25", "-A", UA["User-Agent"],
         "-H", "Referer: https://www.nasdaq.com/", url],
        capture_output=True, text=True).stdout
    try:
        d = json.loads(raw)
    except Exception:
        return []
    return ((d.get("data") or {}).get("tradesTable") or {}).get("rows") or []


def fetch_daily(sym, years=10):
    """分段拉取(接口对单次区间有上限)。返回 index=DatetimeIndex。"""
    import datetime as dt
    today = dt.date.today()
    rec = []
    for k in range(years // 3 + 1):
        end = today - dt.timedelta(days=365 * 3 * k)
        start = end - dt.timedelta(days=365 * 3)
        rows = _one(sym, start.isoformat())
        if not rows:
            time.sleep(0.5)
            rows = _one(sym, start.isoformat())
        for x in rows:
            try:
                rec.append({
                    "date": pd.to_datetime(x["date"], format="%m/%d/%Y"),
                    "open": _num(x["open"]), "high": _num(x["high"]),
                    "low": _num(x["low"]), "close": _num(x["close"]),
                    "volume": _num(x["volume"]),
                })
            except Exception:
                continue
        time.sleep(0.3)
    if not rec:
        return pd.DataFrame()
    df = pd.DataFrame(rec).dropna().drop_duplicates("date")
    return df.sort_values("date").set_index("date")


def load_daily(sym, refresh=False):
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{sym}.pkl"
    if f.exists() and not refresh and (time.time() - f.stat().st_mtime) < 86400:
        return pd.read_pickle(f)
    df = fetch_daily(sym)
    if not df.empty:
        df.to_pickle(f)
    return df


def get_many(syms, sleep=0.2, refresh=False):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out, bad = {}, []

    def one(sym):
        try:
            df = load_daily(sym, refresh=refresh)
            time.sleep(sleep)
            return sym, df
        except Exception:
            return sym, pd.DataFrame()

    with ThreadPoolExecutor(max_workers=4) as ex:
        for f in as_completed([ex.submit(one, s) for s in syms]):
            sym, df = f.result()
            if df is not None and not df.empty:
                out[sym] = df
            else:
                bad.append(sym)
    if bad:
        print(f"  失败 {len(bad)}: {bad[:10]}")
    return out
