#!/usr/bin/env python3
"""每日自动对账:回测 vs 实盘/纸交易,只在偏差超阈值时输出(cron 用)。

静默设计:正常 → 无输出;异常 → 打印告警(会被 cron 推送到对话)。
结果同时写入 reconcile_runs 表,供看板读取。

阈值:
   信号一致率 < 95%          → 逻辑分歧(代码/信号定义不一致)
   成交率偏差 > 20pp          → 成交假设失真
   配对净收益偏差均值 |Δ| > 5bp → 成本/出场逻辑分歧
   漏单 > 3 笔                → 实盘漏执行
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe import ledger, registry
from qe.strict.data import boll_state, load_1m, load_funding, resample
from qe.strict.engine import StrictCfg, run

DB = Path(__file__).resolve().parent.parent / "data" / "ledger.db"
BUCKET = 300
DAYS = int(os.environ.get("QE_RECON_DAYS", "7"))

SCHEMA = """CREATE TABLE IF NOT EXISTS reconcile_runs (
  ts REAL, sid TEXT, days INTEGER, parity REAL, trades_loop INTEGER, trades_bt INTEGER,
  fill_loop REAL, fill_bt REAL, paired INTEGER, mean_diff REAL, missing INTEGER, extra INTEGER
);"""


def _db():
    c = sqlite3.connect(DB, timeout=10)
    c.executescript(SCHEMA)
    return c


def compare(s, days: int) -> dict | None:
    coin = (s["symbols"] or ["BTC"])[0]
    sid = s["id"]
    since = time.time() - days * 86400
    loop_sig = ledger.rows(sid, "loop", "signal", since)
    loop_tr = ledger.rows(sid, "loop", "trade", since)

    b1 = load_1m(coin)
    b1 = b1.loc[pd.Timestamp(since, unit="s", tz="UTC") - pd.Timedelta(days=1):]
    b5 = resample(b1, 5)
    st = boll_state(b5["close"], 20, 2.0)
    sig_mask = (st > 0).to_numpy(dtype=bool) & (b5.index.hour >= 8) & (b5.index.hour < 20)
    r = run(b1, b5, sig_mask, load_funding(coin), StrictCfg(through_bp=2.0, fill_rate=0.85,
                                                          taker_exit_prob=0.514))
    td = r["trades"]

    ls = {int(x["signal_ts"] // BUCKET) for x in loop_sig}
    bs = {int(t.timestamp() // BUCKET) for t in b5.index[sig_mask]}
    parity = len(ls & bs) / max(len(bs), 1) if bs else 1.0

    bt = [(float(t["t"].timestamp()), t) for _, t in td.iterrows()]
    used, diffs = set(), []
    for row in loop_tr:
        lt = float(row.get("fill_ts") or row.get("signal_ts") or 0)
        cand = [(i, ts, t) for i, (ts, t) in enumerate(bt) if i not in used and abs(ts - lt) <= 600]
        if cand:
            i, ts, t = min(cand, key=lambda x: abs(x[1] - lt))
            used.add(i)
            diffs.append(float(row["net_bp"]) - float(t["net_bp"]))
    return {
        "sid": sid, "days": days, "parity": parity,
        "trades_loop": len(loop_tr), "trades_bt": len(td),
        "fill_loop": len(loop_tr) / max(len(ls), 1),
        "fill_bt": len(td) / max(len(bs), 1),
        "paired": len(diffs), "mean_diff": float(np.mean(diffs)) if diffs else 0.0,
        "missing": max(len(bt) - len(used), 0), "extra": max(len(loop_tr) - len(diffs), 0),
    }


def main() -> int:
    alerts = []
    db = _db()
    for s in registry.all():
        if s["state"] not in ("paper", "live"):
            continue
        try:
            c = compare(s, DAYS)
        except Exception as e:
            alerts.append(f"⚠️ 对账失败 {s['name']}: {type(e).__name__}: {e}")
            continue
        if not c:
            continue
        db.execute("INSERT INTO reconcile_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (time.time(), c["sid"], c["days"], c["parity"], c["trades_loop"],
                    c["trades_bt"], c["fill_loop"], c["fill_bt"], c["paired"],
                    c["mean_diff"], c["missing"], c["extra"]))
        # 阈值判定
        if c["parity"] < 0.95:
            alerts.append(f"🔴 {s['name']} 信号一致率 {c['parity']*100:.1f}% < 95% —— 逻辑分歧")
        if abs(c["fill_loop"] - c["fill_bt"]) * 100 > 20:
            alerts.append(f"🟠 {s['name']} 成交率偏差 {c['fill_loop']*100:.1f}% vs "
                          f"{c['fill_bt']*100:.1f}% —— 成交假设失真")
        if c["paired"] >= 3 and abs(c["mean_diff"]) > 5:
            alerts.append(f"🟠 {s['name']} 配对净收益偏差 {c['mean_diff']:+.1f}bp > 5bp")
        if c["missing"] > 3:
            alerts.append(f"🟠 {s['name']} 漏单 {c['missing']} 笔 > 3")
    db.commit()
    db.close()
    if alerts:
        print(f"📋 回测-实盘对账告警(近 {DAYS} 天)\n" + "\n".join(alerts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
