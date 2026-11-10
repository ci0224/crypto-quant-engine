#!/usr/bin/env python3
"""回测对比:三个策略在同一份真实数据上跑,含手续费/资金费/滑点。

用法:
    python scripts/backtest.py                         # 日线,全部策略
    python scripts/backtest.py --interval 1h           # 小时线
    python scripts/backtest.py --only funding_carry
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe.backtest.engine import Backtester  # noqa: E402
from qe.config import RESULTS_DIR, Config  # noqa: E402
from qe.data.panel import load_panel  # noqa: E402
from qe.strategies import library as strat  # noqa: E402

PLANS = {
    "xsec_momentum": lambda px, fd: strat.xsec_momentum(px, lookback=30, top_n=5, rebalance=7),
    "funding_carry": lambda px, fd: strat.funding_carry(px, fd, lookback_days=7, top_n=5, rebalance=7),
    "trend_following": lambda px, fd: strat.trend_following(px, fast=20, slow=100, vol_target=0.25, rebalance=1),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--only", default="")
    ap.add_argument("--start", default="")
    args = ap.parse_args()

    cfg = Config.load()
    px, fd = load_panel(args.interval)
    if args.start:
        px = px[px.index >= pd.Timestamp(args.start, tz="UTC")]
        fd = fd[fd.index >= pd.Timestamp(args.start, tz="UTC")] if not fd.empty else fd

    print(f"数据: {len(px)} 根 {args.interval} · {px.shape[1]} 个币 · "
          f"{px.index[0]:%Y-%m-%d} → {px.index[-1]:%Y-%m-%d}")
    print(f"成本假设: 单边 {cfg.costs.one_way_bps} bps(taker {cfg.costs.taker_bps} + 滑点 {cfg.costs.slippage_bps})"
          f" · 资金费已计入 · 初始资金 ${cfg.initial_equity:,.0f}\n")

    names = [args.only] if args.only else list(PLANS)
    summary = {}
    for name in names:
        w = PLANS[name](px, fd)
        bt = Backtester(px, costs=cfg.costs, funding=fd, initial=cfg.initial_equity,
                        max_leverage=cfg.max_leverage)
        res = bt.run(w)
        s = res.stats
        avg_gross = float(res.weights.abs().sum(axis=1).mean())
        # 年化成本拖累
        cost_drag = float(res.cost_paid.sum() / max(len(res.returns), 1) * 365)
        fund = float(res.funding_paid.sum())
        print(f"【{name}】")
        print(f"  {res.summary()}")
        print(f"  平均总敞口 {avg_gross:.2f}x · 手续费累计 {-res.cost_paid.sum()*100:.1f}% · "
              f"资金费贡献 {-fund*100:+.1f}% · 期末 ${res.equity.iloc[-1]:,.0f}")
        print()
        summary[name] = {**{k: (float(v) if v is not None else None) for k, v in s.items()},
                         "avg_gross": avg_gross, "cost_total": float(-res.cost_paid.sum()),
                         "funding_total": float(-fund), "final": float(res.equity.iloc[-1])}
        # 保存权益曲线
        out = RESULTS_DIR / f"equity_{name}_{args.interval}.csv"
        res.equity.to_csv(out, header=["equity"])

    (RESULTS_DIR / f"summary_{args.interval}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已存: {RESULTS_DIR}/summary_{args.interval}.json")


if __name__ == "__main__":
    main()
