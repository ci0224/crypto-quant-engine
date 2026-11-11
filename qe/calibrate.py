"""L2 队列校准:记录 Hyperliquid 订单簿快照,量化挂单队列深度与成交概率。

为什么需要:
  严格框架里 fill_rate=0.85 是拍的。真实成交取决于:
    ① 我们的挂单在 −100bp 这个价位上,前面排了多少量(队首/队尾)
    ② 该价位的累计深度 / 我们的单笔规模
  记录真实 L2 快照后,可以算出"同类挂单的历史成交率",替代拍脑袋的常数。

用法:
  python -m qe.calibrate record --interval 60      # 常驻记录(systemd)
  python -m qe.calibrate report                   # 出报告
  python -m qe.calibrate report --from-ledger deep_limit_btc   # 用真实台账反推成交率
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.request
from contextlib import contextmanager

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "l2.db")
API = "https://api.hyperliquid.xyz/info"
DIST_BP = (25, 50, 100, 150, 200, 300)

SCHEMA = """
CREATE TABLE IF NOT EXISTS l2 (
  ts    REAL NOT NULL,
  coin  TEXT NOT NULL,
  mid   REAL,
  bid   REAL,
  ask   REAL,
  spread_bp REAL,
  depth_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_l2 ON l2 (coin, ts);
"""


@contextmanager
def conn():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        c.executescript(SCHEMA)
        yield c
        c.commit()
    finally:
        c.close()


def _post(payload: dict, timeout: int = 15):
    req = urllib.request.Request(API, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def snapshot(coin: str) -> dict | None:
    """拉一次订单簿,计算各距离档的累计美元深度(bid 侧)。"""
    try:
        d = _post({"type": "l2Book", "coin": coin})
    except Exception:
        return None
    lv = d.get("levels") or []
    if len(lv) < 2 or not lv[0] or not lv[1]:
        return None
    bids = [(float(x["px"]), float(x["sz"])) for x in lv[0]]
    asks = [(float(x["px"]), float(x["sz"])) for x in lv[1]]
    bid, ask = bids[0][0], asks[0][0]
    mid = (bid + ask) / 2
    depth = {}
    for bp in DIST_BP:
        lvl = mid * (1 - bp / 1e4)
        depth[str(bp)] = round(sum(px * sz for px, sz in bids if px >= lvl), 0)
    return {"ts": time.time(), "coin": coin, "mid": mid, "bid": bid, "ask": ask,
            "spread_bp": (ask - bid) / mid * 1e4, "depth": depth}


def record(coins, interval: int) -> None:
    while True:
        for c in coins:
            s = snapshot(c)
            if not s:
                continue
            with conn() as db:
                db.execute(
                    "INSERT INTO l2 (ts,coin,mid,bid,ask,spread_bp,depth_json) VALUES (?,?,?,?,?,?,?)",
                    (s["ts"], s["coin"], s["mid"], s["bid"], s["ask"], s["spread_bp"],
                     json.dumps(s["depth"])))
        print(f"[l2] {time.strftime('%H:%M:%S')} 记录 {len(coins)} 个标的", flush=True)
        time.sleep(interval)


def report(coins, from_ledger: str | None) -> None:
    with conn() as db:
        for c in coins:
            rows = [dict(r) for r in db.execute(
                "SELECT ts, spread_bp, depth_json FROM l2 WHERE coin=? ORDER BY ts", (c,))]
            if not rows:
                print(f"  {c}: 无快照(先跑 record)")
                continue
            spreads = sorted(r["spread_bp"] for r in rows)
            med_spread = spreads[len(spreads) // 2]
            print(f"\n【{c}】快照 {len(rows):,} 条 | 中位价差 {med_spread:.2f}bp "
                  f"| 覆盖 {(rows[-1]['ts']-rows[0]['ts'])/3600:.1f} 小时")
            print(f"  {'距离':>6}{'中位累计深度(USD)':>20}{'$10k 单占比':>14}")
            meds = {}
            for bp in DIST_BP:
                vals = sorted(json.loads(r["depth_json"]).get(str(bp), 0) for r in rows)
                meds[bp] = vals[len(vals) // 2]
                frac = 10_000 / meds[bp] * 100 if meds[bp] > 0 else float("inf")
                print(f"  {bp:>4}bp{meds[bp]:>20,.0f}{frac:>13.3f}%")
            # 若各档深度完全相同 → 订单簿层数太浅,最远一档已覆盖全部可见量
            if len({round(v) for v in meds.values()}) == 1:
                print(f"  ⚠️ 各距离深度相同 → 快照只有约 20 档(≈{max(DIST_BP)/1:.0f}bp 以内),"
                      "**测不到 −100bp 的队列深度**")
                print("     结论:L2 快照只能用于①价差 ②可见深度/容量;"
                      "深挂队列必须用 testnet 真实挂单校准。")

    if from_ledger:
        from . import ledger, registry
        s = next((x for x in registry.all() if x["name"] == from_ledger), None)
        if s:
            m = ledger.summary(s["id"])
            print(f"\n【实测成交率反推】{from_ledger}:信号 {m['signals']} → 成交 {m['trades']}"
                  f" = {m['fill_rate']*100:.1f}%")
            # 注意:这个比值是"成交/信号档",不是"成交/可成交机会"——
            # 大多数信号档根本没有下探到 −100bp,所以不能直接当 fill_rate 用。
            print("  ⚠️ 该比值≠队列成交率(多数信号档未下探到挂单价)。"
                  "真实的 fill_rate 只能靠 testnet 挂单测,或等实盘积累 ≥50 笔后反推。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["record", "report"])
    ap.add_argument("--coins", default="BTC,ETH,SOL")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--from-ledger", default=None)
    a = ap.parse_args()
    coins = [c.strip().upper() for c in a.coins.split(",") if c.strip()]
    if a.cmd == "record":
        record(coins, a.interval)
    else:
        report(coins, a.from_ledger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
