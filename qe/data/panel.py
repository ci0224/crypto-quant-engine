"""把缓存的多币种数据拼成面板(index=时间, columns=币种)。"""
from __future__ import annotations

import pandas as pd

from ..config import Config
from . import hyperliquid as hl


def _tz(idx) -> pd.DatetimeIndex:
    i = pd.to_datetime(idx, utc=True)
    return i.tz_convert("UTC")


def load_panel(interval: str = "1d", coins: list[str] | None = None,
               min_coverage: float = 0.6) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (收盘价面板, 资金费率面板)。

    min_coverage: 至少有这么比例的有效数据才纳入(排除新上市/退市币)。
    """
    cfg = Config.load()
    coins = coins or cfg.universe
    closes, fundings = {}, {}
    for c in coins:
        d = hl.load("candles", c, interval)
        if d is None or d.empty:
            continue
        closes[c] = d["close"]
        f = hl.load("funding", c)
        if f is not None and not f.empty:
            fundings[c] = f["funding"]

    if not closes:
        raise RuntimeError("没有缓存数据,请先运行 scripts/fetch_data.py")

    px = pd.concat(closes, axis=1).sort_index()
    px.index = _tz(px.index)
    px = px[px.index >= pd.Timestamp(cfg.start, tz="UTC")]
    # 按**近期**可用性筛选(不同币的历史起点不同,用全区间会把 1h 数据全筛掉)
    recent = px.tail(max(50, int(len(px) * 0.25)))
    keep = recent.notna().mean() >= min_coverage
    px = px.loc[:, keep[keep].index]
    px = px.ffill()
    # 去掉开头整行全空的部分
    first_valid = px.dropna(how="all").index[0] if px.notna().any().any() else px.index[0]
    px = px.loc[first_valid:]

    fd = pd.DataFrame()
    if fundings:
        fd = pd.concat(fundings, axis=1).sort_index()
        fd.index = _tz(fd.index)
        fd = fd.reindex(columns=px.columns).fillna(0.0)
    return px, fd
