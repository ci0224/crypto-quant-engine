#!/usr/bin/env python3
"""Testnet 冒烟测试:真实挂单 → 观察 → 撤单/平仓,验证全链路。

    python scripts/testnet_smoke.py            # 挂单并观察 90 秒
    python scripts/testnet_smoke.py --cancel   # 清掉所有挂单
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

URL = "https://api.hyperliquid-testnet.xyz"
MASTER = "0x29ac98f5b4115dd7dd94abf8c46d6b3c1c519061"
ENV = pathlib.Path("/home/ubuntu/.hermes-secrets/hl_api_wallet.env")


def client() -> tuple[Exchange, Info]:
    env = dict(l.split("=", 1) for l in ENV.read_text().splitlines() if "=" in l)
    ex = Exchange(Account.from_key(env["HL_API_KEY"]), URL, account_address=MASTER)
    return ex, Info(URL, skip_ws=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cancel", action="store_true")
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--notional", type=float, default=15.0)
    ap.add_argument("--offset-bp", type=float, default=100.0)
    ap.add_argument("--wait", type=int, default=90)
    a = ap.parse_args()

    ex, info = client()
    state = info.user_state(MASTER)
    eq = float(state["marginSummary"]["accountValue"])
    print(f"主账户 {MASTER[:10]}… | 永续权益 {eq:.2f} USDC")

    oo = info.open_orders(MASTER)
    if a.cancel:
        for o in oo:
            print("  撤单", o["coin"], o["oid"], ex.cancel(o["coin"], o["oid"]))
        return 0
    if eq < 10:
        print("⚠️ 永续账户没钱,请先在 testnet 把 Spot → Perps 划转")
        return 1

    mid = float(info.all_mids()[a.coin])
    tick = 1 if a.coin == "BTC" else 0.01
    px = round(mid * (1 - a.offset_bp / 1e4) / tick) * tick
    sz = round(a.notional / px, 6)
    print(f"挂 {a.coin} ALO 买单: {sz} @ {px} (mid {mid:.2f}, -{a.offset_bp}bp, ${a.notional})")
    res = ex.order(a.coin, True, sz, px, {"limit": {"tif": "Alo"}})
    print("  返回:", str(res)[:220])

    placed = time.time()
    for i in range(a.wait // 15):
        time.sleep(15)
        cur = info.open_orders(MASTER)
        mine = [o for o in cur if abs(float(o["limitPx"]) - px) < 1e-6]
        fills = info.user_fills(MASTER)
        print(f"  +{(time.time()-placed):.0f}s 挂单 {len(mine)} 笔 | 累计成交 {len(fills)} 笔")
        if not mine:
            print("  → 订单已不在挂单列表:成交或被撤")
            break
    for o in info.open_orders(MASTER):
        print("  收尾撤单:", ex.cancel(o["coin"], o["oid"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
