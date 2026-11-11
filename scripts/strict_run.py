#!/usr/bin/env python3
"""严格回测:三档口径对比(复刻 / 执行严格 / 全严格)。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe.strict.data import audit, boll_state, load_1m, load_funding, resample
from qe.strict.engine import StrictCfg, run

PROFILES = {
    "①复刻原规则": dict(through_bp=0.0, fill_rate=1.0, taker_exit_prob=0.0),
    "②执行严格": dict(through_bp=2.0, fill_rate=0.85, taker_exit_prob=0.0),
    "③全严格":   dict(through_bp=2.0, fill_rate=0.85, taker_exit_prob=0.514),
}


def stats(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {"n": 0, "mean": 0.0, "ci_low": 0.0, "t": 0.0, "hit": 0.0, "sharpe": 0.0}
    p = df["net_bp"].astype(float).values
    day = pd.Series(p, index=pd.DatetimeIndex(pd.to_datetime(df["t"], utc=True)).normalize()).groupby(level=0).sum()
    days = pd.date_range(day.index.min(), day.index.max(), freq="D", tz="UTC")
    s = day.reindex(days, fill_value=0.0)
    sd = s.std(ddof=1)
    n = len(p)
    se = p.std(ddof=1) / np.sqrt(n) if n > 1 else float("nan")
    return {"n": n, "mean": float(p.mean()), "ci_low": float(p.mean() - 1.96 * se),
            "t": float(s.mean() / sd * np.sqrt(len(s))) if sd > 0 else 0.0,
            "hit": float((p > 0).mean()),
            "sharpe": float(s.mean() / sd * np.sqrt(365)) if sd > 0 else 0.0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None)
    a = ap.parse_args()
    syms = [a.symbol] if a.symbol else ["BTC", "ETH", "SOL"]

    for sym in syms:
        b1 = load_1m(sym)
        au = audit(b1, sym)
        b5 = resample(b1, 5)
        st = boll_state(b5["close"], 20, 2.0)
        sig = (st > 0)   # 状态机:在下跌段内
        fund = load_funding(sym)

        print("=" * 84)
        print(f"【{sym}】数据审计:{au['bars']:,} 根 {au['start'][:10]}→{au['end'][:10]} "
              f"| 缺口 {au['gap_pct']}% | 重复 {au['duplicates']} | OHLC违例 {au['ohlc_violations']} "
              f"| {'✅通过' if au['pass'] else '❌不通过'}")
        print(f"      资金费 {len(fund):,} 条"
              + (f" {fund.index[0].date()}→{fund.index[-1].date()}" if len(fund) else " ⚠️无"))
        print(f"\n  {'口径':<12}{'笔数':>7}{'均值bp':>10}{'CI下':>9}{'t值':>7}{'命中':>8}{'Sharpe':>8}"
              f"{'资金费bp':>10}{'冲击bp':>8}{'taker%':>8}")
        res = {}
        for name, kw in PROFILES.items():
            r = run(b1, b5, sig, fund, StrictCfg(**kw))
            s = stats(r["trades"]); res[name] = (r, s)
            td = r["trades"]
            f_mean = td["funding_bp"].mean() if len(td) else 0.0
            i_mean = td["impact_bp"].mean() if len(td) else 0.0
            tk = (~td["reason"].isin(["target", "timeout_maker"]).eq(False)).mean() if len(td) else 0.0
            taker_share = ((td["fee_bp"] > 4.5).mean() if len(td) else 0.0) * 100
            print(f"  {name:<12}{s['n']:>7}{s['mean']:>10.2f}{s['ci_low']:>9.2f}{s['t']:>7.2f}"
                  f"{s['hit']*100:>7.1f}%{s['sharpe']:>8.2f}{-f_mean:>10.2f}{-i_mean:>8.2f}{taker_share:>7.0f}%")

        base = res["①复刻原规则"][1]
        full = res["③全严格"][1]
        if base["n"] and full["n"]:
            print(f"\n  严格化代价:均值 {base['mean']:+.2f} → {full['mean']:+.2f}bp "
                  f"({(full['mean']/base['mean']-1)*100:+.1f}%) | 笔数 {base['n']} → {full['n']} "
                  f"({(full['n']/base['n']-1)*100:+.1f}%)")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
