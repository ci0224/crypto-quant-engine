#!/usr/bin/env python3
"""查地址在 Arbitrum 的 ETH / USDC 余额(用于判断钱在哪)。"""
from __future__ import annotations

import json
import sys
import urllib.request

RPCS = [
    "https://arbitrum.llamarpc.com",
    "https://arb1.arbitrum.io/rpc",
    "https://arbitrum-one.publicnode.com",
]
RPC = RPCS[0]
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120", "Content-Type": "application/json"}
TOKENS = {
    "USDC (native)": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    "USDC.e (bridged)": "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8",
    "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
}


def rpc(method: str, params: list):
    req = urllib.request.Request(
        RPC,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers=UA,
    )
    try:
        return json.loads(urllib.request.urlopen(req, timeout=20).read()).get("result")
    except Exception:
        for alt in RPCS[1:]:
            try:
                req2 = urllib.request.Request(alt, data=req.data, headers=UA)
                return json.loads(urllib.request.urlopen(req2, timeout=20).read()).get("result")
            except Exception:
                continue
        return None


def main(addr: str) -> None:
    eth = rpc("eth_getBalance", [addr, "latest"])
    print(f"地址 {addr}")
    print(f"  ETH (Arbitrum) : {int(eth, 16) / 1e18:>12,.6f}")
    for name, token in TOKENS.items():
        data = "0x70a08231000000000000000000000000" + addr[2:].lower()
        raw = rpc("eth_call", [{"to": token, "data": data}, "latest"])
        val = int(raw, 16) / 1e6 if raw and raw != "0x" else 0.0
        print(f"  {name:<15}: {val:>12,.2f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "0xda1b07b9d82db70a9e82c59276316bfd314cac94")
