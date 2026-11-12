"""IBKR 适配器(官方 API,经 TWS / IB Gateway)。

前置条件(需要你做,我做不了):
  1. 在本机或可访问的机器上跑 IB Gateway(或 TWS)
  2. Gateway 设置里勾选 API → Enable ActiveX and Socket Clients
  3. 记下端口:4002 = 纸交易,4001 = 实盘
  4. 填环境变量:IBKR_HOST IBKR_PORT IBKR_CLIENT_ID IBKR_ACCOUNT

未配置时不会静默失败,而是抛 BrokerUnavailable 并说明缺什么。
"""
from __future__ import annotations

import os
from .broker import Broker


class BrokerUnavailable(RuntimeError):
    pass


class IBKRBroker(Broker):
    name = "ibkr"

    def __init__(self):
        try:
            from ib_async import IB
        except Exception as e:
            raise BrokerUnavailable(
                f"缺 ib_async({e})。装法:uv pip install --python .venv/bin/python ib_async")
        self.host = os.environ.get("IBKR_HOST", "127.0.0.1")
        self.port = int(os.environ.get("IBKR_PORT", "4002"))
        self.cid = int(os.environ.get("IBKR_CLIENT_ID", "7"))
        self.acct = os.environ.get("IBKR_ACCOUNT", "")
        self.ib = IB()
        try:
            self.ib.connect(self.host, self.port, clientId=self.cid, timeout=15)
        except Exception as e:
            raise BrokerUnavailable(
                f"连不上 IB Gateway {self.host}:{self.port}({e})。"
                "检查:Gateway 是否在跑 / API 是否启用 / 端口对不对(4002 纸、4001 实盘)")


    # ---------- 只读 ----------
    def account(self) -> dict:
        rows = self.ib.accountSummary()
        def pick(tag):
            for r in rows:
                if r.tag == tag and (not self.acct or r.account == self.acct):
                    return float(r.value or 0)
            return 0.0
        return {"equity": pick("NetLiquidation"), "cash": pick("TotalCashValue"),
                "buying_power": pick("BuyingPower")}

    def position(self, sym: str) -> float:
        for p in self.ib.positions():
            if p.contract.symbol == sym and (not self.acct or p.account == self.acct):
                return float(p.position)
        return 0.0

    def quote(self, sym: str) -> dict:
        from ib_async import Stock
        c = Stock(sym, "SMART", "USD")
        self.ib.qualifyContracts(c)
        t = self.ib.reqTickers(c)[0]
        last = t.last or t.close or 0
        return {"last": float(last), "bid": float(t.bid or 0), "ask": float(t.ask or 0)}

    def open_orders(self) -> list:
        out = []
        for o in self.ib.openTrades():
            if o.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled"):
                continue
            out.append({"oid": o.order.orderId, "sym": o.contract.symbol,
                        "qty": float(o.order.totalQuantity), "side": o.order.action.lower(),
                        "px": getattr(o.order, "lmtPrice", 0) or 0,
                        "status": o.orderStatus.status})
        return out


    # ---------- 下单 ----------
    def _order(self, sym, qty, side, limit):
        from ib_async import Stock, MarketOrder, LimitOrder
        c = Stock(sym, "SMART", "USD")
        self.ib.qualifyContracts(c)
        q = abs(float(qty))
        o = LimitOrder(side.upper(), q, float(limit)) if limit else MarketOrder(side.upper(), q)
        if self.acct:
            o.account = self.acct
        o.tif = "DAY"
        t = self.ib.placeOrder(c, o)
        for _ in range(30):
            self.ib.sleep(0.5)
            if t.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                break
        st = t.orderStatus.status
        return {"ok": st in ("Filled", "Submitted", "PreSubmitted"),
                "oid": t.order.orderId, "status": st,
                "filled_qty": float(t.orderStatus.filled or 0),
                "avg_px": float(t.orderStatus.avgFillPrice or 0),
                "err": None if st != "Inactive" else str(t.log[-1]) if t.log else "Inactive"}

    def buy(self, sym, qty, limit=None):
        return self._order(sym, qty, "buy", limit)

    def sell(self, sym, qty, limit=None):
        return self._order(sym, qty, "sell", limit)

    def cancel(self, oid):
        for o in self.ib.openTrades():
            if o.order.orderId == oid:
                self.ib.cancelOrder(o.order)
                return True
        return False

    def close(self):
        try:
            self.ib.disconnect()
        except Exception:
            pass
