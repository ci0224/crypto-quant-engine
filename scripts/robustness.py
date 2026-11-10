#!/usr/bin/env python3
"""稳健性检查:参数平台(neighborhood)+ 留一法。

一个好策略应该在**相邻参数**上都表现不错(平台),而不是单点最优(尖峰)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe.backtest.engine import Backtester  # noqa: E402
from qe.config import Config  # noqa: E402
from qe.data.panel import load_panel  # noqa: E402
from qe.strategies import library as S  # noqa: E402


def perf(px, fd, w, cfg):
    r = Backtester(px, costs=cfg.costs, funding=fd, initial=10_000, max_leverage=2.0).run(w)
    s = r.stats
    return s["cagr"], s["sharpe"], s["max_dd"], r


def carry_threshold(px: pd.DataFrame, fd: pd.DataFrame, lb: int, hi_q: float, lo_q: float,
                    rebalance: int, vol_target: float) -> pd.DataFrame:
    """阈值型资金费套利:只做资金费分位极端的两端(不强制定仓)。

    hi_q: 做空的分位阈值(如 0.8 = 最高 20%)
    lo_q: 做多的分位阈值(如 0.2 = 最低 20%)
    """
    f = fd.copy()
    f.index = pd.to_datetime(f.index, utc=True)
    f_daily = f.resample("1D").sum().reindex(px.index, method="ffill")
    f_mean = f_daily.rolling(lb).mean()
    w = pd.DataFrame(0.0, index=px.index, columns=px.columns)
    for i, dt in enumerate(px.index):
        if i < lb + 1 or i % rebalance:
            continue
        s = f_mean.loc[dt].dropna()
        if len(s) < 6:
            continue
        hi, lo = s.quantile(hi_q), s.quantile(lo_q)
        shorts = s[s >= hi]
        longs = s[s <= lo]
        if len(shorts):
            w.loc[dt, shorts.index] = -0.5 / len(shorts)
        if len(longs):
            w.loc[dt, longs.index] = 0.5 / len(longs)
        # 美元中性:两端数量不等时按较小一端缩放
        if len(shorts) and len(longs):
            k = min(len(shorts), len(longs))
            w.loc[dt, :] = w.loc[dt, :] * (k / max(len(shorts), len(longs)))
    w = w.replace(0.0, np.nan).ffill().fillna(0.0)
    return S.vol_target_scale(w, px, vol_target)


def main():
    cfg = Config.load()
    px, fd = load_panel("1d")
    start = fd[fd.abs().sum(axis=1) > 0].index[0]
    px, fd = px[px.index >= start], fd[fd.index >= start]
    print(f"面板 {px.shape[1]} 币 · {len(px)} 天 · {px.index[0]:%Y-%m-%d} → {px.index[-1]:%Y-%m-%d}\n")

    print("=" * 100)
    print("① 阈值型资金费套利 —— 参数平台(每格 CAGR / Sharpe)")
    print("=" * 100)
    print(f"{'lb':>4s} " + "".join(f"{f'hi={h}':>16s}" for h in (0.7, 0.8, 0.9)))
    best = []
    for lb in (7, 14, 21):
        row = f"{lb:>4d} "
        for hi in (0.7, 0.8, 0.9):
            w = carry_threshold(px, fd, lb, hi, 1 - hi, 7, 0.15)
            cagr, sh, dd, r = perf(px, fd, w, cfg)
            row += f"  {cagr*100:+7.1f}%/{sh:4.2f}"
            best.append((cagr, sh, dd, lb, hi, w))
        print(row)
    best.sort(key=lambda x: -x[1])
    cagr, sh, dd, lb, hi, w_best = best[0]
    print(f"\n最优:lb={lb} hi={hi} → CAGR {cagr*100:+.1f}% Sharpe {sh:.2f} DD {dd*100:.1f}%")

    print("\n  留一法(最优参数):")
    vals = []
    for c in px.columns:
        cols = [x for x in px.columns if x != c]
        w = carry_threshold(px[cols], fd[cols], lb, hi, 1 - hi, 7, 0.15)
        cc, ss, _, _ = perf(px[cols], fd[cols], w, cfg)
        vals.append(cc)
        print(f"    剔除 {c:6s} → CAGR {cc*100:+7.1f}%")
    print(f"  留一法 CAGR 区间: {min(vals)*100:+.1f}% ~ {max(vals)*100:+.1f}% "
          f"(标准差 {np.std(vals)*100:.1f}%)")

    print()
    print("=" * 100)
    print("② BTC+ETH 趋势 —— 参数平台")
    print("=" * 100)
    sub = px[["BTC", "ETH"]]
    sub_f = fd[["BTC", "ETH"]]
    header = "fast/slow"
    print(f"{header:>10s}" + "".join(f"{s:>16d}" for s in (30, 50, 100)))
    for fast in (5, 10, 20):
        row = f"{fast:>10d}"
        for slow in (30, 50, 100):
            if fast >= slow:
                row += f"{'—':>16s}"
                continue
            w = S.trend_following(sub, fast=fast, slow=slow, rebalance=1, vol_target=0.25)
            cagr, sh, dd, _ = perf(sub, sub_f, w, cfg)
            row += f"  {cagr*100:+6.1f}%/{sh:4.2f}"
        print(row)


if __name__ == "__main__":
    main()
