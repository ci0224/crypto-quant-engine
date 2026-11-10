#!/usr/bin/env python3
"""严格验证:
  1) 资金费套利:限制在"有资金费数据"的窗口内做 60/40 分割
  2) 趋势跟踪:4 折 walk-forward(滚动训练→样本外)
  3) 成本敏感性:maker/taker × 滑点
  4) 月度收益分解
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe.backtest.engine import Backtester  # noqa: E402
from qe.config import Costs, Config  # noqa: E402
from qe.data.panel import load_panel  # noqa: E402
from qe.strategies import library as S  # noqa: E402


def bt(px, fd, w, costs, initial=10_000):
    return Backtester(px, costs=costs, funding=fd, initial=initial, max_leverage=2.0).run(w)


def line(tag, r):
    s = r.stats
    return (f"{tag:22s} CAGR {s['cagr']*100:+7.1f}% · Sharpe {s['sharpe']:5.2f} · "
            f"MaxDD {s['max_dd']*100:6.1f}% · 波动 {s['vol']*100:5.1f}% · 期末 ${r.equity.iloc[-1]:,.0f}")


def main():
    cfg = Config.load()
    px, fd = load_panel("1d")

    print("=" * 96)
    print("① 资金费套利 —— 只在有资金费数据的窗口内评估")
    print("=" * 96)
    if fd.empty or fd.abs().sum().sum() == 0:
        print("  无资金费数据")
    else:
        start = fd[fd.abs().sum(axis=1) > 0].index[0]
        px_f, fd_f = px[px.index >= start], fd[fd.index >= start]
        print(f"  资金费覆盖: {start:%Y-%m-%d} → {fd_f.index[-1]:%Y-%m-%d}({len(px_f)} 根)")
        cut = int(len(px_f) * 0.6)
        for lb, tn in [(7, 5), (14, 5), (14, 3), (3, 5)]:
            w = S.funding_carry(px_f, fd_f, lookback_days=lb, top_n=tn, rebalance=7)
            full = bt(px_f, fd_f, w, cfg.costs)
            tr = bt(px_f.iloc[:cut], fd_f, w.iloc[:cut], cfg.costs)
            te = bt(px_f.iloc[cut:], fd_f, w.iloc[cut:], cfg.costs)
            print(f"  lb={lb} top={tn}")
            print("    " + line("全段", full))
            print("    " + line("  训练", tr))
            print("    " + line("  测试", te))

    print()
    print("=" * 96)
    print("② 趋势跟踪 10/50 —— 4 折 walk-forward(每折只用之前数据定参数?这里是固定参数滚动样本外)")
    print("=" * 96)
    w = S.trend_following(px, fast=10, slow=50, rebalance=1)
    n = len(px)
    folds = 4
    for i in range(1, folds + 1):
        a, b = int(n * i / (folds + 1)), int(n * (i + 1) / (folds + 1))
        seg_px, seg_w = px.iloc[a:b], w.iloc[a:b]
        r = bt(seg_px, fd, seg_w, cfg.costs)
        print("  " + line(f"  折{i} {seg_px.index[0]:%y-%m}→{seg_px.index[-1]:%y-%m}", r))
    r_all = bt(px, fd, w, cfg.costs)
    print("  " + line("  全段", r_all))

    print()
    print("=" * 96)
    print("③ 成本敏感性(趋势 10/50,全段)")
    print("=" * 96)
    for label, c in [("taker+2bps滑点(基准)", Costs(4.5, 1.5, 2.0)),
                     ("taker+0滑点", Costs(4.5, 1.5, 0.0)),
                     ("maker+0滑点", Costs(4.5, 1.5, 0.0, use_maker=True)),
                     ("taker+5bps滑点(悲观)", Costs(4.5, 1.5, 5.0))]:
        r = bt(px, fd, w, c)
        print("  " + line(label, r))

    print()
    print("=" * 96)
    print("④ 趋势 10/50 月度收益")
    print("=" * 96)
    m = (1 + r_all.returns).resample("ME").prod() - 1
    s = "".join(f"{x*100:+6.1f}" for x in m.tail(12))
    print(f"  最近 12 个月(%): {s}")
    print(f"  月胜率 {(m > 0).mean()*100:.0f}% · 最好 {m.max()*100:+.1f}% · 最差 {m.min()*100:+.1f}%")


if __name__ == "__main__":
    main()
