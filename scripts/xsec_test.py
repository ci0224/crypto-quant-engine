#!/usr/bin/env python3
"""横截面策略:8 标的做多空分组,检验是否有独立 edge。

    python scripts/xsec_test.py

与深挂家族的区别:这是**横截面**(币种间相对强弱),不是同一标的的时间序列择时,
因此与现有 10 条策略的相关性应当很低 → 能真正增加"有效押注数"。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe.backtest.engine import Backtester
from qe.config import Costs
from qe.strict.data import load_1m

SYMS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "AVAX", "LINK"]
FILES = {"BTC": "btc", "ETH": "eth", "SOL": "sol", "BNB": "bnb", "XRP": "xrp",
         "DOGE": "doge", "AVAX": "avax", "LINK": "link"}
DATA = Path("/home/ubuntu/apps/crypto-quant-trade/data/binance")


def panel(freq="1D"):
    """逐币加载 1m 并立即降采样为日线后释放,避免 8×190MB 同时驻留(本机只有 3GB 内存)。"""
    import gc

    out = {}
    for s in SYMS:
        p = DATA / f"{FILES[s]}_1m_2020_2026.pkl.gz"
        if not p.exists():
            print(f"  跳过 {s}(无缓存)")
            continue
        b = load_1m(s)                      # 复用统一加载器(处理纯 pickle)
        out[s] = b["close"].resample(freq).last()
        del b
        gc.collect()
    return pd.DataFrame(out).dropna(how="all").ffill()


def xsec_weights(px, lookback, k=3, rebal=7, sign=1):
    ret = px.pct_change(lookback)
    w = pd.DataFrame(0.0, index=px.index, columns=px.columns)
    for i in range(len(px)):
        if i % rebal:
            continue
        r = ret.iloc[i].dropna()
        if len(r) < 2 * k:
            continue
        ranked = r.sort_values()
        longs = ranked.tail(k).index
        shorts = ranked.head(k).index
        w.iloc[i, [px.columns.get_loc(c) for c in longs]] = sign / k
        w.iloc[i, [px.columns.get_loc(c) for c in shorts]] = -sign / k
    return w.replace(0.0, np.nan).ffill().fillna(0.0)


def stats(px, w, label):
    bt = Backtester(px, Costs(), initial=10_000.0, max_leverage=1.0, funding=None)
    r = bt.run(w)
    eq = pd.Series(r.equity, index=px.index).dropna()
    if len(eq) < 100:
        return None
    ret = eq.pct_change().dropna()
    days = (eq.index[-1] - eq.index[0]).days or 1
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (365 / days) - 1
    sh = ret.mean() / (ret.std() or 1e-9) * np.sqrt(365)
    dd = (eq / eq.cummax() - 1).min()
    turn = w.diff().abs().sum(axis=1).mean() * 365
    return dict(label=label, cagr=cagr, sharpe=sh, dd=dd, turnover=turn)


def main() -> int:
    px = panel()
    print(f"面板:{px.shape[0]} 天 × {px.shape[1]} 币  {px.index[0].date()} → {px.index[-1].date()}\n")
    rows = []
    for name, lb, k, rb, sg in [
        ("动量 7d 多空", 7, 3, 7, 1),
        ("动量 14d 多空", 14, 3, 7, 1),
        ("动量 30d 多空", 30, 3, 7, 1),
        ("反转 1d 多空", 1, 3, 1, -1),
        ("反转 3d 多空", 3, 3, 3, -1),
        ("动量 7d 纯多", 7, 3, 7, 1),
    ]:
        w = xsec_weights(px, lb, k, rb, sg)
        if "纯多" in name:
            w = w.clip(lower=0)
        r = stats(px, w, name)
        if r:
            rows.append(r)
            print(f"  {r['label']:16s} CAGR={r['cagr']:+7.1%}  Sharpe={r['sharpe']:5.2f}  "
                  f"DD={r['dd']:+6.1%}  年换手={r['turnover']:.0f}x")
    pd.DataFrame(rows).to_csv("/tmp/xsec.csv", index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
