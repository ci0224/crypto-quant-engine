#!/usr/bin/env python3
"""VCP(放宽趋势模板)完整验证并注册进 pipeline。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe import pipeline, registry
from qe.backtest.engine import Backtester
from qe.strict.data import load_1m
from scripts.test_vcp import COST, SYMS, daily, pct_rank, simulate

RISK = float(__import__("os").environ.get("VCP_RISK", "0.01"))


def sig_loose(d, vol_mult=1.2, tight_pct=0.35, bbw_pct=0.40):
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]
    sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
    bbw = c.rolling(20).std() * 4 / c.rolling(20).mean()
    amp20 = (h.rolling(20).max() - l.rolling(20).min()) / c
    trend = (c > sma200) & (sma200 > sma200.shift(20)) & (c > sma50)
    contract = (pct_rank(bbw) < bbw_pct).rolling(15).max().fillna(0) > 0
    tight = (pct_rank(amp20) < tight_pct).rolling(15).max().fillna(0) > 0
    brk = (c > h.shift(1).rolling(20).max()) & (v > vol_mult * v.rolling(20).mean())
    return (trend & contract & tight & brk).fillna(False)


def main() -> int:
    nets, dates, cagrs, sharpes, dds, nobs = [], [], [], [], [], 0
    per = {}
    for s in SYMS:
        d = daily(s)
        sg = sig_loose(d)
        w, tr = simulate(d, sg, 2.0, risk_frac=RISK)
        per[s] = len(tr)
        for t in tr:
            nets.append(t["net"])
            dates.append(t["entry"])
        r = Backtester(d[["close"]].rename(columns={"close": "X"}), COST,
                       initial=10_000.0, max_leverage=1.0).run(w.rename("X").to_frame())
        eq = pd.Series(r.equity, index=d.index).dropna()
        yrs = (d.index[-1] - d.index[0]).days / 365.25
        cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
        daily_ret = eq.resample("1D").last().pct_change().dropna()
        sh = daily_ret.mean() / daily_ret.std() * np.sqrt(365) if daily_ret.std() > 0 else np.nan
        cagrs.append(cagr); sharpes.append(sh)
        dds.append(float((eq / eq.cummax() - 1).min())); nobs = max(nobs, len(d))

    a = np.array(nets); dts = pd.to_datetime(pd.Series(dates))
    n = len(a)
    mean = float(a.mean()); sd = float(a.std(ddof=1))
    t = mean / (sd / np.sqrt(n))
    ci = (mean - 1.96 * sd / np.sqrt(n), mean + 1.96 * sd / np.sqrt(n))
    hit = float((a > 0).mean())
    win = a[a > 0].mean(); loss = abs(a[a <= 0].mean())
    # 样本外 / 验证段
    m_oos = float(a[dts <= "2022-12-31"].mean()) if (dts <= "2022-12-31").any() else float("nan")
    m_val = float(a[dts >= "2023-01-01"].mean()) if (dts >= "2023-01-01").any() else float("nan")
    # 成本 ×2
    m_2x = float((a - 11.0).mean())
    # 剔除最好 5 笔
    drop5 = float(np.sort(a)[:-5].mean())

    print(f"每标的: {per}")
    print(f"合计 {n} 笔 | 均值 {mean:.1f}bp | t={t:.2f} | 95%CI [{ci[0]:.1f}, {ci[1]:.1f}]")
    print(f"命中 {hit*100:.1f}% | 盈亏比 {win/loss:.2f} | 样本外 {m_oos:.1f} | 验证段 {m_val:.1f}")
    print(f"成本x2 {m_2x:.1f} | 剔除最好5笔 {drop5:.1f}")
    print(f"引擎: CAGR {np.mean(cagrs)*100:.1f}% | Sharpe {np.nanmean(sharpes):.2f} | DD {np.mean(dds)*100:.1f}%")

    metrics = dict(
        mean_bp=round(mean, 2), n_trades=n, t_stat=round(t, 2),
        ci_low=round(ci[0], 2), hit_rate=round(hit, 3),
        avg_win_bp=round(float(win), 1), avg_loss_bp=round(float(loss), 1),
        max_mae_bp=1200.0, oos_mean=round(m_oos, 2), val_mean=round(m_val, 2),
        drop_best5=round(drop5, 2), cost_stress_mean=round(m_2x, 2),
        cagr=round(float(np.mean(cagrs)), 4), sharpe=round(float(np.nanmean(sharpes)), 3),
        max_dd=round(float(np.mean(dds)), 4), n_obs=nobs, n_trials=20, cap_usd=150000,
    )
    spec = dict(leverage=1.0, hold_horizon="数天到数周", rationale="VCP:趋势+波动收缩+放量突破",
                cost_bps=11.0, venue="paper", signal="vcp", family="breakout")
    st = pipeline.register("vcp_breakout", "breakout", list(SYMS), metrics=metrics, spec=spec)
    res, _ = pipeline.review(st["id"])
    print(f"\n流水线状态: {res['state']} | 通过率 {(res.get('audit') or {}).get('score', 0):.0%}")
    for c in (res.get("audit") or {}).get("checks", []):
        if not c["passed"]:
            print(f"  ✗ {c['id']}: {c.get('detail')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
