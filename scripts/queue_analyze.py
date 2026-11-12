#!/usr/bin/env python3
"""用录到的 L2 数据估算「挂单队列位置」。

核心问题:我们挂在 mid×(1−100bp),成交时前面排了多少?

HL 只给 20 档(~3bp),所以做法是:
  ① 从 l2_full 找出**我们价位出现在盘口**的那些快照(价格逼近时才会出现)
  ② 该价位上的 sz 就是"前面排的量";sz 随时间下降 = 被吃掉
  ③ 用我们自己的单笔规模 / 该价位剩余量 → 成交概率的粗估

    python scripts/queue_analyze.py --coin BTC --price 73000
    python scripts/queue_analyze.py --coin BTC --auto     # 用 mid-100bp 当目标价
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "l2.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--price", type=float, default=None)
    ap.add_argument("--auto", action="store_true", help="用最新 mid×(1−100bp) 作为目标价")
    ap.add_argument("--my-usd", type=float, default=20.0, help="我的单笔名义(美元)")
    a = ap.parse_args()

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    if a.price is None:
        r = c.execute("SELECT mid FROM l2_band WHERE coin=? ORDER BY ts_ms DESC LIMIT 1",
                      (a.coin,)).fetchone()
        if not r:
            print("没有数据,先跑 qe-l2 服务")
            return 1
        a.price = r["mid"] * 0.99
        print(f"目标价(自动)= {a.price:,.2f}")

    rows = c.execute(
        "SELECT ts_ms, px, sz, n FROM l2_full WHERE coin=? AND side='bid' "
        "AND px BETWEEN ? AND ? ORDER BY ts_ms", (a.coin, a.price * 0.999, a.price * 1.001)).fetchall()
    if not rows:
        print(f"录到的数据里,{a.coin} 买盘从未触及 {a.price:,.2f} —— 说明价格还没跌到那个位置。")
        print("（这本身是有用信息：我们的挂单在可见窗口之外，队列无从估算）")
        return 0

    print(f"\n{len(rows)} 个快照里, {a.price:,.2f} 附近有挂单:")
    first = rows[0]
    last = rows[-1]
    span = (last["ts_ms"] - first["ts_ms"]) / 1000
    print(f"  时间跨度 {span:.1f}s | 首次看到 sz={first['sz']:.6f} ({first['sz']*a.price:,.0f} USD) "
          f"| 最后 sz={last['sz']:.6f} ({last['sz']*a.price:,.0f} USD)")

    # 队列存活曲线:每个快照该价位剩余量
    seq = [(r["ts_ms"], r["sz"] * a.price) for r in rows]
    print("  该价位剩余美元量(抽样 8 个点):")
    step = max(1, len(seq) // 8)
    for ts, usd in seq[::step]:
        print(f"    +{(ts - first['ts_ms'])/1000:6.1f}s  ${usd:>12,.0f}")

    # 成交概率粗估:如果把我的单子排在这个价位队尾
    median_usd = sorted(x[1] for x in seq)[len(seq) // 2]
    ratio = a.my_usd / max(median_usd, 1e-9)
    print(f"\n  该价位中位剩余 ${median_usd:,.0f} | 我的单笔 ${a.my_usd:,.0f} = 队列的 {ratio*100:.2f}%")
    if ratio < 0.02:
        print("  → 排队压力很小:价格一旦触到,大概率成交")
    elif ratio < 0.2:
        print("  → 需要该价位被吃掉约 {:.0f}% 才轮到我".format(ratio * 100))
    else:
        print("  → 我的单子占比过大,可能需要价格**穿过**该价位才能成交")
    return 0


if __name__ == "__main__":
    sys.exit(main())
