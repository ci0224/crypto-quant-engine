#!/usr/bin/env python3
"""多链余额速查:定位"我的 100 多块"到底在哪条链。"""
from __future__ import annotations

import json
import sys
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120",
      "Content-Type": "application/json"}

CHAINS = {
    "Ethereum": ("https://ethereum.publicnode.com", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
    "Arbitrum": ("https://arbitrum.llamarpc.com", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6),
    "Base": ("https://base.publicnode.com", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6),
    "BSC": ("https://bsc.publicnode.com", "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", 18),
    "Polygon": ("https://polygon-bor.publicnode.com", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6),
    "Optimism": ("https://optimism.publicnode.com", "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", 6),
}


def call(url: str, payload: dict):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=UA)
    return json.loads(urllib.request.urlopen(req, timeout=15).read()).get("result")


def main(addr: str) -> None:
    print(f"地址 {addr}\n")
    print(f"{'链':<10} {'原生币':>18} {'USDC':>14}")
    for name, (url, token, dec) in CHAINS.items():
        try:
            native = int(call(url, {"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
                                    "params": [addr, "latest"]}), 16) / 1e18
        except Exception:
            native = float("nan")
        try:
            data = "0x70a08231000000000000000000000000" + addr[2:].lower()
            raw = call(url, {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                             "params": [{"to": token, "data": data}, "latest"]})
            usdc = int(raw, 16) / (10 ** dec) if raw and raw != "0x" else 0.0
        except Exception:
            usdc = float("nan")
        print(f"{name:<10} {native:>18,.5f} {usdc:>14,.2f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "0xda1b07b9d82db70a9e82c59276316bfd314cac94")
