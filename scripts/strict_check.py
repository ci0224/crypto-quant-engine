#!/usr/bin/env python3
"""按 crypto-quant-trade 的 7 层标准检查「趋势策略」。

对照 research/09_回测严格审计清单.md:
  C1/C5 成本:11bp 往返(4.5×2 + 1×2);敏感性 0/3/11/20bp
  S2 Sharpe=mean/std×sqrt(365) ddof=1;S3 t 值按【天】算
  S5 5 日块 bootstrap 2000 次 95% CI
  S7 剔除最好 1 天 / 5 天
  S6 多重检验:参数格点 9 组 → Bonferroni
  A4 月度分解;K2 3x 下 MAE 与清算距离
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe.backtest.engine import Backtester
from qe.config import Config, Costs
from qe.data.panel import load_panel
from qe.strategies import library as S
from qe.strategies.spec import get as get_spec

FAST, SLOW, VOL = 10, 50, 0.25


def stats(daily: pd.Series) -> dict:
    d = daily.dropna()
    if len(d) < 20:
        return {}
    sd = d.std(ddof=1)
    sharpe = d.mean() / sd * np.sqrt(365) if sd else np.nan
    t = d.mean() / sd * np.sqrt(len(d)) if sd else np.nan
    eq = (1 + d).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    return {"n": len(d), "cagr": eq.iloc[-1] ** (365 / len(d)) - 1 if eq.iloc[-1] > 0 else -1,
            "sharpe": sharpe, "t": t, "dd": dd}


def main():
    cfg = Config.load()
    spec = get_spec("trend_btc_eth")
    lev = spec.leverage
    px, fd = load_panel("1d")
    coins = [c for c in ("BTC", "ETH") if c in px.columns]
    px = px[coins]
    w = S.trend_following(px, fast=FAST, slow=SLOW, vol_target=VOL)

    print("=" * 78)
    print(f"趋势策略 fast={FAST} slow={SLOW} vol={VOL}  样本 {px.index[0].date()} → {px.index[-1].date()}")
    # ---- 策略声明(09 §5 K1/K2/K6:声明前置)----
    # K1:每个标的的【同向持仓】持续时长(按符号变化切段),才反映真实持仓周期
    sgn = np.sign(w.fillna(0.0))
    runs = []
    for col in sgn.columns:
        v = sgn[col].values
        cur, prev = 0, 0
        for x in v:
            if x != 0 and x == prev:
                cur += 1
            elif x != 0:
                if cur:
                    runs.append(cur)
                cur, prev = 1, x
            else:
                if cur:
                    runs.append(cur)
                cur, prev = 0, 0
        if cur:
            runs.append(cur)
    hold_p50 = float(np.median(runs)) if runs else 0
    hold_p95 = float(np.percentile(runs, 95)) if runs else 0
    active = (w.abs().sum(axis=1) > 1e-12)
    print(f"[声明] 持仓 {spec.hold_horizon} | 杠杆上限 {spec.leverage:g}x")
    print(f"[K1] 同向持仓(根): p50={hold_p50:.0f} p95={hold_p95:.0f} max={max(runs) if runs else 0} | "
          f"有仓位天数 {active.mean()*100:.0f}% | 持仓段 {len(runs)} 段")

    print("=" * 78)

    # ---- C1/C5 成本敏感性(11bp = 他们的口径)----
    print("\n[C] 成本敏感性(往返 bp → CAGR / Sharpe / MaxDD / 年换手)")
    for bps in (0, 3, 11, 20, 40):
        r = Backtester(px, Costs(taker_bps=bps / 2, maker_bps=bps / 2, slippage_bps=0.0),
                       initial=10000, max_leverage=spec.leverage, funding=fd).run(w)
        st = stats(r.returns)
        tw = w.diff().abs().sum(axis=1)
        to = float(tw.sum() / (len(tw) / 365))
        print(f"   {bps:3d}bp → {st['cagr']*100:7.2f}%  Sharpe {st['sharpe']:5.2f}  DD {st['dd']*100:6.1f}%  年换手 {to:.2f}x")

    # ---- 基准:11bp ----
    r = Backtester(px, Costs(taker_bps=4.5, maker_bps=4.5, slippage_bps=1.0),
                   initial=10000, max_leverage=spec.leverage, funding=fd).run(w)
    d = r.returns.dropna()
    st = stats(d)
    print(f"\n[基准 11bp] CAGR {st['cagr']*100:.2f}% | Sharpe {st['sharpe']:.2f} | t(天) {st['t']:.2f} | n={st['n']} 天 | DD {st['dd']*100:.1f}%")

    # ---- S5 块 bootstrap ----
    rng = np.random.default_rng(42)
    arr = d.values
    block = 5
    n_blocks = len(arr) // block
    boots = []
    for _ in range(2000):
        idx = rng.integers(0, n_blocks, n_blocks)
        sample = np.concatenate([arr[i * block:(i + 1) * block] for i in idx])
        sd = sample.std(ddof=1)
        if sd:
            boots.append(sample.mean() / sd * np.sqrt(365))
    boots = np.array(boots)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    print(f"[S5] 5日块 bootstrap 95% CI Sharpe: [{lo:.2f}, {hi:.2f}] | 覆盖 0? {'是 ❌' if lo < 0 < hi else '否 ✅'}")

    # ---- S7 剔除极值 ----
    for k in (1, 5):
        dd = d.drop(d.nlargest(k).index)
        s2 = stats(dd)
        print(f"[S7] 剔除最好 {k} 天 → CAGR {s2['cagr']*100:6.2f}%  Sharpe {s2['sharpe']:5.2f}")

    # ---- A4 月度 ----
    m = (1 + d).resample("ME").prod() - 1
    pos = (m > 0).mean()
    print(f"[A4] 月度:共 {len(m)} 个月,正收益 {pos*100:.0f}%,最差 {m.min()*100:.1f}%,最好 {m.max()*100:.1f}%")
    print("     最近 6 个月:", " ".join(f"{x:+.1%}" for x in m.tail(6)))

    # ---- K2/K3:按【声明的】杠杆算最坏单日与清算距离 ----
    lev = spec.leverage
    worst_day = d.min()
    print(f"[K2] {lev:g}x 杠杆:最坏单日 {worst_day*100:.1f}% × {lev:g} = {worst_day*lev*100:.1f}% 权益;"
          f"距 -100% 清算还有 {abs(1/ (worst_day*lev)) if worst_day<0 else float('inf'):.1f} 个这样的日子")

    # ---- S6 多重检验:9 组参数格点 ----
    from math import erf, sqrt
    def p1(t):  # 单侧 p 值(H1: 均值>0),正态近似
        return 0.5 * (1 - erf(t / sqrt(2)))

    print("\n[S6] 参数格点 9 组 + Bonferroni(α=0.05/9≈0.0056,单侧 z≈2.54)")
    rows = []
    for f in (5, 10, 20):
        for s in (30, 50, 100):
            ww = S.trend_following(px, fast=f, slow=s, vol_target=VOL)
            rr = Backtester(px, Costs(taker_bps=4.5, maker_bps=4.5, slippage_bps=1.0),
                            initial=10000, max_leverage=spec.leverage, funding=fd).run(ww)
            stt = stats(rr.returns)
            rows.append((f, s, stt["cagr"], stt["sharpe"], stt["t"], p1(stt["t"])))
    for f, s, c, sh, t, pv in rows:
        flag = "✅" if pv < 0.0056 else "  "
        print(f"   fast={f:2d} slow={s:3d}  CAGR {c*100:7.2f}%  Sharpe {sh:5.2f}  t={t:5.2f}  p={pv:.4f} {flag}")
    sig = sum(1 for r in rows if r[5] < 0.0056)
    print(f"   通过 Bonferroni 的格点:{sig}/9;通过名义 0.05 的:{sum(1 for r in rows if r[5] < 0.05)}/9")


if __name__ == "__main__":
    main()
