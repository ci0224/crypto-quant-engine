#!/usr/bin/env python3
"""按时间窗口(3m / 1y / 3y / 全样本)统计深挂策略的表现。

口径:
  · 每笔收益 = pnl_bp × 杠杆 / 10000(复利连乘)
  · 年化 = (期末/期初)^(365/窗口天数) - 1
  · 回撤 = 权益曲线最大回撤;Sharpe 用日聚合
  · 成本已含(maker 3bp 往返)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/home/ubuntu/apps/crypto-quant-trade")
sys.path.insert(0, str(ROOT / "research"))
import backtest_strategies as bs  # noqa: E402

bs.DATA = str(ROOT / "data/binance/btc_1m_2020_2026.pkl.gz")
import backtest_maker as bm  # noqa: E402

MIN_N = 20   # 年化的最少笔数
SYMS = [("BTC", "btc_1m_2020_2026.pkl.gz", 3.0),
        ("ETH", "eth_1m_2020_2026.pkl.gz", 3.0),
        ("SOL", "sol_1m_2020_2026.pkl.gz", 1.5)]


def setup(pkl):
    m1, bars = bs.load_bars(str(ROOT / "data/binance" / pkl))
    bm.o, bm.h, bm.l, bm.c, bm.t = (m1.open.values, m1.high.values, m1.low.values,
                                    m1.close.values, m1.index)
    bm.close = bars.close.values
    bm.pos = np.searchsorted(bm.t.values, (bars.index + pd.Timedelta(minutes=5)).values, side="left")
    bm.rsi = bs.s_rsi(bars).fillna(0).values
    bm.boll = bs.s_bollinger(bars).fillna(0).values
    bm.kelt = bs.s_keltner(bars).fillna(0).values
    bm.SIGS = {"Boll<low": (bm.boll > 0, +1)}
    return bars


def run(sym, pkl, lev):
    bars = setup(pkl)
    hs = bars.index.hour.values
    t = bars.index
    # 与 11 号报告头条配置一致:08-20 UTC 窗口
    mask = bm.SIGS["Boll<low"][0] & (hs >= 8) & (hs < 20)
    tr = bm.run(mask, +1, 100, 120)
    rows = []
    for q in tr:
        rows.append({"ts": pd.Timestamp(bm.t[q["xi"]]).tz_localize("UTC") if pd.Timestamp(bm.t[q["xi"]]).tz is None else pd.Timestamp(bm.t[q["xi"]]),
                     "bp": q["pnl_bp"]})
    df = pd.DataFrame(rows).sort_values("ts")
    df["ret"] = df["bp"] / 10000.0 * lev
    df["eq"] = (1 + df["ret"]).cumprod()
    return df


def window_stats(df, days, end):
    if days is None:
        sub = df
        span = (df["ts"].iloc[-1] - df["ts"].iloc[0]).days
    else:
        start = end - pd.Timedelta(days=days)
        sub = df[df["ts"] >= start]
        span = days
    if len(sub) < 3:
        return None
    eq = (1 + sub["ret"]).cumprod()
    total = eq.iloc[-1] - 1
    ann = (1 + total) ** (365.0 / max(span, 1)) - 1 if span > 0 else float("nan")
    dd = float((eq / eq.cummax() - 1).min())
    daily = sub.set_index("ts")["ret"].resample("1D").sum().dropna()
    sharpe = float(daily.mean() / daily.std(ddof=1) * np.sqrt(365)) if len(daily) > 2 and daily.std(ddof=1) > 0 else float("nan")
    se = float(sub["bp"].std(ddof=1) / np.sqrt(len(sub))) if len(sub) > 1 else float("nan")
    ci = (float(sub["bp"].mean() - 1.96 * se), float(sub["bp"].mean() + 1.96 * se))
    # 少于 20 笔不年化:3 个月 5 笔年化 = 把噪音 ^4,毫无意义
    return dict(n=len(sub), mean=sub["bp"].mean(), hit=(sub["bp"] > 0).mean(),
                total=total, ann=ann if len(sub) >= MIN_N else None,
                dd=dd, sharpe=sharpe, se=se, ci=ci)


def main():
    end = pd.Timestamp.utcnow().tz_localize("UTC") if pd.Timestamp.utcnow().tz is None else pd.Timestamp.utcnow()
    for sym, pkl, lev in SYMS:
        df = run(sym, pkl, lev)
        end_s = df["ts"].iloc[-1]
        print(f"\n=== {sym} ({lev:g}x) · 共 {len(df)} 笔 · {df['ts'].iloc[0].date()} → {end_s.date()} ===")
        print(f"{'窗口':<8}{'笔数':>6}{'均值':>9}{'均值95%CI':>20}{'胜率':>7}{'区间收益':>10}{'年化':>9}{'回撤':>9}{'Sharpe':>8}")
        for label, days in [("3 个月", 91), ("1 年", 365), ("3 年", 1095), ("全样本", None)]:
            st = window_stats(df, days, end_s)
            if not st:
                print(f"{label:<8}{'样本不足':>8}")
                continue
            ci = f"[{st['ci'][0]:.0f}, {st['ci'][1]:.0f}]bp"
            ann = f"{st['ann']*100:>7.1f}%" if st["ann"] is not None else "  样本不足"
            mark = "" if st["ann"] is not None else "  ← 不年化"
            print(f"{label:<8}{st['n']:>6}{st['mean']:>8.1f}bp{ci:>20}{st['hit']*100:>6.0f}%"
                  f"{st['total']*100:>9.1f}%{ann:>9}{st['dd']*100:>8.1f}%{st['sharpe']:>8.2f}{mark}")


if __name__ == "__main__":
    main()
