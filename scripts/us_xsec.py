#!/usr/bin/env python3
"""美股横截面动量(12-1):每月按动量排序,做多前 20%。"""
import sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.us_backtest import load_all, COST
from qe.backtest.engine import Backtester


def main(mode="eq", top_frac=0.2, trend=False, reb_days=21, mkt_scale=0.0):
    data = load_all()
    px = pd.DataFrame({s: d["close"] for s, d in data.items()}).sort_index()
    px = px.dropna(how="all")
    print(f"面板 {px.shape[0]} 天 × {px.shape[1]} 只")
    ret = px.pct_change()
    mom = px.shift(21) / px.shift(252) - 1
    reb = pd.Series(False, index=px.index)
    reb.iloc[::reb_days] = True
    w = pd.DataFrame(0.0, index=px.index, columns=px.columns)
    for i in range(1, len(px)):
        if not reb.iloc[i]:
            w.iloc[i] = w.iloc[i - 1]
            continue
        m = mom.iloc[i].dropna()
        if len(m) < 30:
            continue
        if trend:
            sma = px.iloc[max(0, i-200):i+1].mean()
            m = m[px.iloc[i][m.index] > sma[m.index]]
            if len(m) < 10:
                continue
        k = max(1, int(len(m) * top_frac))
        top = m.nlargest(k).index
        w.iloc[i] = 0.0
        if mode == "eq":
            w.loc[px.index[i], top] = 1.0 / k
        else:
            vol = ret.iloc[max(0, i-63):i].std()
            iv = 1.0 / vol.loc[top]
            w.loc[px.index[i], top] = (iv / iv.sum()).values
    if mkt_scale > 0:
        mkt = px.mean(axis=1)
        sma = mkt.rolling(200).mean()
        risk_off = (mkt < sma).astype(float)
        w = w.mul(1 - (1 - mkt_scale) * risk_off, axis=0)
    w = w.shift(1).fillna(0)
    r = Backtester(px, COST, initial=1e4, max_leverage=1.0, funding=None).run(w)
    eq = pd.Series(r.equity, index=px.index).dropna()
    d = eq.pct_change().dropna()
    yrs = len(d) / 252
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    sh = d.mean() / (d.std() + 1e-12) * np.sqrt(252)
    dd = (eq / eq.cummax() - 1).min()
    print(f"  横截面动量: CAGR {cagr*100:.1f}% | Sharpe {sh:.2f} | DD {dd*100:.1f}%")
    return cagr, sh, dd


if __name__ == "__main__":
    main()
