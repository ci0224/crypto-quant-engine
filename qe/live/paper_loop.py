"""自动纸交易循环:把处于 paper 状态的策略,按实时行情跑模拟盘。

    python -m qe.live.paper_loop --once      # 跑一轮(用于 cron / 调试)
    python -m qe.live.paper_loop --interval 300

两类策略分开处理:
  · 权重型(trend / xsec_* / funding_carry):算目标权重 → 与纸面持仓对比 → 按 mid±成本成交
  · 事件型(deep_limit):扫最近 5m K 线,识别 Boll<low 信号 → 模拟 −100bp 限价单 → 跟踪成交

状态写在 data/paper/<sid>.json(不碰 DB,DB 只存汇总统计)。
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .. import pipeline, registry
from ..config import Config, Costs
from ..data import hyperliquid as hl
from ..strategies import library as S

STATE = Path(__file__).resolve().parent.parent.parent / "data" / "paper"
FEE_BP = {"deep_limit": 3.0}      # maker 往返
DEFAULT_FEE = 11.0                # taker 往返


def _state(sid):
    STATE.mkdir(parents=True, exist_ok=True)
    p = STATE / f"{sid}.json"
    if p.exists():
        return json.loads(p.read_text()), p
    return {"eq": 10000.0, "peak": 10000.0, "trades": 0, "pnl_bp": [], "pos": {},
            "orders": {}, "day0": time.time()}, p


def _save(st, p):
    p.write_text(json.dumps(st, ensure_ascii=False))


def _candles(coin, interval="1d", n=400):
    """返回收盘价 Series(index=UTC 时间)。hl.candles(coin, interval, days)"""
    days = n if interval.endswith("d") else max(1, int(n / 288) + 1)   # 5m: 288 根/天
    df = hl.candles(coin, interval, days=max(days, 2))
    if df is None or df.empty:
        return pd.Series(dtype=float)
    return df["close"].astype(float)


def _weights(stgy, coins):
    """按家族算目标权重(只支持权重型)。"""
    px = pd.concat({c: _candles(c) for c in coins}, axis=1).ffill().dropna(how="all")
    if px.empty or len(px) < 120:
        return None
    fam = stgy["family"]
    if fam == "trend":
        sp = stgy["spec"] or {}
        return S.trend_following(px, fast=int(sp.get("fast", 10)), slow=int(sp.get("slow", 50)),
                                 vol_target=float(sp.get("vol_target", 0.25))).iloc[-1]
    if fam == "xsec_momentum":
        return S.xsec_momentum(px).iloc[-1]
    return None


def tick(stgy):
    sid, name, fam = stgy["id"], stgy["name"], stgy["family"]
    st, path = _state(sid)
    spec = stgy["spec"] or {}
    lev = float(spec.get("leverage") or 1.0)
    fee = FEE_BP.get(fam, DEFAULT_FEE)

    if fam in ("trend", "xsec_momentum"):
        w = _weights(stgy, stgy["symbols"])
        if w is None:
            return None
        mids = hl.all_mids()
        eq = st["eq"]
        # 目标名义 = 权重 × 权益 × 杠杆
        for c in stgy["symbols"]:
            tgt_w = float(w.get(c, 0.0)) * lev
            px = mids.get(c)
            if not px:
                continue
            cur_notional = st["pos"].get(c, 0.0) * px
            tgt_notional = tgt_w * eq
            delta = tgt_notional - cur_notional
            if abs(delta) < eq * 0.01:     # 1% 阈值,避免噪声换手
                continue
            fee_bp = fee * abs(delta) / eq
            st["eq"] -= eq * fee_bp / 1e4
            st["pos"][c] = st["pos"].get(c, 0.0) + delta / px
            st["trades"] += 1
            st["pnl_bp"].append(-fee_bp)
        st["peak"] = max(st["peak"], st["eq"])

    elif fam == "deep_limit":
        # 事件型:扫最近 5m 线,找 Boll<low;若在 08-20 UTC 且未挂单 → 记一笔模拟限价单
        for c in stgy["symbols"]:
            px5 = _candles(c, "5m", 20)
            if len(px5) < 60:
                continue
            ma = px5.rolling(20).mean()
            sd = px5.rolling(20).std(ddof=0)
            low_band = ma - 2 * sd
            last_t = px5.index[-1]
            key = f"{c}:{last_t.isoformat()}"
            sig = px5.iloc[-1] < low_band.iloc[-1]
            hour = last_t.hour
            if sig and 8 <= hour < 20 and key not in st["orders"]:
                ref = float(px5.iloc[-1])
                st["orders"][key] = {"coin": c, "ref": ref, "limit": ref * (1 - 0.0100),
                                     "target": ref * (1 - 0.0100) * (1 + 0.0120),
                                     "ts": time.time(), "filled": False}
        # 用最新价检查成交/止盈
        mids = hl.all_mids()
        for k, o in list(st["orders"].items()):
            px = mids.get(o["coin"])
            if not px:
                continue
            if not o["filled"] and px <= o["limit"]:
                o["filled"] = True
                o["fill_ts"] = time.time()
            if o["filled"]:
                if px >= o["target"]:
                    st["pnl_bp"].append(120 - FEE_BP["deep_limit"])
                    st["trades"] += 1
                    st["orders"].pop(k)
                elif time.time() - o.get("fill_ts", time.time()) > 55 * 60:
                    pnl = (px / o["limit"] - 1) * 1e4 - FEE_BP["deep_limit"]
                    st["pnl_bp"].append(pnl)
                    st["trades"] += 1
                    st["orders"].pop(k)
        # 清理 6 小时前的未成交挂单
        for k, o in list(st["orders"].items()):
            if not o["filled"] and time.time() - o["ts"] > 6 * 3600:
                st["orders"].pop(k)
        st["peak"] = max(st["peak"], st["eq"])

    else:
        return None

    _save(st, path)
    days = (time.time() - st["day0"]) / 86400
    dd = (st["eq"] / st["peak"] - 1) if st["peak"] else 0.0
    stats = {"days": round(days, 2), "trades": st["trades"],
             "mean_bp": round(float(np.mean(st["pnl_bp"])), 2) if st["pnl_bp"] else 0.0,
             "equity": round(st["eq"], 2), "max_dd": round(dd, 4)}
    pipeline.paper_tick(sid, stats)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=300)
    a = ap.parse_args()
    while True:
        rows = [s for s in registry.all() if s["state"] == "paper"]
        for s in rows:
            try:
                st = tick(s)
                if st:
                    print(f"[paper] {s['name']:18s} days={st['days']:.1f} trades={st['trades']} "
                          f"mean={st['mean_bp']}bp eq={st['equity']}")
            except Exception as e:
                print(f"[paper] {s['name']} 失败: {type(e).__name__}: {e}")
        if a.once:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
