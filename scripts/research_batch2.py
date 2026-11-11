#!/usr/bin/env python3
"""批量研究 v2:全部走向量化引擎(复利 + 换手成本),这才是可比的。

    python scripts/research_batch2.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe.backtest.engine import Backtester
from qe.config import Costs
from qe.strict.data import load_1m, load_funding
from scripts.research_batch import atr, rsi, state_w

COST = Costs(taker_bps=4.5, slippage_bps=1.0)


def bars1h(sym):
    b = load_1m(sym)
    return b.resample("1h").agg({"open": "first", "high": "max", "low": "min",
                                 "close": "last", "volume": "sum"}).dropna()


def make_rules(b, sym):
    c = b["close"]
    out = {}
    r2, r14 = rsi(c, 2), rsi(c, 14)
    ema20 = c.ewm(span=20, adjust=False).mean()
    sd20 = c.rolling(20).std()
    a = atr(b, 20)
    out["RSI(2)超卖反转"] = state_w(len(b), r2 < 10, r2 > 60)
    out["RSI(14)超卖反转"] = state_w(len(b), r14 < 30, r14 > 55)
    out["布林回归"] = state_w(len(b), c < ema20 - 2 * sd20, c > ema20)
    out["布林突破"] = state_w(len(b), c > ema20 + 2 * sd20, c < ema20)
    out["Keltner反弹"] = state_w(len(b), c < ema20 - 2 * a, c > ema20)
    out["ATR通道突破"] = state_w(len(b), c > ema20 + 2 * a, c < ema20)
    out["Donchian突破"] = state_w(len(b), c > b["high"].rolling(20).max().shift(1),
                                  c < b["low"].rolling(10).min().shift(1))
    out["动量24h"] = np.sign(c.pct_change(24).fillna(0)).to_numpy()
    out["动量72h"] = np.sign(c.pct_change(72).fillna(0)).to_numpy()
    out["EMA9/21"] = np.sign(c.ewm(span=9).mean() - c.ewm(span=21).mean()).to_numpy()
    out["EMA50/200"] = np.sign(c.ewm(span=50).mean() - c.ewm(span=200).mean()).to_numpy()
    out["EMA20/100"] = np.sign(c.ewm(span=20).mean() - c.ewm(span=100).mean()).to_numpy()
    down3 = (c.diff() < 0).rolling(3).sum() == 3
    out["连跌3根"] = state_w(len(b), down3, pd.Series(np.arange(len(b)) % 2 == 0))
    try:
        fs = load_funding(sym).reindex(b.index, method="ffill")
        out["资金费极值"] = state_w(len(b), fs > 0.0003, fs < 0.0001)
        out["资金费反向"] = state_w(len(b), fs < -0.0002, fs > 0)
    except Exception:
        pass
    return out


def metrics(b, w):
    px = b["close"].rename("X").to_frame()
    wf = pd.Series(np.asarray(w, float), index=b.index, name="X").to_frame()
    r = Backtester(px, COST, initial=10_000.0, max_leverage=1.0, funding=None).run(wf)
    eq = pd.Series(np.asarray(r.equity, dtype=float), index=b.index)
    if eq.iloc[-1] <= 0 or not np.isfinite(eq.iloc[-1]):
        return None
    yrs = max((b.index[-1] - b.index[0]).days / 365, 0.1)
    tot = eq.iloc[-1] / eq.iloc[0]
    d = eq.resample("D").last().pct_change().dropna()
    dd = float((eq / eq.cummax() - 1).min())
    turn = float(np.abs(np.diff(np.asarray(w, float))).sum() / yrs)
    return dict(cagr=(tot ** (1 / yrs) - 1) * 100, sh=float(d.mean() / d.std() * np.sqrt(365)) if d.std() else 0,
                dd=dd * 100, turn=turn, cost=float(r.cost_paid.sum() / eq.iloc[0]) * 100)


def main():
    rows = []
    for sym in ("BTC", "ETH", "SOL"):
        b = bars1h(sym)
        for name, w in make_rules(b, sym).items():
            m = metrics(b, w)
            if m is None:
                rows.append(dict(sym=sym, rule=name, cagr=-100, sh=0, dd=-100, turn=0, cost=0))
                continue
            rows.append(dict(sym=sym, rule=name, **m))
    df = pd.DataFrame(rows).sort_values("sh", ascending=False)
    pd.set_option("display.width", 200, "display.max_rows", 60)
    print("\n=== 引擎口径(复利,11bp 往返,1x,1h)===")
    print(df.to_string(index=False, float_format=lambda x: f"{x:8.2f}"))
    good = df[(df.cagr > 0) & (df.sh > 1.0) & (df.dd > -60)]
    print(f"\n通过(CAGR>0, Sharpe>1, DD>-60%):{len(good)} / {len(df)}")
    print(good.to_string(index=False, float_format=lambda x: f"{x:8.2f}"))
    df.to_csv("/tmp/research_batch2.csv", index=False)


if __name__ == "__main__":
    main()
