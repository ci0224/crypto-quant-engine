#!/usr/bin/env python3
"""把已跑完研究的策略灌进流水线(用真实回测数字),演示完整流转。

数据来源:scripts/deep_limit_multi.py 与 scripts/long_sample.py 的实测输出。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe import pipeline, registry

# ---- 实测指标(见 README / docs)----
DEEP = {
    "BTC": dict(n_trades=618, n_days=2499, mean_bp=27.29, t_stat=4.62, ci_low=16.79, ci_high=37.30,
                oos_mean=26.58, val_mean=29.40, hit_rate=0.688, max_mae_bp=2810, max_dd=-0.415,
                drop_best5=22.1, cost_stress_mean=24.3),
    "ETH": dict(n_trades=1191, n_days=2499, mean_bp=16.62, t_stat=4.39, ci_low=9.19, ci_high=23.90,
                oos_mean=17.66, val_mean=14.53, hit_rate=0.664, max_mae_bp=2843, max_dd=-0.44,
                drop_best5=12.8, cost_stress_mean=14.1),
    "SOL": dict(n_trades=1819, n_days=2240, mean_bp=21.80, t_stat=6.44, ci_low=15.21, ci_high=28.18,
                oos_mean=21.19, val_mean=22.85, hit_rate=0.685, max_mae_bp=4581, max_dd=-0.52,
                drop_best5=17.4, cost_stress_mean=19.0),
}
LEV = {"BTC": 3.0, "ETH": 3.0, "SOL": 1.5}   # SOL 波动更大,MAE 4581bp → 只能 1.5x

TREND = dict(n_trades=118, n_days=2499, mean_bp=None, t_stat=1.48, ci_low=-0.18, ci_high=1.35,
             oos_mean=8.4, val_mean=12.9, max_dd=-0.339, drop_best5=4.98, cost_stress_mean=12.1,
             shapre=0.57)
CARRY = dict(n_trades=210, n_days=1680, mean_bp=11.0, t_stat=0.81, ci_low=-0.05, ci_high=0.19,
             oos_mean=None, val_mean=0.11, max_dd=-0.31, drop_best5=-2.4, cost_stress_mean=-1.2)


def seed():
    existing = {s["name"] for s in registry.all()}
    out = []

    for sym, m in DEEP.items():
        name = f"deep_limit_{sym.lower()}"
        if name in existing:
            continue
        spec = {"leverage": LEV[sym], "hold_horizon": "≤55 分钟(超时腿可延长)",
                "cost_bps": 3.0, "family_note": "maker 深挂 dip-buy"}
        s = pipeline.register(name, "deep_limit", [sym], spec, metrics=m,
                              note="Boll<low 深挂 −100bp,目标 +120bp,08–20 UTC")
        s, res = pipeline.review(s["id"])
        out.append((name, s["state"], res["score"]))

    if "trend_btc_eth" not in existing:
        spec = {"leverage": 1.0, "hold_horizon": "数天到数周(日线 10/50)", "cost_bps": 11.0}
        s = pipeline.register("trend_btc_eth", "trend", ["BTC", "ETH"], spec, metrics=TREND,
                              note="长样本复核:t=1.48、CI 跨 0")
        s, res = pipeline.review(s["id"])
        out.append(("trend_btc_eth", s["state"], res["score"]))

    if "funding_carry" not in existing:
        spec = {"leverage": 1.0, "hold_horizon": "1 周", "cost_bps": 11.0}
        s = pipeline.register("funding_carry", "funding_carry", ["BTC", "ETH"], spec, metrics=CARRY,
                              note="留一法 -8.2%~+19.8%,判定过拟合")
        s, res = pipeline.review(s["id"])
        out.append(("funding_carry", s["state"], res["score"]))

    for name, state, score in out:
        print(f"  {name:20s} → {state:10s} (audit score {score})")


if __name__ == "__main__":
    seed()
    print("\n当前策略:")
    for s in registry.all():
        print(f"  {s['name']:20s} {s['state']:18s} {s['family']}")
