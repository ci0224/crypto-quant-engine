"""配置与全局常量。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
RESULTS_DIR = DATA_DIR / "results"
for _d in (DATA_DIR, CACHE_DIR, RESULTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


@dataclass
class Costs:
    """交易成本(全部为 bps)。Hyperliquid taker 4.5bps、maker 1.5bps(基础档)。"""
    taker_bps: float = 4.5
    maker_bps: float = 1.5
    slippage_bps: float = 2.0
    use_maker: bool = False

    @property
    def one_way_bps(self) -> float:
        return (self.maker_bps if self.use_maker else self.taker_bps) + self.slippage_bps


@dataclass
class Config:
    universe: list[str] = field(default_factory=list)
    costs: Costs = field(default_factory=Costs)
    start: str = "2024-11-01"
    lookback_days: int = 730
    top_n: int = 5
    rebalance_days: int = 7
    target_vol: float = 0.25
    trend_fast: int = 10
    trend_slow: int = 50
    max_leverage: float = 1.0
    initial_equity: float = 10_000.0

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        p = Path(path) if path else ROOT / "config.yaml"
        if not p.exists():
            return cls(universe=DEFAULT_UNIVERSE)
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        costs = Costs(**(raw.pop("costs", {}) or {}))
        return cls(costs=costs, **(raw or {}))


DEFAULT_UNIVERSE = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "SUI",
    "TON", "NEAR", "APT", "ARB", "OP", "LTC", "BCH", "ATOM", "FIL", "INJ",
]

API_URL = os.environ.get("HL_API", "https://api.hyperliquid.xyz/info")
