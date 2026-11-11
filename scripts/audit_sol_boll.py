#!/usr/bin/env python3
"""把 SOL 布林突破做多 放进 pipeline 跑完整审计(G1–G11)。

逐笔口径:
  · 入场:4h 收盘 > 布林上轨(20, 2σ) → 下一根开盘价建仓(不偷价)
  · 出场:收盘 < 中轨 → 下一根开盘平仓
  · 成本:taker 5.5bp/边(4.5 + 1 滑点)= 11bp 往返
  · 逐笔 MAE:持仓期间最低价相对入场价的最大逆行
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe import pipeline, registry
from qe.strict.data import load_1m

COST_RT_BP = 11.0
COST_SIDE_BP = 5.5


def bars(rule: str = "4h") -> pd.DataFrame:
    b1 = load_1m("SOL")
    return b1.resample(rule).agg({"open": "first", "high": "max", "low": "min",
                                  "close": "last", "volume": "sum"}).dropna()


def trades_from_w(df: pd.DataFrame, n=20, k=2.0) -> list[dict]:
    ma = df["close"].rolling(n).mean()
    sd = df["close"].rolling(n).std()
    up, mid = ma + k * sd, ma
    pos, entry_i = 0, None
    out = []
    for i in range(len(df)):
        c = df["close"].iloc[i]
        if pos == 0 and i + 1 < len(df) and pd.notna(up.iloc[i]) and c > up.iloc[i]:
            pos, entry_i = 1, i + 1          # 下一根开盘入场
        elif pos == 1 and i + 1 < len(df) and pd.notna(mid.iloc[i]) and c < mid.iloc[i]:
            exit_i = i + 1
            ep = float(df["open"].iloc[entry_i])
            xp = float(df["open"].iloc[exit_i])
            seg = df.iloc[entry_i:exit_i + 1]
            mae = (float(seg["low"].min()) / ep - 1) * 1e4
            gross = (xp / ep - 1) * 1e4
            out.append({"entry_ts": df.index[entry_i], "exit_ts": df.index[exit_i],
                        "gross_bp": gross, "net_bp": gross - COST_RT_BP,
                        "hold_h": (df.index[exit_i] - df.index[entry_i]).total_seconds() / 3600,
                        "mae_bp": mae})
            pos, entry_i = 0, None
    return out


def boot_ci(x: np.ndarray, n_boot=2000, block=1) -> tuple[float, float]:
    rng = np.random.default_rng(42)
    means = []
    for _ in range(n_boot):
        idx = rng.integers(0, len(x), len(x))
        means.append(x[idx].mean())
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def summarize(tr: list[dict]) -> dict:
    if not tr:
        return {}
    p = np.array([t["net_bp"] for t in tr])
    d = pd.Series(p, index=pd.to_datetime([t["exit_ts"] for t in tr], utc=True)) \
        .groupby(level=0).sum()
    dd = pd.date_range(d.index.min(), d.index.max(), freq="D", tz="UTC")
    s = d.reindex(dd, fill_value=0.0)
    sd = s.std(ddof=1)
    lo, hi = boot_ci(p)
    return {"n": len(p), "mean": float(p.mean()), "std": float(p.std(ddof=1)),
            "hit": float((p > 0).mean()), "ci_low": lo, "ci_high": hi,
            "t": float(s.mean() / sd * np.sqrt(len(s))) if sd else 0.0,
            "mae": float(min(t["mae_bp"] for t in tr)),
            "hold_med": float(np.median([t["hold_h"] for t in tr]))}


def main() -> int:
    df = bars("4h")
    tr = trades_from_w(df)
    print(f"SOL 4h 布林突破:共 {len(tr)} 笔,{df.index[0].date()} → {df.index[-1].date()}")

    full = summarize(tr)
    oos = summarize([t for t in tr if t["exit_ts"] < pd.Timestamp("2023-01-01", tz="UTC")])
    val = summarize([t for t in tr if t["exit_ts"] >= pd.Timestamp("2023-01-01", tz="UTC")])
    rec = summarize([t for t in tr if t["exit_ts"] >= pd.Timestamp("2025-11-11", tz="UTC")])

    # 成本 ×2
    p = np.array([t["net_bp"] for t in tr])
    p2 = np.array([t["gross_bp"] - COST_RT_BP * 2 for t in tr])
    # 剔除最好 5 天
    d = pd.Series([t["net_bp"] for t in tr],
                  index=pd.to_datetime([t["exit_ts"] for t in tr], utc=True))
    daily = d.groupby(level=0).sum().sort_values()
    drop5 = daily.iloc[:-5].sum() / len(tr) if len(daily) > 5 else 0.0

    print(f"  全样本: {full['n']} 笔 | 均值 {full['mean']:+.2f}bp | CI [{full['ci_low']:.2f}, {full['ci_high']:.2f}] | t={full['t']:.2f} | 命中 {full['hit']*100:.1f}% | MAE {full['mae']:.0f}bp | 持仓中位 {full['hold_med']:.0f}h")
    print(f"  样本外(≤2022): {oos.get('n',0)} 笔 | 均值 {oos.get('mean',0):+.2f}bp | t={oos.get('t',0):.2f}")
    print(f"  验证段(≥2023): {val.get('n',0)} 笔 | 均值 {val.get('mean',0):+.2f}bp | t={val.get('t',0):.2f}")
    print(f"  近 12 月: {rec.get('n',0)} 笔 | 均值 {rec.get('mean',0):+.2f}bp | CI [{rec.get('ci_low',0):.2f}, {rec.get('ci_high',0):.2f}]")
    print(f"  成本×2: 均值 {p2.mean():+.2f}bp | 剔除最好 5 天: {drop5:+.2f}bp/笔")

    metrics = {
        "mean_bp": round(full["mean"], 2), "n_trades": full["n"], "t_stat": round(full["t"], 2),
        "ci_low": round(full["ci_low"], 2), "hit_rate": round(full["hit"], 3),
        "max_mae_bp": round(full["mae"], 0), "std_bp": round(full["std"], 2),
        "oos_mean": round(oos.get("mean", 0), 2), "val_mean": round(val.get("mean", 0), 2),
        "drop_best5": round(drop5, 2),
        "cost_stress_mean": round(float(p2.mean()), 2),
        "avg_win_bp": round(float(p[p > 0].mean()), 2) if (p > 0).any() else None,
        "avg_loss_bp": round(float(p[p <= 0].mean()), 2) if (p <= 0).any() else None,
        "recent_n": rec.get("n", 0), "recent_mean_bp": round(rec.get("mean", 0), 2),
        "recent_ci_low": round(rec.get("ci_low", 0), 2),
        "start": str(df.index[0].date()), "end": str(df.index[-1].date()),
    }
    spec = {"leverage": 1.0, "hold_horizon": "数天", "cost_bps": COST_RT_BP,
            "venue": "paper", "rule": "close>BB_upper(20,2σ) 入,close<BB_mid 出",
            "timeframe": "4h", "rationale": "用户提供的候选,全样本复现后提交审计"}

    existing = [s for s in registry.all() if s["name"] == "sol_boll_breakout"]
    if existing:
        sid = existing[0]["id"]
        registry.update(sid, metrics=metrics, spec=spec, state="backtested")
    else:
        sid = pipeline.register("sol_boll_breakout", "boll_breakout", ["SOL"], spec,
                                metrics, note="来自用户候选(SOL 布林突破做多),4h 严格复现")
        sid = sid["id"] if isinstance(sid, dict) else sid
    strat, res = pipeline.review(sid)
    print(f"\n审计结果: {strat['state'].upper()} | score {res.get('score')} | blockers {res.get('blockers')}")
    for c in res.get("checks", []):
        print(f"  {'✅' if c['passed'] else '❌'} {c['id']} {c['label']} | {c['detail']}")
    import qe.gate as _g
    for c in _g.live_checks(metrics):
        print(f"  {'✅' if c['passed'] else '🚫'} {c['id']} {c['label']} | {c['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
