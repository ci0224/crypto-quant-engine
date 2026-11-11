#!/usr/bin/env python3
"""复现用户给的 4 个策略(除 HYPE,无数据),用严格成本 + 全样本 + 最近 180 天对比。

规则(按字面理解,参数原样):
  A. SOL EMA50/200 双向趋势：w = sign(EMA50 - EMA200)
  B. SOL 布林突破做多：收盘 > 上轨 入场,w=1；收盘 < 中轨 出场,w=0
  C. HYPE RSI(7) 超卖抄底：无 HYPE 数据,跳过
  D. BTC 72h 动量双向：w = sign(close/close[-72h] - 1)
成本:taker 11bp 往返(4.5×2 + 1×2),按换手计。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe.config import Costs
from qe.backtest.engine import Backtester
from qe.strict.data import load_1m

COST = Costs(taker_bps=4.5, maker_bps=1.5, slippage_bps=1.0)


def resample_ohlc(b1: pd.DataFrame, rule: str) -> pd.DataFrame:
    return b1.resample(rule).agg({"open": "first", "high": "max", "low": "min",
                                  "close": "last", "volume": "sum"}).dropna()


def ema_cross(close: pd.Series, fast: int, slow: int) -> pd.Series:
    f, s = close.ewm(span=fast, adjust=False).mean(), close.ewm(span=slow, adjust=False).mean()
    return np.sign(f - s).rename("w")


def boll_breakout(df: pd.DataFrame, n: int = 20, k: float = 2.0) -> pd.Series:
    ma, sd = df["close"].rolling(n).mean(), df["close"].rolling(n).std()
    up, mid = ma + k * sd, ma
    w = pd.Series(0.0, index=df.index)
    pos = 0
    for i in range(len(df)):
        c = df["close"].iloc[i]
        if pos == 0 and c > up.iloc[i]:
            pos = 1
        elif pos == 1 and c < mid.iloc[i]:
            pos = 0
        w.iloc[i] = pos
    return w


def momentum(close: pd.Series, bars: int) -> pd.Series:
    return np.sign(close / close.shift(bars) - 1).rename("w")


def rsi(close: pd.Series, n: int = 7) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50)


def rsi_dip(close: pd.Series, n: int = 7, lo: float = 30, hi: float = 50) -> pd.Series:
    r = rsi(close, n)
    w = pd.Series(0.0, index=close.index)
    pos = 0
    for i in range(len(r)):
        if pos == 0 and r.iloc[i] < lo:
            pos = 1
        elif pos == 1 and r.iloc[i] > hi:
            pos = 0
        w.iloc[i] = pos
    return w


def stats(px: pd.Series, w: pd.Series, days: float) -> dict:
    col = "X"
    bt = Backtester(px.rename(col).to_frame(), COST, initial=10_000.0,
                    max_leverage=1.0, funding=None)
    r = bt.run(w.rename(col).to_frame())
    eq = r.equity
    ret = eq.iloc[-1] / eq.iloc[0] - 1
    yrs = max(days / 365.25, 1e-9)
    cagr = (1 + ret) ** (1 / yrs) - 1
    d = r.returns
    sharpe = d.mean() / d.std(ddof=1) * np.sqrt(365) if d.std(ddof=1) > 0 else 0
    dd = (eq / eq.cummax() - 1).min()
    tw = w.diff().abs().sum()
    return {"ret": ret * 100, "cagr": cagr * 100, "sharpe": sharpe,
            "dd": dd * 100, "trades": int(tw > 0) and int((w.diff().abs() > 0).sum()),
            "turnover": float(tw)}


def main() -> int:
    print("复现口径:Binance 1m 数据,taker 11bp 往返,1x 名义,无杠杆\n")
    for sym, label in (("SOL", "SOL"), ("BTC", "BTC")):
        b1 = load_1m(sym)
        for rule, names in (("1h", "1h"), ("4h", "4h"), ("1D", "1d")):
            b = resample_ohlc(b1, rule)
            if len(b) < 300:
                continue
            n180 = b.loc[b.index >= b.index[-1] - pd.Timedelta(days=180)]
            print(f"--- {label} {names} ---")
            sp = b["close"]
            for nm, w_full in (
                ("EMA50/200 双向", ema_cross(sp, 50, 200)),
                ("布林突破做多", boll_breakout(b)),
                ("72h 动量双向", momentum(sp, 72 if rule == "1h" else 18)),
            ):
                w = w_full.fillna(0)
                full = stats(sp, w, (b.index[-1] - b.index[0]).days)
                w180 = w.loc[n180.index]
                h = stats(sp.loc[n180.index], w180, 180)
                print(f"  {nm:<16s} 全样本: 收益{full['ret']:>8.1f}% CAGR{full['cagr']:>6.1f}% "
                      f"Sharpe{full['sharpe']:>5.2f} DD{full['dd']:>6.1f}% | "
                      f"近180天: {h['ret']:>6.1f}% Sharpe{h['sharpe']:>5.2f} DD{h['dd']:>6.1f}%")
    print("\n注:HYPE 无 K 线数据(币安无 HYPEUSDT 永续),该策略无法复现。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
