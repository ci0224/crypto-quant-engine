"""严格回测框架 · 数据层:1m K 线 + 真实资金费率 + 完整性审计。"""
from __future__ import annotations

import glob
import io
import os
import pickle
import zipfile
from typing import Optional

import numpy as np
import pandas as pd

BIN = "/home/ubuntu/apps/crypto-quant-trade/data/binance"


def load_1m(symbol: str) -> pd.DataFrame:
    """从 binance_data.py 的 pkl 缓存读 1m K 线(index=UTC)。"""
    p = os.path.join(BIN, f"{symbol.lower()}_1m_2020_2026.pkl.gz")
    with open(p, "rb") as f:
        df = pickle.load(f)
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    return df[cols].astype(float)


def resample(bars: pd.DataFrame, minutes: int = 5) -> pd.DataFrame:
    """聚合到 N 分钟(OHLCV + 美元成交额)。"""
    r = bars.resample(f"{minutes}min", label="left", closed="left").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
    ).dropna()
    r["dollar"] = r["volume"] * r["close"]
    return r


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    ma = close.rolling(n, min_periods=n).mean()
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    return pd.DataFrame({"mid": ma, "up": ma + k * sd, "low": ma - k * sd})


def load_funding(symbol: str) -> pd.DataFrame:
    """真实资金费率(每 8 小时结算一次),来自 Binance 官方归档。"""
    pat = os.path.join(BIN, f"{symbol}USDT-fundingRate-*.zip")
    files = sorted(glob.glob(pat))
    if not files:
        return pd.DataFrame(columns=["rate"])
    frames = []
    for fp in files:
        try:
            with zipfile.ZipFile(fp) as z:
                name = z.namelist()[0]
                raw = z.read(name)
            df = pd.read_csv(io.BytesIO(raw))
            # 官方列名:calc_time,funding_interval_hours,last_funding_rate
            if "calc_time" not in df.columns:
                continue
            out = pd.DataFrame({
                "t": pd.to_datetime(df["calc_time"].astype("int64"), unit="ms", utc=True),
                "rate": df["last_funding_rate"].astype(float),
            })
            frames.append(out)
        except Exception:
            continue
    if not frames:
        return pd.DataFrame(columns=["rate"])
    out = pd.concat(frames).drop_duplicates("t").sort_values("t").set_index("t")
    return out


def audit(bars: pd.DataFrame, symbol: str) -> dict:
    """数据完整性审计:缺口 / 重复 / OHLC 违例 / 零成交 / 时间单调。"""
    idx = bars.index
    full = pd.date_range(idx[0], idx[-1], freq="1min", tz="UTC")
    missing = len(full) - len(idx)
    dup = int(idx.duplicated().sum())
    ohlc_bad = int(((bars["high"] < bars[["open", "close"]].max(axis=1)) |
                    (bars["low"] > bars[["open", "close"]].min(axis=1)) |
                    (bars["high"] < bars["low"])).sum())
    zero_vol = int((bars["volume"] <= 0).sum())
    mono = bool(idx.is_monotonic_increasing)
    gap_pct = missing / max(len(full), 1) * 100
    return {
        "symbol": symbol, "bars": len(bars),
        "start": str(idx[0]), "end": str(idx[-1]),
        "missing_bars": missing, "gap_pct": round(gap_pct, 4),
        "duplicates": dup, "ohlc_violations": ohlc_bad,
        "zero_volume_bars": zero_vol, "monotonic_time": mono,
        "pass": (gap_pct < 0.5 and dup == 0 and ohlc_bad == 0 and mono),
    }


def boll_state(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    """原仓库的信号定义(状态机):跌破下轨→1,站上上轨→-1,回到中轨→0。

    与"逐根布尔"不同,state=1 会在整个下跌段持续为真,因此同一波下跌可多次进场
    (由 free_from 限制不重叠)。复刻原结果必须用这个定义。
    """
    ma = close.rolling(n, min_periods=n).mean()
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    up, lo = ma + k * sd, ma - k * sd
    out, st = [], 0
    c = close.values
    m, u, l = ma.values, up.values, lo.values
    for i in range(len(c)):
        if not np.isfinite(m[i]):
            out.append(0)
            continue
        if c[i] < l[i]:
            st = 1
        elif c[i] > u[i]:
            st = -1
        elif st == 1 and c[i] >= m[i]:
            st = 0
        elif st == -1 and c[i] <= m[i]:
            st = 0
        out.append(st)
    return pd.Series(out, index=close.index)
