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


def _hl_signals(s, days: int, since: float) -> set:
    """用 HL 5m K 线 + 与实盘相同的信号函数重建基准(仅 testnet/mainnet)。

    为什么必须这样:实盘信号来自 HL 的 5m K 线,回测信号来自 Binance 1m。
    两个交易所的价格不同 → 同一时刻的布林带位置不同 → 必然对不上。
    要比"逻辑是否一致",必须同源同函数。
    """
    from qe.data import hyperliquid as hl
    from qe.live.paper_loop import _mk_signal
    sg = ((s.get("spec") or {}).get("signal") or "boll20")
    out: set = set()
    for c in (s.get("symbols") or []):
        try:
            df = hl.candles(c, "5m", max(2, int(days) + 1))
            if df is not None and len(df) and int(time.time()*1000) < int(df.index[-1].timestamp()*1000) + 300_000:
                df = df.iloc[:-1]          # 与实盘一致:丢掉未收盘 bar
        except Exception:
            continue
        if df is None or len(df) < 60:
            continue
        sig = _mk_signal(df, sg, series=True)
        for t, ok in zip(df.index, sig.to_numpy(dtype=bool)):
            if ok and t.timestamp() >= since and 8 <= t.hour < 20:
                out.add(int(t.timestamp() // BUCKET))
    return out


def compare(s, days: int) -> dict | None:
    coin = (s["symbols"] or ["BTC"])[0]
    sid = s["id"]
    since = time.time() - days * 86400
    venue = ((s.get("spec") or {}).get("venue") or "paper")
    src = "testnet" if venue == "testnet" else "loop"

    loop_sig = ledger.rows(sid, src, "signal", since)
    loop_tr = ledger.rows(sid, src, "trade", since)
    # 只看"该时间点之后"的实盘记录:代码口径改过之前的数据不可比
    import os
    era = float(os.environ.get("QE_RECON_ERA", "0"))
    if era > 0:
        loop_sig = [x for x in loop_sig if float(x.get("signal_ts") or 0) >= era]
        loop_tr = [x for x in loop_tr if float(x.get("fill_ts") or x.get("signal_ts") or 0) >= era]
    ls = {int(x["signal_ts"] // BUCKET) for x in loop_sig if x.get("signal_ts")}

    # ---- Binance 严格回测(逐笔对比始终用它)----
    b1 = load_1m(coin)
    b1 = b1.loc[pd.Timestamp(since, unit="s", tz="UTC") - pd.Timedelta(days=1):]
    b5 = resample(b1, 5)
    st = boll_state(b5["close"], 20, 2.0)
    sig_mask = (st > 0).to_numpy(dtype=bool) & (b5.index.hour >= 8) & (b5.index.hour < 20)
    r = run(b1, b5, sig_mask, load_funding(coin),
            StrictCfg(through_bp=2.0, fill_rate=0.85, taker_exit_prob=0.514))
    td = r["trades"]
    bs = {int(t.timestamp() // BUCKET) for t in b5.index[sig_mask]}

    # ---- 信号一致率 ----
    # 只比对"循环实际在跑"的时段:实盘可能只跑了几个小时,
    # 用整 7 天做分母会把覆盖率问题误判成逻辑分歧。
    live_ts = [float(x["signal_ts"]) for x in loop_sig if x.get("signal_ts")]
    t_eff = max(since, min(live_ts)) if live_ts else since
    base = _hl_signals(s, days, t_eff) if venue != "paper" else bs
    # 口径说明:实盘只在"新建挂单"时记一条(一个 dip 只记首条),
    # 基准是"每根满足条件的 bar" → 用分母=基准会系统性偏低。
    # 正确指标是**召回率**:实盘发的每个信号,基准里是否也存在(±1 根 bar)
    if len(base) < 2 or len(ls) < 2:
        parity = None
    else:
        base_ext = set()
        for b in base:
            base_ext.update((b - 1, b, b + 1))
        parity = len(ls & base_ext) / len(ls)

    # ---- 成交率(样本 <5 条不判定;1 信号 3 笔这种除法病要挡掉)----
    fill_loop = (len(loop_tr) / len(ls)) if len(ls) >= 5 else None
    fill_bt = (len(td) / len(bs)) if len(bs) >= 5 else None

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
        "signals_loop": len(ls), "signals_bt": len(base),
        "live_hours": round((time.time() - t_eff) / 3600, 1),
        "trades_loop": len(loop_tr), "trades_bt": len(td),
        "fill_loop": fill_loop, "fill_bt": fill_bt,
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
        fl, fb = c["fill_loop"], c["fill_bt"]
        db.execute("INSERT INTO reconcile_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (time.time(), c["sid"], c["days"],
                    (-1.0 if c["parity"] is None else c["parity"]), c["trades_loop"],
                    c["trades_bt"], (-1.0 if fl is None else fl), (-1.0 if fb is None else fb),
                    c["paired"], c["mean_diff"], c["missing"], c["extra"]))
        # 阈值判定(样本不足的项一律不判定)
        if c["parity"] is not None and c["parity"] < 0.95:
            alerts.append(f"🔴 {s['name']} 信号一致率 {c['parity']*100:.1f}% < 95% —— 逻辑分歧")
        if fl is not None and fb is not None and abs(fl - fb) * 100 > 20:
            alerts.append(f"🟠 {s['name']} 成交率偏差 {fl*100:.1f}% vs {fb*100:.1f}% —— 成交假设失真")
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
