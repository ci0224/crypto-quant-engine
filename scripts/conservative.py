#!/usr/bin/env python3
"""保守口径:把三处系统性放水补上,看 edge 还剩多少。

补的三项:
  ① 资金费:持仓 hold_min 分钟 × 小时费率(long 付、short 收)→ 这里按 long 全额付,保守
  ② 出场 taker 混合:11 号报告 §12 实测 51.4% 出场以 taker 成交 → 出场费从 1.5bp 变 3.04bp
  ③ 成交率:HL 实测 69% 复现 → 每个信号只有 69% 变成成交(影响笔数与年化,不影响单笔 bp)
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd

ROOT = "/home/ubuntu/apps/crypto-quant-trade"
sys.path.insert(0, f"{ROOT}/research")
import backtest_strategies as bs

bs.DATA = f"{ROOT}/data/binance/btc_1m_2020_2026.pkl.gz"
import backtest_maker as bm

FUND_BP_PER_HOUR = 1.25      # 保守:按 0.00125%/小时(HL 常态量级)
TAKER_SHARE = 0.514          # 出场以 taker 成交的比例(11 号 §12 实测)
EXTRA_EXIT_BP = TAKER_SHARE * (4.5 - 1.5)   # = 1.542bp
FILL_RATE = 0.69             # HL 实测成交复现率

SYMS = [("BTC", "btc_1m_2020_2026.pkl.gz", 3.0),
        ("ETH", "eth_1m_2020_2026.pkl.gz", 3.0),
        ("SOL", "sol_1m_2020_2026.pkl.gz", 1.5)]


def run(sym, pkl, lev):
    m1, bars = bs.load_bars(f"{ROOT}/data/binance/{pkl}")
    bm.o, bm.h, bm.l, bm.c, bm.t = (m1.open.values, m1.high.values, m1.low.values,
                                    m1.close.values, m1.index)
    bm.close, bm.N = bars.close.values, len(m1)
    bm.pos = np.searchsorted(bm.t.values, (bars.index + pd.Timedelta(minutes=5)).values, side="left")
    bm.rsi = bs.s_rsi(bars).fillna(0).values
    bm.boll = bs.s_bollinger(bars).fillna(0).values
    bm.kelt = bs.s_keltner(bars).fillna(0).values
    bm.SIGS = {"Boll<low": (bm.boll > 0, +1)}
    hs = bars.index.hour.values
    tr = bm.run(bm.SIGS["Boll<low"][0] & (hs >= 8) & (hs < 20), +1, 100, 120)
    days = (bars.index[-1] - bars.index[0]).days
    return tr, days


def stats(tr, days, lev):
    bp = np.array([q["pnl_bp"] for q in tr])
    hold = np.array([q["hold_min"] for q in tr])
    fund = hold / 60.0 * FUND_BP_PER_HOUR
    net = bp - fund - EXTRA_EXIT_BP
    rets = net / 1e4 * lev
    # 成交率:只保留 69% 的信号(按比例缩放笔数,单笔 bp 不变)
    n_eff = len(net) * FILL_RATE
    per_year_eff = n_eff / days * 365
    gross = float(np.prod(1 + rets) ** (FILL_RATE) - 1)   # 近似:成交率线性折算到复利
    ann_net = float((1 + gross) ** (365 / days) - 1)
    ann_raw = float(np.prod(1 + (bp / 1e4 * lev)) ** (365 / days) - 1)
    return dict(n=len(net), mean_bp=float(bp.mean()), net_bp=float(net.mean()),
                fund_bp=float(fund.mean()), tpy=per_year_eff, ann_raw=ann_raw, ann_net=ann_net)


def main():
    print(f"假设:资金费 {FUND_BP_PER_HOUR}bp/小时 · 出场 taker 混合 +{EXTRA_EXIT_BP:.2f}bp · 成交率 {FILL_RATE:.0%}")
    print(f"{'标的':<6}{'笔数':>6}{'回测均值':>10}{'资金费':>9}{'保守均值':>10}{'笔/年(有效)':>12}{'回测年化':>10}{'保守年化':>10}")
    for sym, pkl, lev in SYMS:
        tr, days = run(sym, pkl, lev)
        s = stats(tr, days, lev)
        print(f"{sym:<6}{s['n']:>6}{s['mean_bp']:>9.2f}bp{-s['fund_bp']:>8.2f}bp"
              f"{s['net_bp']:>9.2f}bp{s['tpy']:>11.1f}{s['ann_raw']*100:>9.1f}%{s['ann_net']*100:>9.1f}%")


if __name__ == "__main__":
    main()
