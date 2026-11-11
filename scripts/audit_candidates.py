#!/usr/bin/env python3
"""把研究筛出的候选送进 pipeline 跑 G1–G11。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe import pipeline, registry
from qe.research.trade_stats import trade_stats
from scripts.research_batch2 import bars1h, make_rules, metrics

CANDS = [
    ("SOL", "资金费极值", "funding_extreme_short", "funding", 1.0),
    ("BTC", "资金费反向", "funding_negative_long", "funding", 1.0),
    ("ETH", "ATR通道突破", "atr_breakout", "breakout", 1.0),
    ("ETH", "布林突破", "boll_breakout_eth", "breakout", 1.0),
]


def main():
    for sym, rule, name, fam, lev in CANDS:
        b = bars1h(sym)
        w = make_rules(b, sym)[rule]
        m = metrics(b, w)
        st = trade_stats(b, pd.Series(np.asarray(w, float), index=b.index), 11.0)
        if not st or not m:
            print(f"{sym} {rule}: 数据不足,跳过")
            continue
        met = dict(
            mean_bp=st["mean"], n_trades=st["n"], t_stat=st["t"],
            ci_low=st["ci_lo"], hit_rate=st["hit"], payoff=st["payoff"],
            max_mae_bp=abs(st["mae"]), oos_mean=st["oos"], val_mean=st["val"],
            drop_best5=st["drop5"], cost_stress_mean=st["mean"] - 11.0,
            recent_n=st["rec_n"], recent_mean_bp=st["rec"], recent_ci_low=st["ci_lo"],
            cagr=m["cagr"], sharpe=m["sh"], max_dd=m["dd"], turnover=m["turn"],
        )
        spec = dict(leverage=lev, hold_horizon="数小时到数天", cost_bps=11.0,
                    venue="paper", notional_usd=20.0,
                    note=f"{sym} {rule} @1h,引擎 CAGR {m['cagr']:.1f}%/Sharpe {m['sh']:.2f}/DD {m['dd']:.1f}%")
        ex = None
        for s in registry.all():
            if s["name"] == name:
                ex = s
        if ex:
            registry.update(ex["id"], metrics=met, spec=spec, state="backtested")
            sid = ex["id"]
        else:
            sid = pipeline.register(name, fam, [sym], spec, metrics=met, note="批量研究候选")["id"]
        res, gate_res = pipeline.review(sid)
        print(f"\n{sym} {rule} → {name}")
        print(f"  引擎: CAGR {m['cagr']:.1f}% | Sharpe {m['sh']:.2f} | DD {m['dd']:.1f}% | 换手 {m['turn']:.0f}%/yr")
        print(f"  逐笔: {st['n']} 笔 | 均值 {st['mean']:.2f}bp | t={st['t']:.2f} | CI低 {st['ci_lo']:.2f} | 近12月 {st['rec']:.2f}bp")
        print(f"  审计: {res['state'].upper()} {gate_res.get('score', 0):.0%}")
        bad = [c for c in gate_res.get("checks", []) if not c["passed"]]
        for c in bad:
            print(f"    ✗ {c['id']} {c['label']} | {c['detail']}")


if __name__ == "__main__":
    main()
