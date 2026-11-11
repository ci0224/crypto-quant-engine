#!/usr/bin/env python3
"""深挂 dip-buy 规则的多标复刻与扩展。

复用 crypto-quant-trade 的**已验证引擎**(backtest_maker.run),不改规则:
  Boll<low(5m 跌破下轨) → 收盘 ×(1−100bp) maker 挂单(5 分钟有效)
  → 入场 ×(1+120bp) 止盈(55 分钟有效)→ 超时腿继续等,>240 分钟记 stuck
  时间窗 08–20 UTC;成本 maker 1.5bp/边 = 3bp 往返

判定:2020-2022 为预注册样本外(你的 11 号报告用过的切分),2023-2026 为验证段。
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

ROOT = "/home/ubuntu/apps/crypto-quant-trade"
sys.path.insert(0, ROOT + "/research")

import backtest_strategies as bs  # noqa: E402

# backtest_maker 在 import 时就会加载默认数据(Deribit json,本机没有)
# → 先把 DATA 指向 BTC 缓存,导入成功后再逐标的 rebind
bs.DATA = f"{ROOT}/data/binance/btc_1m_2020_2026.pkl.gz"
import backtest_maker as bm  # noqa: E402

FILES = {
    "BTC": f"{ROOT}/data/binance/btc_1m_2020_2026.pkl.gz",
    "ETH": f"{ROOT}/data/binance/eth_1m_2020_2026.pkl.gz",
    "SOL": f"{ROOT}/data/binance/sol_1m_2020_2026.pkl.gz",
}
X_BP, TGT_BP = 100, 120
CUT = pd.Timestamp("2023-01-01", tz="UTC")


def setup(pkl: str):
    m1, bars = bs.load_bars(pkl)
    bm.o, bm.h, bm.l, bm.c = (m1.open.values, m1.high.values, m1.low.values, m1.close.values)
    bm.t, bm.close, bm.N1 = m1.index, bars.close.values, len(m1)
    bm.pos = np.searchsorted(bm.t.values,
                             (bars.index + pd.Timedelta(minutes=5)).values, side="left")
    bm.boll = bs.s_bollinger(bars).fillna(0).values
    bm.rsi = bs.s_rsi(bars).fillna(0).values
    bm.kelt = bs.s_keltner(bars).fillna(0).values
    bm.SIGS = {"Boll<low": (bm.boll > 0, +1)}
    return m1, bars


def stats(tr, bars, days):
    if not tr:
        return None
    p = np.array([q["pnl_bp"] for q in tr])
    idx = pd.to_datetime([bm.t[q["xi"]] for q in tr], utc=True)
    day = pd.Series(p, index=idx.normalize()).groupby(level=0).sum()
    s = day.reindex(days, fill_value=0.0)
    sd = s.std(ddof=1)
    hold = np.array([q["hold_min"] for q in tr])
    return dict(
        n=len(p), mean=p.mean(), hit=(p > 0).mean(), worst=p.min(),
        sharpe=s.mean() / sd * np.sqrt(365), t=s.mean() / sd * np.sqrt(len(s)),
        mae=max(q["mae_bp"] for q in tr), maxhold=hold.max(),
        over1h=(hold > 60).mean(), stuck=np.mean([q["reason"] == "stuck" for q in tr]),
        p=p, idx=idx, s=s,
    )


def boot_ci(p, idx, days, L=5, n=2000, seed=42):
    df = pd.DataFrame({"p": p, "d": idx.normalize()})
    byd = {d: g["p"].to_numpy() for d, g in df.groupby("d")}
    rng = np.random.default_rng(seed)
    starts = np.arange(len(days) - L + 1)
    out = []
    for _ in range(n):
        vals = []
        for st in rng.choice(starts, size=int(np.ceil(len(days) / L)), replace=True):
            for k in range(L):
                vals.extend(byd.get(days[st + k], []))
        v = np.array(vals)
        if len(v) > 5:
            out.append(v.mean())
    lo, hi = np.percentile(out, [2.5, 97.5])
    return lo, hi


def main():
    print(f"规则:Boll<low, x{X_BP}bp maker, 目标 +{TGT_BP}bp, 08-20 UTC, 成本 3bp 往返\n")
    rows = []
    for sym, pkl in FILES.items():
        m1, bars = setup(pkl)
        days = pd.date_range(bars.index[0].normalize(), bars.index[-1].normalize(), freq="D")
        hs = bars.index.hour.values
        mask = bm.SIGS["Boll<low"][0] & (hs >= 8) & (hs < 20)
        tr = bm.run(mask, +1, X_BP, TGT_BP)
        st = stats(tr, bars, days)
        if not st:
            print(f"{sym}: 无交易")
            continue
        lo, hi = boot_ci(st["p"], st["idx"], days)
        # 分段
        def seg(a, b):
            sel = [q for q in tr if a <= bm.t[q["xi"]].tz_convert("UTC") < b]
            return (np.mean([q["pnl_bp"] for q in sel]), len(sel)) if sel else (float("nan"), 0)

        m22 = seg(bars.index[0], CUT)
        m26 = seg(CUT, bars.index[-1] + pd.Timedelta(days=1))
        print(f"{'='*78}\n{sym}  样本 {bars.index[0].date()} → {bars.index[-1].date()}")
        print(f"  笔数 {st['n']:4d} | 均值 {st['mean']:+7.2f}bp | 命中 {st['hit']*100:4.1f}% | "
              f"最差 {st['worst']:7.1f}bp")
        print(f"  Sharpe(日) {st['sharpe']:5.2f} | t(日) {st['t']:5.2f} | "
              f"bootstrap 95% CI 均值 [{lo:+.2f}, {hi:+.2f}] bp {'❌跨0' if lo < 0 < hi else '✅不跨0'}")
        print(f"  2020-2022: {m22[0]:+7.2f}bp (n={m22[1]})   2023-2026: {m26[0]:+7.2f}bp (n={m26[1]})")
        print(f"  MAE 最大 {st['mae']:.0f}bp | 最长持仓 {st['maxhold']:.0f}分 | >60分 {st['over1h']*100:.1f}% "
              f"| stuck {st['stuck']*100:.1f}%")
        # 3x 清算距离:逐仓 3x → 保证金 1/3,价格逆行 33.3% 强平;看 MAE
        print(f"  3x 下强平需逆行 3333bp,实测最大 MAE {st['mae']:.0f}bp → "
              f"{'✅ 安全' if st['mae'] < 3000 else '❌ 有爆仓风险'}")
        rows.append((sym, st["n"], st["mean"], st["t"], lo, hi, st["hit"], m22[0], m26[0]))

    print(f"\n{'='*78}\n汇总")
    print(f"{'标的':<5}{'笔数':>6}{'均值bp':>9}{'t(日)':>7}{'CI下':>8}{'CI上':>8}{'命中':>7}{'20-22':>9}{'23-26':>9}")
    for r in rows:
        print(f"{r[0]:<5}{r[1]:>6}{r[2]:>9.2f}{r[3]:>7.2f}{r[4]:>8.2f}{r[5]:>8.2f}"
              f"{r[6]*100:>6.1f}%{r[7]:>9.2f}{r[8]:>9.2f}")


if __name__ == "__main__":
    main()
