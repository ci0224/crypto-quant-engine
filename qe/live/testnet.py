"""Hyperliquid Testnet 执行器:真实撮合、真实队列、假钱。

与本地纸交易的本质区别:
  本地纸交易 = 我们自己在 Python 里"模拟成交"(fill_rate 是拍的)
  testnet    = Hyperliquid 真实撮合引擎,限价单真的进队列,成交与否由市场决定

所以 testnet 是 stage-2 的正确载体:代码与实盘完全一致,只换 endpoint 和私钥。
"""
from __future__ import annotations

import os
import pathlib
import time

TESTNET_URL = "https://api.hyperliquid-testnet.xyz"


def _load_key() -> tuple[str, str]:
    p = pathlib.Path("/home/ubuntu/.hermes-secrets/hl_testnet.env")
    env = dict(l.split("=", 1) for l in p.read_text().splitlines() if "=" in l)
    return env["HL_TESTNET_ADDRESS"], env["HL_TESTNET_KEY"]


def info():
    from hyperliquid.info import Info
    return Info(TESTNET_URL, skip_ws=True)


def exchange():
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    addr, key = _load_key()
    return Exchange(Account.from_key(key), TESTNET_URL, account_address=addr)


def address() -> str:
    return _load_key()[0]


def mid(coin: str) -> float:
    return float(info().all_mids()[coin])


def equity() -> float:
    st = info().user_state(address())
    return float(st["marginSummary"]["accountValue"])


def place_alo(coin: str, is_buy: bool, notional_usd: float, offset_bp: float):
    """挂 ALO(post-only)限价单,距离 mid offset_bp(买=下方,卖=上方)。"""
    px_mid = mid(coin)
    px = px_mid * (1 - offset_bp / 1e4) if is_buy else px_mid * (1 + offset_bp / 1e4)
    sz = round(notional_usd / px, 6)
    ex = exchange()
    res = ex.order(coin, is_buy, sz, round(px, 6),
                   {"limit": {"tif": "Alo"}}, reduce_only=False)
    return {"px": px, "sz": sz, "res": res}


def open_orders(coin: str | None = None):
    oo = info().open_orders(address())
    return [o for o in oo if coin is None or o["coin"] == coin]


def fills():
    return info().user_fills(address())
