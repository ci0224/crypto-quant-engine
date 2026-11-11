#!/usr/bin/env python3
"""成交率敏感性:同一策略在不同 fill_rate 下的笔数/均值/年化/t 值。

用途:在没有真实成交数据时,给出"最坏情况"的边界,而不是拍一个数字。
依据:crypto-quant-trade §15 的 HL 重放实测 69% 成交复现率。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe.strict.data import boll_state, load_1m, load_funding, resample
from qe.strict.engine import StrictCfg, run

RATES = (1.00, 0.85, 0.69, 0.50)


def main(symbol: str = "BTC") -> int:
    b1 = load_1m(symbol)
    b5 = resample(b1, "5")
    st = boll_state(b5["close"])
    sig = (st > 0).to_numpy(dtype=bool) & (b5.index.hour >= 8) & (b5.index.hour < 20)
    fd = load_funding(symbol)
    yrs = (b5.index[-1] - b5.index[0]).days / 365.25

    print(f"{symbol} 成交率敏感性(严格口径:through 2bp + 真实资金费 + taker 混合)")
    print(f"{'fill_rate':>10s} {'笔数':>7s} {'均值bp':>9s} {'年化(1x)':>10s} {'t 值':>7s}")
    for fr in RATES:
        cfg = StrictCfg(through_bp=2.0, fill_rate=fr, taker_exit_prob=0.514)
        res = run(b1, b5, sig, fd, cfg)
        td = pd.DataFrame(res["trades"])
        if td.empty:
            print(f"{fr:>10.2f} {0:>7d} {'—':>9s} {'—':>10s} {'—':>7s}")
            continue
        n = len(td)
        mean = float(td["net_bp"].mean())
        day = td.groupby(pd.to_datetime(td["exit_t"]).dt.normalize())["net_bp"].sum()
        t = float(day.mean() / day.std(ddof=1) * np.sqrt(len(day))) if len(day) > 2 else float("nan")
        print(f"{fr:>10.2f} {n:>7d} {mean:>9.2f} {n / yrs * mean / 100:>9.1f}% {t:>7.2f}")
    print("\n参考:100% = 纸交易假设;69% = 原仓库 §15 HL 实测;50% = 极悲观")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "BTC"))
