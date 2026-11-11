#!/usr/bin/env python3
"""用 crypto-quant-trade 的 Binance 1m 数据(2020-2026)重跑趋势策略。

目的:2 年 HL 样本 t=1.18 不显著,看 6 年样本能否给出确定性答案。
数据:data/binance/btc_1m_2020_2026.pkl.gz(他们的 builder 生成,raw pickle)
"""
from __future__ import annotations

import pickle
import sys
from math import erf, sqrt
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe.backtest.engine import Backtester
from qe.config import Costs
from qe.strategies import library as S
from qe.strategies.spec import get as get_spec

PKL = Path("/home/ubuntu/apps/crypto-quant-trade/data/binance/btc_1m_2020_2026.pkl.gz")


def load_daily() -> pd.DataFrame:
    with open(PKL, "rb") as f:
        m1 = pickle.load(f)
    if not isinstance(m1.index, pd.DatetimeIndex):
        m1.index = pd.to_datetime(m1.index, utc=True)
    if m1.index.tz is None:
        m1.index = m1.index.tz_localize("UTC")
    d = m1["close"].resample("1D").last().dropna()
    return pd.DataFrame({"BTC": d})


def stats(daily: pd.Series) -> dict:
    d = daily.dropna()
    sd = d.std(ddof=1)
    eq = (1 + d).cumprod()
    return {
        "n": len(d),
        "cagr": eq.iloc[-1] ** (365 / len(d)) - 1,
        "sharpe": d.mean() / sd * np.sqrt(365),
        "t": d.mean() / sd * np.sqrt(len(d)),
        "dd": (eq / eq.cummax() - 1).min(),
    }


def main():
    spec = get_spec("trend_btc_eth")
    px = load_daily()
    print("=" * 78)
    print(f"BTC 日线 {px.index[0].date()} → {px.index[-1].date()}  ({len(px)} 天)")
    print(f"[声明] 持仓 {spec.hold_horizon} | 杠杆 {spec.leverage:g}x")
    print("=" * 78)

    w = S.trend_following(px, fast=10, slow=50, vol_target=0.25)
    r = Backtester(px, Costs(taker_bps=4.5, maker_bps=4.5, slippage_bps=1.0),
                   initial=10000, max_leverage=spec.leverage).run(w)
    d = r.returns.dropna()
    st = stats(d)
    print(f"\n[基准 11bp] CAGR {st['cagr']*100:.2f}% | Sharpe {st['sharpe']:.2f} | t(天) {st['t']:.2f} "
          f"| n={st['n']} 天 | MaxDD {st['dd']*100:.1f}%")

    # 分年
    print("\n分年:")
    for y, g in (1 + d).groupby(d.index.year):
        e = g.prod() - 1
        print(f"   {y}: {e*100:+7.2f}%")

    # bootstrap
    rng = np.random.default_rng(42)
    arr, block = d.values, 5
    nb = len(arr) // block
    boots = []
    for _ in range(2000):
        idx = rng.integers(0, nb, nb)
        s = np.concatenate([arr[i * block:(i + 1) * block] for i in idx])
        sd = s.std(ddof=1)
        if sd:
            boots.append(s.mean() / sd * np.sqrt(365))
    lo, hi = np.percentile(boots, [2.5, 97.5])
    print(f"\n[S5] bootstrap 95% CI Sharpe: [{lo:.2f}, {hi:.2f}] → 跨 0? {'是 ❌' if lo < 0 < hi else '否 ✅'}")

    for k in (1, 5):
        s2 = stats(d.drop(d.nlargest(k).index))
        print(f"[S7] 剔除最好 {k} 天 → CAGR {s2['cagr']*100:6.2f}%  Sharpe {s2['sharpe']:5.2f}")

    # Bonferroni 9 格点
    def p1(t):
        return 0.5 * (1 - erf(t / sqrt(2)))

    print("\n[S6] 9 组格点 + Bonferroni(α=0.0056)")
    sig = 0
    for f in (5, 10, 20):
        row = []
        for s in (30, 50, 100):
            ww = S.trend_following(px, fast=f, slow=s, vol_target=0.25)
            rr = Backtester(px, Costs(taker_bps=4.5, maker_bps=4.5, slippage_bps=1.0),
                            initial=10000, max_leverage=spec.leverage).run(ww)
            stt = stats(rr.returns.dropna())
            pv = p1(stt["t"])
            sig += pv < 0.0056
            row.append(f"s={s}:{stt['cagr']*100:6.1f}%/t{stt['t']:5.2f}/p{pv:.3f}")
        print(f"   fast={f:2d}  " + "  ".join(row))
    print(f"   通过 Bonferroni:{sig}/9")


if __name__ == "__main__":
    main()
