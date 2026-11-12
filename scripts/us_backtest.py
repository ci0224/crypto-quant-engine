#!/usr/bin/env python3
"""美股全池回测:3 个信号 × 109 只,组合等权。"""
import sys, glob
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qe.config import Costs
from qe.backtest.engine import Backtester
from qe.us.signals import sma_trend, mom_12_1, vcp

DATA = Path(__file__).resolve().parent.parent / "data" / "us"
COST = Costs(taker_bps=0.0, slippage_bps=5.0)


def load_all():
    out = {}
    for f in sorted(DATA.glob("*.pkl")):
        try:
            df = pd.read_pickle(f)
            if len(df) > 800:
                out[f.stem] = df
        except Exception:
            pass
    return out


def run():
    data = load_all()
    print(f"标的 {len(data)} 只")
    res = {}
    for name, fn in (("sma200", sma_trend), ("mom12_1", mom_12_1), ("vcp", vcp)):
        rets, per = {}, []
        for sym, df in data.items():
            try:
                w = fn(df).astype(float)
                px = df["close"].rename("X").to_frame()
                ww = w.rename("X").to_frame()
                r = Backtester(px, COST, initial=1e4, max_leverage=1.0, funding=None).run(ww)
                eq = pd.Series(r.equity, index=df.index)
                d = eq.pct_change().dropna()
                if len(d) < 500:
                    continue
                rets[sym] = d
                yrs = len(d) / 252
                cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
                sh = d.mean() / (d.std() + 1e-12) * np.sqrt(252)
                dd = (eq / eq.cummax() - 1).min()
                per.append((sym, cagr, sh, dd))
            except Exception:
                continue
        if not rets:
            continue
        port = pd.DataFrame(rets).fillna(0).mean(axis=1)
        eq = (1 + port).cumprod()
        yrs = len(port) / 252
        cagr = eq.iloc[-1] ** (1 / yrs) - 1
        sh = port.mean() / (port.std() + 1e-12) * np.sqrt(252)
        dd = (eq / eq.cummax() - 1).min()
        res[name] = dict(n=len(per), cagr=cagr, sharpe=sh, dd=dd, per=per)
        print(f"\n=== {name} ===")
        print(f"  组合: CAGR {cagr*100:.1f}% | Sharpe {sh:.2f} | DD {dd*100:.1f}% | 持仓天数占比 {float((port!=0).mean())*100:.0f}%")
        per.sort(key=lambda x: -x[2])
        for sym, c, s, d in per[:3]:
            print(f"    最好 {sym}: CAGR {c*100:.1f}% Sharpe {s:.2f} DD {d*100:.1f}%")
    return res


if __name__ == "__main__":
    run()
