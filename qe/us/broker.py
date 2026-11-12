"""券商抽象层:同一接口,三种实现(Paper / IBKR / Robinhood)。

为什么抽象:策略代码不应该知道钱在哪里。切券商只换 venue,
策略、闸门、台账、对账全部不动 —— 和加密那边 testnet/mainnet 的做法一致。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
STATE = Path(__file__).resolve().parent.parent.parent / "data" / "us"


class Broker(ABC):
    name = "base"
    can_short = False

    @abstractmethod
    def account(self) -> dict: ...
    @abstractmethod
    def position(self, sym: str) -> float: ...
    @abstractmethod
    def quote(self, sym: str) -> dict: ...
    @abstractmethod
    def buy(self, sym: str, qty: float, limit: float | None = None) -> dict: ...
    @abstractmethod
    def sell(self, sym: str, qty: float, limit: float | None = None) -> dict: ...
    @abstractmethod
    def open_orders(self) -> list: ...

    @staticmethod
    def market_open(now: dt.datetime | None = None) -> bool:
        n = (now or dt.datetime.now(ET)).astimezone(ET)
        if n.weekday() >= 5:
            return False
        o = n.replace(hour=9, minute=30, second=0, microsecond=0)
        c = n.replace(hour=16, minute=0, second=0, microsecond=0)
        return o <= n < c

    @staticmethod
    def minutes_to_close(now: dt.datetime | None = None) -> float:
        n = (now or dt.datetime.now(ET)).astimezone(ET)
        c = n.replace(hour=16, minute=0, second=0, microsecond=0)
        return (c - n).total_seconds() / 60.0


class PaperBroker(Broker):
    """纸交易券商:真实行情 + 模拟成交(含滑点/佣金),状态存 JSON。"""

    name = "paper"

    def __init__(self, cash: float = 100_000.0, slip_bp: float = 5.0,
                 commission: float = 0.0, state_file: str = "paper_state.json"):
        STATE.mkdir(parents=True, exist_ok=True)
        self.f = STATE / state_file
        if self.f.exists():
            self.s = json.loads(self.f.read_text())
        else:
            self.s = {"cash": cash, "pos": {}, "orders": [], "n": 0}
            self._save()
        self.slip_bp = slip_bp
        self.commission = commission

    def _save(self):
        tmp = self.f.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=1))
        tmp.replace(self.f)

    def account(self) -> dict:
        eq = self.s["cash"]
        for sym, q in self.s["pos"].items():
            q0 = self._last(sym)
            if q0:
                eq += q * q0
        return {"equity": eq, "cash": self.s["cash"], "buying_power": self.s["cash"]}

    def position(self, sym: str) -> float:
        return float(self.s["pos"].get(sym, 0.0))

    def _last(self, sym: str) -> float:
        try:
            from qe.us.data import load_daily
            df = load_daily(sym)
            return float(df["close"].iloc[-1]) if not df.empty else 0.0
        except Exception:
            return 0.0

    def quote(self, sym: str) -> dict:
        px = self._last(sym)
        return {"last": px, "bid": px * (1 - 2e-4), "ask": px * (1 + 2e-4)}

    def _fill(self, sym, qty, side):
        px = self._last(sym)
        if not px:
            return {"ok": False, "err": f"{sym} 无行情"}
        slip = px * self.slip_bp / 1e4
        fill = px + slip if side == "buy" else px - slip
        notional = abs(qty) * fill
        fee = self.commission
        cur = float(self.s["pos"].get(sym, 0.0))
        if side == "buy":
            if notional + fee > self.s["cash"]:
                return {"ok": False, "err": "现金不足"}
            self.s["cash"] -= notional + fee
            self.s["pos"][sym] = cur + qty
        else:
            if cur < qty - 1e-9:
                return {"ok": False, "err": f"持仓不足({cur})"}
            self.s["cash"] += notional - fee
            left = cur - qty
            if left <= 1e-9:
                self.s["pos"].pop(sym, None)
            else:
                self.s["pos"][sym] = left
        self.s["n"] += 1
        oid = f"paper-{self.s['n']}"
        self.s["orders"].append({"oid": oid, "sym": sym, "side": side, "qty": qty,
                                 "px": fill, "ts": time.time()})
        self._save()
        return {"ok": True, "oid": oid, "filled_qty": abs(qty), "avg_px": fill, "fee": fee}

    def buy(self, sym, qty, limit=None):
        return self._fill(sym, abs(qty), "buy")

    def sell(self, sym, qty, limit=None):
        return self._fill(sym, abs(qty), "sell")

    def open_orders(self) -> list:
        return []


def get_broker(venue: str, **kw) -> Broker:
    """按 venue 造券商:us_paper / ibkr / robinhood。"""
    v = (venue or "us_paper").lower()
    if v in ("us_paper", "paper"):
        return PaperBroker(**kw)
    if v == "ibkr":
        from .ibkr import IBKRBroker
        return IBKRBroker()
    if v == "robinhood":
        from .robinhood import RobinhoodBroker
        return RobinhoodBroker()
    raise ValueError(f"未知 venue: {venue}")
