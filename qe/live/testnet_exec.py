"""Testnet 执行器:由 pipeline 中 venue=testnet 的策略调用(不是 cron)。

与 PaperExecutor 的区别:
  PaperExecutor  —— 我们的代码模拟"会不会成交"
  TestnetExec    —— 真的挂进 HL 撮合引擎,成交与否由市场和队列决定

所有动作写入 ledger(source="testnet"),供对账器与回测比对。
"""
from __future__ import annotations

import pathlib
from contextlib import contextmanager
import time

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

TESTNET = "https://api.hyperliquid-testnet.xyz"
ENVF = pathlib.Path("/home/ubuntu/.hermes-secrets/hl_api_wallet.env")
MASTER = "0x29ac98f5b4115dd7dd94abf8c46d6b3c1c519061"


def _env() -> dict:
    return dict(l.split("=", 1) for l in ENVF.read_text().splitlines() if "=" in l)



_LCK = pathlib.Path("/tmp/qe_hl_order.lock")


@contextmanager
def _order_lock():
    """跨进程互斥:HL 的 nonce 必须严格递增,两个进程同时下单会撞。

    服务进程与手工测试脚本都会下单 → 用文件锁串行化,避免 "nonce too low"。
    """
    import fcntl
    _LCK.touch(exist_ok=True)
    with open(_LCK, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class TestnetExec:
    def __init__(self) -> None:
        self.ex = Exchange(Account.from_key(_env()["HL_API_KEY"]), TESTNET,
                           account_address=MASTER)
        self.info = Info(TESTNET, skip_ws=True)
        self._dec = {}
        try:
            for a in self.info.meta()["universe"]:
                self._dec[a["name"]] = int(a.get("szDecimals", 3))
        except Exception:
            pass

    def mid(self, coin: str) -> float:
        return float(self.info.all_mids()[coin])

    def equity(self) -> float:
        st = self.info.user_state(MASTER)
        spot = self.info.spot_user_state(MASTER)
        s = sum(float(b["total"]) for b in (spot.get("balances") or []) if b["coin"] == "USDC")
        return float(st["marginSummary"]["accountValue"]) + s

    def px_dec(self, coin: str) -> int:
        """HL 价格精度规则:小数位 = 6 - szDecimals - 1(BTC 0 / ETH 1 / SOL 3)。"""
        return max(0, 6 - self._dec.get(coin, 3) - 1)

    def place_alo(self, coin: str, is_buy: bool, notional: float, offset_bp: float):
        mid = self.mid(coin)
        px = mid * (1 - offset_bp / 1e4) if is_buy else mid * (1 + offset_bp / 1e4)
        # BTC 的 tick size 是 1(整数价),其它保留 1-2 位
        px = round(px, self.px_dec(coin))
        dec = self._dec.get(coin, 3)
        sz = round(notional / px, dec)
        sz = max(sz, 10 ** (-dec))
        with _order_lock():
            res = self.ex.order(coin, is_buy, sz, px, {"limit": {"tif": "Alo"}})
        oid = None
        try:
            oid = res["response"]["data"]["statuses"][0]["resting"]["oid"]
        except (KeyError, IndexError, TypeError):
            pass
        return {"px": px, "sz": sz, "oid": oid, "raw": res}

    def place_tp(self, coin: str, sz: float, tp_px: float):
        px = round(tp_px, self.px_dec(coin))
        with _order_lock():
            res = self.ex.order(coin, False, round(sz, self._dec.get(coin, 3)), px,
                                {"limit": {"tif": "Alo"}})
        oid = None
        try:
            oid = res["response"]["data"]["statuses"][0]["resting"]["oid"]
        except (KeyError, IndexError, TypeError):
            pass
        return {"oid": oid, "raw": res}


    def place_market(self, coin: str, is_buy: bool, notional: float):
        """市价单(HL 用激进价 + IOC 模拟市价)。返回成交价/数量。"""
        mid = self.mid(coin)
        px = mid * (1.005 if is_buy else 0.995)
        sz = notional / mid
        sz = round(sz, self._dec.get(coin, 3))
        px = round(px, 1 if coin == "BTC" else 2)
        with _order_lock():
            r = self.ex.order(coin, is_buy, sz, px, {"limit": {"tif": "Ioc"}}, reduce_only=False)
        st = (r.get("response") or {}).get("data", {}).get("statuses", [{}])[0]
        filled = st.get("filled") or {}
        return {"px": float(filled.get("avgPx") or px), "sz": float(filled.get("totalSz") or sz),
                "oid": filled.get("oid"), "raw": r}

    def cancel(self, coin: str, oid: int) -> dict:
        try:
            with _order_lock():
                return self.ex.cancel(coin, oid)
        except Exception as e:  # noqa: BLE001
            return {"err": str(e)}

    def open_orders(self) -> list:
        return self.info.open_orders(MASTER)

    def fills(self) -> list:
        return self.info.user_fills(MASTER)

    def position(self, coin: str) -> float:
        st = self.info.user_state(MASTER)
        for p in st.get("assetPositions") or []:
            if p["position"]["coin"] == coin:
                return float(p["position"]["szi"])
        return 0.0

    def close_market(self, coin: str, sz: float) -> dict:
        return self.ex.market_close(coin, abs(sz))
