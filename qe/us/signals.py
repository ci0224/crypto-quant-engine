"""美股信号:全部基于日线收盘,次日开盘执行(不偷价)。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma_trend(df: pd.DataFrame, n: int = 200) -> pd.Series:
    """close > SMA(n) 且 SMA 上升 → 持有。"""
    c = df["close"]
    ma = c.rolling(n).mean()
    return (c > ma) & (ma > ma.shift(20))


def mom_12_1(df: pd.DataFrame) -> pd.Series:
    """12-1 动量:过去 252 日收益(剔除最近 21 日)> 0。"""
    c = df["close"]
    return (c.shift(21) / c.shift(252) - 1) > 0


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def vcp(df: pd.DataFrame, tight_pct=0.35, vol_mult=1.2, lookback=15) -> pd.Series:
    """波动收缩 + 放量突破(加密版口径,见 research/20)。"""
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    ma50, ma200 = c.rolling(50).mean(), c.rolling(200).mean()
    amp = (h.rolling(20).max() - l.rolling(20).min()) / c
    rank = amp.rolling(100).apply(lambda s: float((s.iloc[-1] <= s).mean()), raw=False)
    contracted = (rank < tight_pct).rolling(lookback).max() > 0
    trend = (c > ma200) & (ma200 > ma200.shift(20)) & (c > ma50)
    brk = c > h.shift(1).rolling(20).max()
    vol = v > vol_mult * v.rolling(20).mean()
    return trend & contracted & brk & vol
