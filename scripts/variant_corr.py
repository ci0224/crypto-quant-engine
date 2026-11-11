#!/usr/bin/env python3
"""8 个通过的 maker 变体:相关性矩阵 + 有效押注数 + 组合效果。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe.strict.data import load_1m, load_funding, resample
from qe.strict.engine import StrictCfg, run
from scripts.maker_sweep import signals

VARIANTS = {
    "BTC": ["boll20", "rsi14<30", "keltner"],
    "SOL": ["boll20", "rsi14<30", "keltner", "vwap-2sd", "boll40x2.5"],
}


def main():
    cfg = StrictCfg(x_bp=100.0, tgt_bp=120.0, through_bp=2.0, fill_rate=0.85, taker_exit_prob=0.514)
    daily = {}
    for sym, names in VARIANTS.items():
        b1 = load_1m(sym)
        b5 = resample(b1, "5")
        f = load_funding(sym)
        sigs = signals(b5)
        for n in names:
            res = run(b1, b5, pd.Series(sigs[n], index=b5.index), f, cfg)
            tr = res["trades"]
            if len(tr) == 0:
                continue
            ts = pd.DatetimeIndex(pd.to_datetime(tr["t"], utc=True))
            daily[f"{sym}_{n}"] = pd.Series(tr["net_bp"].to_numpy(float),
                                            index=ts.normalize()).groupby(level=0).sum()
    df = pd.DataFrame(daily).fillna(0.0)
    print(f"日 PnL 矩阵: {df.shape[0]} 天 × {df.shape[1]} 个变体\n")
    corr = df.corr()
    pd.set_option("display.width", 220, "display.max_columns", 20)
    print("=== 相关性矩阵 ===")
    print(corr.round(2).to_string())
    # 有效押注数(特征值法)
    ev = np.linalg.eigvalsh(corr.to_numpy())
    n_eff = (ev.sum() ** 2) / (ev ** 2).sum()
    print(f"\n有效押注数(特征值法)= {n_eff:.2f} / {len(corr)}")
    avg = corr.to_numpy()[np.triu_indices(len(corr), 1)].mean()
    print(f"平均两两相关 = {avg:.2f}")
    # 等权组合
    port = df.mean(axis=1)
    sh = port.mean() / port.std() * np.sqrt(365)
    best = max(df[c].mean() / df[c].std() * np.sqrt(365) for c in df.columns)
    print(f"\n等权组合 Sharpe = {sh:.2f} | 单个最好 Sharpe = {best:.2f} | 差异 {(sh/best-1)*100:+.1f}%")
    # 子集:每标的只留最好的一个
    sub = df[["BTC_keltner", "SOL_keltner"]].mean(axis=1)
    print(f"BTC_keltner + SOL_keltner 组合 Sharpe = {sub.mean()/sub.std()*np.sqrt(365):.2f}")
    df.to_csv("/tmp/variant_daily.csv")


if __name__ == "__main__":
    main()
