#!/usr/bin/env python3
"""检验:把两个 ETH 突破合并,到底有没有意义(相关性 / 有效押注数 / 组合效果)。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe.backtest.engine import Backtester
from qe.config import Costs
from scripts.research_batch2 import bars1h, make_rules

COST = Costs(taker_bps=4.5, slippage_bps=1.0)


def equity(b, w):
    px = b["close"].rename("X").to_frame()
    wf = pd.Series(np.asarray(w, float), index=b.index, name="X").to_frame()
    r = Backtester(px, COST, initial=10_000.0, max_leverage=1.0, funding=None).run(wf)
    return pd.Series(np.asarray(r.equity, float), index=b.index)


def stats(eq):
    d = eq.resample("D").last().pct_change().dropna()
    yrs = (eq.index[-1] - eq.index[0]).days / 365
    tot = eq.iloc[-1] / eq.iloc[0]
    return dict(cagr=(tot ** (1 / yrs) - 1) * 100,
                sh=d.mean() / d.std() * np.sqrt(365),
                dd=(eq / eq.cummax() - 1).min() * 100,
                vol=d.std() * np.sqrt(365) * 100)


def main():
    b = bars1h("ETH")
    R = make_rules(b, "ETH")
    ea = equity(b, R["ATR通道突破"])
    eb = equity(b, R["布林突破"])
    ra, rb = ea.resample("D").last().pct_change().dropna(), eb.resample("D").last().pct_change().dropna()
    both = pd.concat([ra, rb], axis=1).dropna()
    rho = both.corr().iloc[0, 1]

    sa, sb = stats(ea), stats(eb)
    ec = (ea / ea.iloc[0] + eb / eb.iloc[0]) / 2 * 10_000  # 等权 50/50
    sc = stats(ec)
    # 信号重叠度
    wa = np.asarray(R["ATR通道突破"], float)
    wb = np.asarray(R["布林突破"], float)
    overlap = ((wa > 0) & (wb > 0)).mean() / max((wa > 0).mean(), 1e-9)

    print(f"日收益相关性 ρ = {rho:.3f}")
    print(f"信号重叠:布林有仓时 ATR 也持仓的比例 = {overlap*100:.0f}%")
    print(f"有效押注数 (1/ρ² 近似) ≈ {1/max(rho**2,1e-9):.2f}\n")
    for name, s in (("ATR突破", sa), ("布林突破", sb), ("等权组合", sc)):
        print(f"  {name:8s} CAGR {s['cagr']:6.1f}% | Sharpe {s['sh']:5.2f} | DD {s['dd']:6.1f}% | Vol {s['vol']:5.1f}%")
    print(f"\n组合 vs 最好的单个:Sharpe {sc['sh']:.2f} vs {max(sa['sh'],sb['sh']):.2f} "
          f"({(sc['sh']/max(sa['sh'],sb['sh'])-1)*100:+.1f}%)")


if __name__ == "__main__":
    main()
