#!/usr/bin/env python3
"""批量研究:12 个候选策略 × 3 标的,统一严格口径 + Bonferroni。

    python scripts/research_batch.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe.research.trade_stats import trade_stats
from qe.strict.data import load_1m, load_funding

COST = 11.0


def bars1h(sym):
    b = load_1m(sym)
    b = b.resample("1h").agg({"open": "first", "high": "max", "low": "min",
                              "close": "last", "volume": "sum"}).dropna()
    return b


def rsi(s, n):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan)).fillna(50)


def atr(b, n=20):
    tr = pd.concat([b["high"] - b["low"], (b["high"] - b["close"].shift()).abs(),
                    (b["low"] - b["close"].shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def state_w(n, enter, exit_):
    w = np.zeros(n)
    on = 0
    e = np.asarray(enter, dtype=bool)
    x = np.asarray(exit_, dtype=bool)
    for i in range(n):
        if on == 0 and e[i]:
            on = 1
        elif on != 0 and x[i]:
            on = 0
        w[i] = on
    return w


def rules(b, sym):
    c = b["close"]
    out = {}
    r2, r14 = rsi(c, 2), rsi(c, 14)
    ema20 = c.ewm(span=20, adjust=False).mean()
    sd20 = c.rolling(20).std()
    up, lo = ema20 + 2 * sd20, ema20 - 2 * sd20
    a = atr(b, 20)
    out["RSI(2)超卖反转"] = state_w(len(b), r2 < 10, r2 > 60)
    out["RSI(14)超卖反转"] = state_w(len(b), r14 < 30, r14 > 55)
    out["布林回归(反突破)"] = state_w(len(b), c < lo, c > ema20)
    out["布林突破(顺突破)"] = state_w(len(b), c > up, c < ema20)
    out["Keltner下轨反弹"] = state_w(len(b), c < ema20 - 2 * a, c > ema20)
    out["ATR通道突破"] = state_w(len(b), c > ema20 + 2 * a, c < ema20)
    hi20 = b["high"].rolling(20).max()
    lo10 = b["low"].rolling(10).min()
    out["Donchian突破"] = state_w(len(b), c > hi20.shift(1), c < lo10.shift(1))
    out["动量24h双向"] = np.sign(c.pct_change(24).fillna(0)).to_numpy()
    out["动量72h双向"] = np.sign(c.pct_change(72).fillna(0)).to_numpy()
    out["EMA9/21双向"] = np.sign(c.ewm(span=9).mean() - c.ewm(span=21).mean()).to_numpy()
    out["EMA50/200双向"] = np.sign(c.ewm(span=50).mean() - c.ewm(span=200).mean()).to_numpy()
    down3 = (c.diff() < 0).rolling(3).sum() == 3
    out["连跌3根反转"] = state_w(len(b), down3, pd.Series(np.arange(len(b)) % 2 == 0))
    try:
        f = load_funding(sym)
        fs = f.reindex(b.index, method="ffill")
        out["资金费极值反向"] = state_w(len(b), fs > 0.0003, fs < 0.0001)
    except Exception:
        pass
    return out


def main():
    rows = []
    for sym in ("BTC", "ETH", "SOL"):
        b = bars1h(sym)
        for name, w in rules(b, sym).items():
            st = trade_stats(b, pd.Series(w, index=b.index), COST)
            if not st:
                continue
            rows.append(dict(sym=sym, rule=name, n=st["n"], mean=st["mean"], t=st["t"],
                             ci=st["ci_lo"], hit=st["hit"], po=st["payoff"],
                             rec_n=st["rec_n"], rec=st["rec"], drop5=st["drop5"],
                             oos=st["oos"], val=st["val"], sh=st["sharpe_daily"]))
    df = pd.DataFrame(rows).sort_values("t", ascending=False)
    pd.set_option("display.width", 200, "display.max_rows", 60)
    print("\n=== 12 规则 × 3 标的(1h,taker 11bp,逐笔) ===")
    print(df.to_string(index=False, float_format=lambda x: f"{x:8.2f}"))
    ok = df[(df.t >= 2.0) & (df.ci > 0)]
    print(f"\n通过 G3(t≥2) + G4(CI>0) 的:{len(ok)} / {len(df)}")
    if len(ok):
        print(ok.to_string(index=False, float_format=lambda x: f"{x:8.2f}"))
    print(f"\nBonferroni:{len(df)} 组检验 → 阈值 t ≈ {3.3:.1f}(α=0.05/{len(df)})")
    hard = df[(df.t >= 3.3) & (df.ci > 0) & (df.rec > 0)]
    print(f"通过 Bonferroni + 近 1 年为正 的:{len(hard)} / {len(df)}")
    if len(hard):
        print(hard.to_string(index=False, float_format=lambda x: f"{x:8.2f}"))
    df.to_csv("/tmp/research_batch.csv", index=False)
    print("\n明细已存 /tmp/research_batch.csv")


if __name__ == "__main__":
    main()
