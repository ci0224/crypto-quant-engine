"""Robinhood 适配器(**非官方** API,需显式开关 + 风险确认)。

⚠️ 重要事实(2026-11-12 实测):
   · docs.robinhood.com 只提供 /crypto/trading/,**股票没有官方 API**
   · api.robinhood.com 是逆向接口:要账号密码 + MFA,随时可能变
   · 自动化交易可能违反 ToS,存在封号风险

因此本适配器默认**禁用**,必须同时满足:
   ROBINHOOD_UNOFFICIAL=1        (明确知道自己用的是非官方接口)
   ROBINHOOD_I_ACCEPT_RISK=1     (明确接受封号/违约风险)
   ROBINHOOD_USER / ROBINHOOD_PASS / [ROBINHOOD_MFA_SECRET]
"""
from __future__ import annotations

import os
import sys

from .broker import Broker
from .ibkr import BrokerUnavailable


def _gate():
    if os.environ.get("ROBINHOOD_UNOFFICIAL") != "1":
        raise BrokerUnavailable(
            "Robinhood 没有官方股票 API。若确定要用非官方接口,设置 ROBINHOOD_UNOFFICIAL=1")
    if os.environ.get("ROBINHOOD_I_ACCEPT_RISK") != "1":
        raise BrokerUnavailable(
            "非官方接口有封号/违约风险。确认后设置 ROBINHOOD_I_ACCEPT_RISK=1")
    for k in ("ROBINHOOD_USER", "ROBINHOOD_PASS"):
        if not os.environ.get(k):
            raise BrokerUnavailable(f"缺环境变量 {k}")


class RobinhoodBroker(Broker):
    name = "robinhood"

    def __init__(self):
        _gate()
        try:
            import robin_stocks.robinhood as rh
        except Exception as e:
            raise BrokerUnavailable(f"缺 robin_stocks({e})")
        self.rh = rh
        print("[robinhood] ⚠️ 使用非官方接口,已按你的显式确认继续", file=sys.stderr)
        try:
            rh.login(os.environ["ROBINHOOD_USER"], os.environ["ROBINHOOD_PASS"],
                     store_session=True, mfa_code=os.environ.get("ROBINHOOD_MFA_SECRET"))
        except Exception as e:
            raise BrokerUnavailable(f"Robinhood 登录失败:{e}(可能需要 MFA 验证码)")

    def account(self) -> dict:
        p = self.rh.account.build_user_profile()
        return {"equity": float(p.get("equity") or 0),
                "cash": float(p.get("cash") or 0),
                "buying_power": float(p.get("buying_power") or 0)}

    def position(self, sym: str) -> float:
        h = self.rh.account.build_holdings()
        return float((h.get(sym) or {}).get("quantity") or 0)

    def quote(self, sym: str) -> dict:
        px = float(self.rh.stocks.get_latest_price(sym)[0] or 0)
        return {"last": px, "bid": px, "ask": px}

    def buy(self, sym, qty, limit=None):
        return self._do(sym, qty, "buy")

    def sell(self, sym, qty, limit=None):
        return self._do(sym, qty, "sell")

    def _do(self, sym, qty, side):
        q = round(abs(float(qty)), 6)
        if q < 1e-6:
            return {"ok": False, "err": "数量为 0"}
        fn = (self.rh.orders.order_buy_market if side == "buy"
              else self.rh.orders.order_sell_market)
        r = fn(sym, q, timeInForce="gfd")
        px = self.quote(sym)["last"]
        return {"ok": bool(r and r.get("id")), "oid": (r or {}).get("id"),
                "filled_qty": q, "avg_px": px,
                "err": None if (r or {}).get("id") else str(r)[:120]}

    def open_orders(self) -> list:
        try:
            return self.rh.orders.get_all_open_stock_orders()
        except Exception:
            return []
