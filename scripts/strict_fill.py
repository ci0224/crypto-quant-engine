#!/usr/bin/env python3
"""严格成交假设下的对比:碰到就成交 vs 需穿过 N bp。"""
import sys
import numpy as np
import pandas as pd

ROOT = "/home/ubuntu/apps/crypto-quant-trade"
sys.path.insert(0, f"{ROOT}/research")
import backtest_strategies as bs

bs.DATA = f"{ROOT}/data/binance/btc_1m_2020_2026.pkl.gz"
import backtest_maker as bm

m1, bars = bs.load_bars(bs.DATA)
bm.o, bm.h, bm.l, bm.c, bm.t = (m1.open.values, m1.high.values, m1.low.values,
                                m1.close.values, m1.index)
bm.close = bars.close.values
bm.N = len(m1)
bm.pos = np.searchsorted(bm.t.values, (bars.index + pd.Timedelta(minutes=5)).values, side="left")
bm.rsi = bs.s_rsi(bars).fillna(0).values
bm.boll = bs.s_bollinger(bars).fillna(0).values
bm.kelt = bs.s_keltner(bars).fillna(0).values
bm.SIGS = {"Boll<low": (bm.boll > 0, +1)}

src = open(f"{ROOT}/research/explore_maker_deep.py", encoding="utf-8").read()
fn = src[src.index("def strict_run"):src.index("    return trades") + len("    return trades")]
from types import SimpleNamespace
ns = {"np": np, "f": bm, "bmm": SimpleNamespace(FEE=bm.FEE_RT_BP)}
exec(fn, ns)
strict_run = ns["strict_run"]

hs = bars.index.hour.values
mask = bm.SIGS["Boll<low"][0] & (hs >= 8) & (hs < 20)

print(f"{'成交假设':<24}{'笔数':>6}{'均值':>10}{'命中':>7}{'相对宽松':>9}")
base = bm.run(mask, +1, 100, 120)
bm_mean = float(np.mean([q["pnl_bp"] for q in base]))
print(f"{'碰到挂单价即成交':<22}{len(base):>6}{bm_mean:>9.2f}bp{np.mean([q['pnl_bp']>0 for q in base])*100:>6.1f}%{'—':>9}")
for extra in (1, 2, 3, 5, 10):
    t = strict_run(mask, +1, 100, 120, extra)
    if not t:
        print(f"{'需穿过 +%dbp' % extra:<22}{0:>6}{'—':>10}")
        continue
    mu = float(np.mean([q["pnl_bp"] for q in t]))
    print(f"{'需穿过 +%dbp' % extra:<22}{len(t):>6}{mu:>9.2f}bp{np.mean([q['pnl_bp']>0 for q in t])*100:>6.1f}%{(mu/bm_mean-1)*100:>8.0f}%")

# 三项现实折扣叠加(以宽松版均值为基准)
print("\n=== 现实折扣叠加(按每笔均值 27.29bp)===")
for fund in (1, 2, 5):
    for taker_mix in (0.0, 1.5, 3.0):
        net = 27.29 - fund - taker_mix
        print(f"  资金费 -{fund}bp + 出场含 taker -{taker_mix}bp → {net:.2f}bp({net/27.29*100:.0f}% 保留)")
