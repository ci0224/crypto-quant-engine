#!/usr/bin/env python3
"""策略参数扫描 + 训练/测试分割(前 60% 训练、后 40% 测试)。

只信"测试段也赚"的参数;训练段好、测试段崩 = 过拟合。

用法:
    python scripts/sweep.py                 # 全部
    python scripts/sweep.py --interval 1h
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe.backtest.engine import Backtester  # noqa: E402
from qe.config import RESULTS_DIR, Config  # noqa: E402
from qe.data.panel import load_panel  # noqa: E402
from qe.strategies import library as S  # noqa: E402

GRID = [
    ("xsec_momentum", {"lookback": [7, 14, 30, 60], "rebalance": [1, 7], "top_n": [5]}),
    ("xsec_reversal", {"lookback": [1, 2, 3], "rebalance": [1, 2], "top_n": [5]}),
    ("funding_carry", {"lookback_days": [3, 7, 14], "top_n": [3, 5], "rebalance": [7]}),
    ("trend_following", {"fast": [10, 20, 50], "slow": [50, 100, 200], "rebalance": [1, 7]}),
]


def run_one(name, params, px, fd, cfg):
    fn = getattr(S, name)
    try:
        if name == "funding_carry":
            w = fn(px, fd, **params)
        else:
            w = fn(px, **params)
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:60]}"
    bt = Backtester(px, costs=cfg.costs, funding=fd, initial=cfg.initial_equity,
                    max_leverage=cfg.max_leverage)
    return bt.run(w), None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="1d")
    args = ap.parse_args()
    cfg = Config.load()
    px, fd = load_panel(args.interval)
    n = len(px)
    split = int(n * 0.6)
    print(f"数据 {px.shape[1]} 币 · {n} 根 · 训练 {split} 根 / 测试 {n - split} 根\n")

    rows = []
    for name, grid in GRID:
        keys = list(grid)
        for combo in itertools.product(*[grid[k] for k in keys]):
            p = dict(zip(keys, combo))
            if name == "trend_following" and p["fast"] >= p["slow"]:
                continue
            w = None
            try:
                w = (getattr(S, name)(px, fd, **p) if name == "funding_carry"
                     else getattr(S, name)(px, **p))
            except Exception as e:  # noqa: BLE001
                continue
            bt = Backtester(px, costs=cfg.costs, funding=fd, initial=cfg.initial_equity,
                            max_leverage=cfg.max_leverage)
            res = bt.run(w)
            tr = Backtester(px.iloc[:split], costs=cfg.costs, funding=fd, initial=cfg.initial_equity,
                            max_leverage=cfg.max_leverage).run(w.iloc[:split])
            te = Backtester(px.iloc[split:], costs=cfg.costs, funding=fd, initial=cfg.initial_equity,
                            max_leverage=cfg.max_leverage).run(w.iloc[split:])
            rows.append({
                "strategy": name, **p,
                "all_cagr": res.stats["cagr"], "all_sharpe": res.stats["sharpe"],
                "all_dd": res.stats["max_dd"],
                "train_cagr": tr.stats["cagr"], "train_sharpe": tr.stats["sharpe"],
                "test_cagr": te.stats["cagr"], "test_sharpe": te.stats["sharpe"],
                "test_dd": te.stats["max_dd"],
                "gross": float(res.weights.abs().sum(axis=1).mean()),
            })
            print(f"  {name:16s} {json.dumps(p, ensure_ascii=False):52s} "
                  f"训练 CAGR {tr.stats['cagr']*100:+7.1f}% Sharpe {tr.stats['sharpe']:5.2f} │ "
                  f"测试 CAGR {te.stats['cagr']*100:+7.1f}% Sharpe {te.stats['sharpe']:5.2f}")

    df = pd.DataFrame(rows)
    out = RESULTS_DIR / f"sweep_{args.interval}.csv"
    df.to_csv(out, index=False)
    print(f"\n共 {len(df)} 组 → {out}")

    # 只保留"训练与测试都为正"的
    ok = df[(df.train_cagr > 0) & (df.test_cagr > 0)].sort_values("test_sharpe", ascending=False)
    print(f"\n=== 训练&测试同时为正的 {len(ok)} 组(按测试 Sharpe 排序)===")
    if len(ok):
        print(ok.head(12).to_string(index=False,
              columns=["strategy", "train_cagr", "train_sharpe", "test_cagr", "test_sharpe", "test_dd", "gross"]))
    else:
        print("❌ 没有一组在测试段为正 —— 说明这些参数族在当前数据上都不成立(诚实结论)")


if __name__ == "__main__":
    main()
