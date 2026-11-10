#!/usr/bin/env python3
"""找"第二个策略":在同一份数据上测试与资金费套利**逻辑不同**的候选,要求训练/测试都为正。"""
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


def run(px, fd, w, cfg):
    return Backtester(px, costs=cfg.costs, funding=fd, initial=cfg.initial_equity, max_leverage=2.0).run(w)


def show(tag, px, fd, w, cfg, split=0.6):
    full, cut = run(px, fd, w, cfg), int(len(px) * split)
    tr = run(px.iloc[:cut], fd, w.iloc[:cut], cfg)
    te = run(px.iloc[cut:], fd, w.iloc[cut:], cfg)
    ok = "✅" if (tr.stats["cagr"] > 0 and te.stats["cagr"] > 0) else "❌"
    print(f"  {ok} {tag:34s} 全段 CAGR {full.stats['cagr']*100:+6.1f}% Sh {full.stats['sharpe']:4.2f} | "
          f"训练 {tr.stats['cagr']*100:+6.1f}% Sh {tr.stats['sharpe']:4.2f} | "
          f"测试 {te.stats['cagr']*100:+6.1f}% Sh {te.stats['sharpe']:4.2f} | DD {full.stats['max_dd']*100:5.1f}%")
    return ok == "✅"


def main():
    cfg = Config.load()
    px, fd = load_panel("1d")

    # 只在有资金费的窗口测(否则资金费相关策略无意义)
    start = fd[fd.abs().sum(axis=1) > 0].index[0] if not fd.empty else px.index[0]
    pxf, fdf = px[px.index >= start], fd[fd.index >= start]
    print(f"数据窗口: {pxf.index[0]:%Y-%m-%d} → {pxf.index[-1]:%Y-%m-%d} · {pxf.shape[1]} 币\n")

    print("─" * 120)
    print("候选 1:1h 短线反转(日线之外的时间框架)")
    print("─" * 120)
    pxh, fdh = load_panel("1h")
    for lb in (2, 4, 8, 24):
        w = S.xsec_reversal(pxh, lookback=lb, top_n=5, rebalance=1, vol_target=0.20)
        show(f"1h 反转 lb={lb}根", pxh, fdh, w, cfg)

    print("\n" + "─" * 120)
    print("候选 2:1h 动量")
    print("─" * 120)
    for lb in (12, 24, 72):
        w = S.xsec_momentum(pxh, lookback=lb, top_n=5, rebalance=6, vol_target=0.20)
        show(f"1h 动量 lb={lb}根", pxh, fdh, w, cfg)

    print("\n" + "─" * 120)
    print("候选 3:集中趋势(只做 BTC/ETH,波动目标 25%)")
    print("─" * 120)
    for coins in (["BTC", "ETH"], ["BTC"]):
        sub = pxf[[c for c in coins if c in pxf.columns]]
        w = S.trend_following(sub, fast=10, slow=50, rebalance=1, vol_target=0.25)
        show(f"趋势 {'+'.join(coins)}", sub, fdf, w, cfg)

    print("\n" + "─" * 120)
    print("候选 4:趋势 + 资金费过滤(只在资金费非极端时顺势)")
    print("─" * 120)
    f_daily = fdf.resample("1D").sum().reindex(pxf.index, method="ffill")
    f_rank = f_daily.rank(axis=1, pct=True)          # 0~1 分位
    for hi in (0.9, 0.8):
        base = S.trend_following(pxf, fast=10, slow=50, rebalance=1, vol_target=0.25)
        # 资金费分位 > hi(拥挤多头)时,把多仓减半
        filt = (f_rank > hi).astype(float)
        w = base * (1 - 0.5 * filt)
        show(f"趋势+资金费过滤 hi={hi}", pxf, fdf, w, cfg)

    print("\n" + "─" * 120)
    print("候选 5:资金费套利变体(低波动 12%,更保守)")
    print("─" * 120)
    for vt in (0.12, 0.18, 0.35):
        w = S.funding_carry(pxf, fdf, lookback_days=7, top_n=5, rebalance=7, vol_target=vt)
        show(f"资金费套利 vol目标={vt}", pxf, fdf, w, cfg)


if __name__ == "__main__":
    main()
