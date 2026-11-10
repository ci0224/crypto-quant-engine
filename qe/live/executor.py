"""订单执行:纸交易(默认)与 Hyperliquid 真实下单。

实盘需要环境变量:
    HL_ACCOUNT_ADDRESS  你的主钱包地址(0x...)
    HL_PRIVATE_KEY      **API 钱包**私钥(在 Hyperliquid 后台生成,只有交易权限,不能提币)

安全原则:
  · 私钥只从环境变量/600 权限文件读取,绝不落日志、绝不进 git
  · 默认 paper 模式,必须显式 --mode live 才会真实下单
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import DATA_DIR

LIVE_DIR = DATA_DIR / "live"
LIVE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Position:
    coin: str
    size: float = 0.0        # 正=多,负=空(单位:币)
    entry_px: float = 0.0
    notional: float = 0.0


@dataclass
class Book:
    equity: float
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"equity": self.equity,
                "positions": {k: vars(v) for k, v in self.positions.items()},
                "trades": self.trades[-500:]}


class PaperExecutor:
    """纸交易:按中间价 ± 滑点成交,收 taker 手续费。"""

    def __init__(self, equity: float = 10_000.0, taker_bps: float = 4.5, slippage_bps: float = 2.0):
        self.book = Book(equity=equity)
        self.taker = taker_bps / 10_000
        self.slip = slippage_bps / 10_000

    def submit(self, coin: str, target_notional: float, mid: float, ts: str = "") -> dict | None:
        """把 coin 的目标名义金额调到 target_notional(0=平仓)。返回成交记录或 None。"""
        cur = self.book.positions.get(coin) or Position(coin)
        cur_notional = cur.size * mid
        delta = target_notional - cur_notional
        if abs(delta) < max(10.0, abs(target_notional) * 0.02):   # 2% 容差,避免频繁小额调仓
            return None
        size = delta / mid
        px = mid * (1 + self.slip if size > 0 else 1 - self.slip)
        fee = abs(size * px) * self.taker
        new_size = cur.size + size
        self.book.equity -= fee
        self.book.positions[coin] = Position(coin, new_size, cur.entry_px or px, abs(new_size) * mid)
        t = {"ts": ts or time.strftime("%F %T"), "coin": coin, "side": "buy" if size > 0 else "sell",
             "size": round(size, 6), "px": round(px, 4), "notional": round(size * px, 2), "fee": round(fee, 4),
             "mode": "paper"}
        self.book.trades.append(t)
        return t

    def mark(self, mids: dict[str, float]) -> float:
        """按最新价重估权益(不含未实现盈亏的现金调整,简化为名义敞口)。"""
        return self.book.equity


class HyperliquidExecutor:
    """真实下单(需要 SDK + API 钱包私钥)。"""

    def __init__(self, account_address: str | None = None, private_key: str | None = None,
                 testnet: bool = False):
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from eth_account import Account

        pk = private_key or os.environ.get("HL_PRIVATE_KEY", "")
        addr = account_address or os.environ.get("HL_ACCOUNT_ADDRESS", "")
        if not pk or not addr:
            raise RuntimeError("缺少 HL_PRIVATE_KEY / HL_ACCOUNT_ADDRESS(请用 API 钱包私钥)")
        wallet = Account.from_key(pk)
        base = "https://api.hyperliquid-testnet.xyz" if testnet else "https://api.hyperliquid.xyz"
        self.info = Info(base, skip_ws=True)
        self.ex = Exchange(wallet, base, account_address=addr)
        self.addr = addr
        self.slippage = 0.01

    def mids(self) -> dict[str, float]:
        m = self.info.all_mids()
        return {k: float(v) for k, v in m.items() if not k.startswith("#")}

    def submit(self, coin: str, target_notional: float, mid: float, ts: str = "") -> dict | None:
        state = self.info.user_state(self.addr)
        pos = {p["position"]["coin"]: float(p["position"]["szi"]) for p in state.get("assetPositions", [])}
        cur_size = pos.get(coin, 0.0)
        delta_size = target_notional / mid - cur_size
        if abs(delta_size * mid) < 12:
            return None
        meta = self.info.meta()
        sz_dec = next((a["szDecimals"] for a in meta["universe"] if a["name"] == coin), 2)
        size = round(abs(delta_size), sz_dec)
        if size == 0:
            return None
        is_buy = delta_size > 0
        res = self.ex.market_open(coin, is_buy, size, slippage=self.slippage)
        return {"ts": ts or time.strftime("%F %T"), "coin": coin, "side": "buy" if is_buy else "sell",
                "size": size, "px": mid, "mode": "live", "raw_status": str(res.get("status"))[:80]}

    def equity(self) -> float:
        st = self.info.user_state(self.addr)
        return float(st["marginSummary"]["accountValue"])


def make_executor(mode: str, equity: float):
    if mode == "live":
        return HyperliquidExecutor()
    return PaperExecutor(equity=equity)


def save_state(name: str, payload: dict) -> Path:
    p = LIVE_DIR / f"state_{name}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p
