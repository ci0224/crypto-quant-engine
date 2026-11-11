#!/usr/bin/env python3
"""Testnet 成交率探针:真挂单,真队列,测"纸交易 100% 成交"到底虚高多少。

    python scripts/testnet_probe.py --dry      # 只找信号不挂单
    python scripts/testnet_probe.py            # 真挂单(需 testnet 资金)

记录到 data/testnet_probe.json:
    {coin, sig_ts, limit_px, placed_ts, filled_ts, fill_px, waited_min, outcome}
outcome ∈ filled / expired / cancelled
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qe.data import hyperliquid as hl
from qe.live import testnet as T
from qe.strict.data import boll_state

LOG = ROOT / "data" / "testnet_probe.json"
OFFSET_BP = 100.0
NOTIONAL = 12.0
ENTRY_WINDOW_MIN = 5


def _load() -> list:
    if LOG.exists():
        return json.loads(LOG.read_text())
    return []


def _save(rows: list) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(rows, indent=1))


def scan(dry: bool) -> int:
    rows = _load()
    made = 0
    for coin in ("BTC", "ETH", "SOL"):
        try:
            b5 = hl.candles(coin, "5m", 2)
        except Exception as e:  # noqa: BLE001
            print(f"  {coin}: 取K线失败 {e}")
            continue
        if b5 is None or len(b5) < 30:
            continue
        st = boll_state(b5["close"]).iloc[-1]
        hour = b5.index[-1].hour
        if st <= 0 or not (8 <= hour < 20):
            continue
        sig_ts = int(b5.index[-1].timestamp())
        if any(r["coin"] == coin and r["sig_ts"] == sig_ts for r in rows):
            continue
        px = T.mid(coin)
        limit = px * (1 - OFFSET_BP / 1e4)
        row = {"coin": coin, "sig_ts": sig_ts, "limit_px": limit,
               "placed_ts": int(time.time()), "outcome": "pending"}
        if not dry:
            try:
                res = T.place_alo(coin, True, NOTIONAL, OFFSET_BP)
                row["order_res"] = str(res["res"])[:160]
            except Exception as e:  # noqa: BLE001
                row["order_res"] = f"ERR {e}"
        rows.append(row)
        made += 1
        print(f"  {coin} 信号 @ {b5.index[-1]} 挂 {limit:.2f}{' (dry)' if dry else ''}")
    if made:
        _save(rows)
    return made


def settle() -> None:
    """检查挂单是否成交,更新 outcome。"""
    rows = _load()
    changed = False
    for r in rows:
        if r["outcome"] != "pending":
            continue
        waited = (time.time() - r["placed_ts"]) / 60
        if waited >= ENTRY_WINDOW_MIN:
            oo = T.open_orders(r["coin"])
            still = any(abs(o["limitPx"] - r["limit_px"]) / r["limit_px"] < 1e-4 for o in oo)
            r["outcome"] = "expired" if still else "filled_or_unknown"
            r["waited_min"] = round(waited, 1)
            changed = True
    if changed:
        _save(rows)


def report() -> None:
    rows = [r for r in _load() if r["outcome"] != "pending"]
    if not rows:
        print("暂无样本")
        return
    filled = sum(1 for r in rows if r["outcome"] == "filled_or_unknown")
    print(f"样本 {len(rows)} | 成交 {filled} | 实测成交率 {filled / len(rows) * 100:.1f}%")
    print("(对照:纸交易假设 100%,严格框架假设 85%,你 §15 HL 重放 69%)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        report()
    else:
        print(f"钱包 {T.address()} | 权益 {T.equity()} USDC")
        n = scan(a.dry)
        settle()
        print(f"本轮新增信号 {n}")
