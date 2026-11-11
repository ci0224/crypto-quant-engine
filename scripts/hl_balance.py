#!/usr/bin/env python3
"""查任一地址在 Hyperliquid 的资金分布(四个地方都要查,钱不会凭空消失)。

    python scripts/hl_balance.py 0x你的地址
"""
from __future__ import annotations

import json
import sys
import urllib.request

API = "https://api.hyperliquid.xyz/info"


def post(payload: dict):
    req = urllib.request.Request(API, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def main(addr: str) -> None:
    print(f"地址 {addr}\n")

    perps = post({"type": "clearinghouseState", "user": addr})
    if "marginSummary" in perps:
        ms = perps["marginSummary"]
        print(f"① 永续账户 (Perps)     : {float(ms['accountValue']):>12,.2f} USDC")
        print(f"   持仓名义            : {float(ms['totalNtlPos']):>12,.2f}")
        print(f"   已用保证金          : {float(ms['totalMarginUsed']):>12,.2f}")

    spot = post({"type": "spotClearinghouseState", "user": addr})
    bals = spot.get("balances") or []
    tot = 0.0
    for b in bals:
        if b["coin"] == "USDC":
            tot = float(b["total"])
    print(f"\n② 现货账户 (Spot)      : {tot:>12,.2f} USDC")
    if len(bals) > 1:
        others = ", ".join(f"{b['coin']} {b['total']}" for b in bals if b["coin"] != "USDC")[:80]
        print(f"   其它币种            : {others}")

    vaults = post({"type": "userVaultEquities", "user": addr})
    vtot = sum(float(v.get("equity", 0)) for v in vaults) if isinstance(vaults, list) else 0.0
    print(f"\n③ Vault 里的钱         : {vtot:>12,.2f} USDC")
    if isinstance(vaults, list) and vaults:
        for v in vaults[:3]:
            print(f"   vault {v.get('vaultAddress','')[:12]}… : {float(v.get('equity',0)):,.2f}")

    st = post({"type": "delegatorSummary", "user": addr})
    if isinstance(st, dict) and "delegated" in st:
        print(f"\n④ 质押 (Staking)       : {float(st['delegated']):>12,.2f} HYPE")

    total = 0.0
    if "marginSummary" in perps:
        total += float(perps["marginSummary"]["accountValue"])
    total += tot + vtot
    print(f"\n{'='*44}\n合计(USDC)           : {total:>12,.2f}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python scripts/hl_balance.py 0x地址")
        sys.exit(1)
    main(sys.argv[1])
