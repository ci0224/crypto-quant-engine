"""Hyperliquid 数据客户端(零依赖 stdlib + pandas)。

只用公开的 /info 接口:
  · candleSnapshot  → K 线(OHLCV)
  · metaAndAssetCtxs → 全市场 mark/funding/OI 快照
  · fundingHistory   → 资金费率历史(小时级)
  · allMids          → 最新中间价
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.request
from pathlib import Path

import pandas as pd

from ..config import API_URL, CACHE_DIR


def _post(payload: dict, timeout: int = 30, retries: int = 3) -> object:
    body = json.dumps(payload).encode()
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(API_URL, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"Hyperliquid 请求失败: {payload.get('type')}: {last}")


# ---------------- K 线 ----------------
def candles(coin: str, interval: str = "1d", days: int = 730) -> pd.DataFrame:
    now = int(time.time() * 1000)
    raw = _post({"type": "candleSnapshot",
                 "req": {"coin": coin, "interval": interval,
                         "startTime": now - days * 86_400_000, "endTime": now}})
    rows = raw if isinstance(raw, list) else []
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.set_index("time").sort_index()
    for c in ("o", "h", "l", "c", "v"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    return df[["open", "high", "low", "close", "volume"]].dropna()


# ---------------- 资金费率 ----------------
def funding_history(coin: str, days: int = 365) -> pd.DataFrame:
    """小时级资金费率(返回 index=time, funding=每小时费率)。"""
    now = int(time.time() * 1000)
    out, start = [], now - days * 86_400_000
    # 接口单次返回有限条,分批拉取
    while start < now:
        chunk = _post({"type": "fundingHistory", "coin": coin, "startTime": start})
        if not isinstance(chunk, list) or not chunk:
            break
        out.extend(chunk)
        last = max(x["time"] for x in chunk)
        if last <= start:
            break
        start = last + 1
        if len(out) > 200_000:
            break
    if not out:
        return pd.DataFrame(columns=["funding"])
    df = pd.DataFrame(out).drop_duplicates(subset=["time"])
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df["funding"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    return df.set_index("time").sort_index()[["funding"]]


# ---------------- 快照 ----------------
def asset_ctxs() -> pd.DataFrame:
    """全部永续合约的最新 mark 价 / 资金费 / 未平仓量。"""
    meta, ctxs = _post({"type": "metaAndAssetCtxs"})
    rows = []
    for u, c in zip(meta["universe"], ctxs):
        rows.append({
            "coin": u["name"],
            "mark": float(c.get("markPx") or 0),
            "funding_hourly": float(c.get("funding") or 0),
            "open_interest": float(c.get("openInterest") or 0),
            "prev_day_px": float(c.get("prevDayPx") or 0),
            "day_volume": float(c.get("dayNtlVlm") or 0),
        })
    df = pd.DataFrame(rows).set_index("coin")
    return df


def all_mids() -> dict[str, float]:
    d = _post({"type": "allMids"})
    return {k: float(v) for k, v in d.items() if not k.startswith("#")}


# ---------------- 本地缓存 ----------------
def _cache_path(kind: str, coin: str, interval: str = "") -> Path:
    tag = f"{coin}_{interval}" if interval else coin
    return CACHE_DIR / f"{kind}_{tag}.csv.gz"


def save(df: pd.DataFrame, kind: str, coin: str, interval: str = "") -> Path:
    p = _cache_path(kind, coin, interval)
    with gzip.open(p, "wt", encoding="utf-8") as f:
        df.to_csv(f)
    return p


def load(kind: str, coin: str, interval: str = "") -> pd.DataFrame | None:
    p = _cache_path(kind, coin, interval)
    if not p.exists():
        return None
    with gzip.open(p, "rt", encoding="utf-8") as f:
        df = pd.read_csv(f, index_col=0, parse_dates=True)
    return df
