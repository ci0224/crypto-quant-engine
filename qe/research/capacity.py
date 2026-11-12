"""容量模型:平方根冲击 + 反解"edge 衰减一半"的资金上限。

文献依据:
  · Almgren & Chriss (2000) 最优执行:冲击分永久/临时两部分
  · 平方根律 impact ≈ k·σ·√(Q/ADV) —— 业界标准(Bouchaud 等, Tóth 等 2011)
  · 这里的用途不是"精确预测滑点",而是给出**单笔名义的上限量级**

口径:
  impact_bp = k · σ_daily · √(Q / ADV_daily) · 1e4
  当 impact_bp = edge_bp × (1 − decay) 时反解 Q → 该策略在该币上的最大单笔名义
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Capacity:
    symbol: str
    adv_usd: float          # 日均美元成交额
    sigma_daily: float      # 日波动率(小数)
    edge_bp: float          # 单笔毛利(bp)
    notional_usd: float     # 单笔名义上限
    impact_bp: float        # 在该名义下的预估冲击
    aum_usd: float          # 按 concurrency 笔并发折算的组合上限


def impact_bp(notional_usd: float, adv_usd: float, sigma_daily: float, k: float = 0.1) -> float:
    """平方根冲击(bp)。k=0.1 是偏保守的经验系数(文献区间 0.05-0.3)。"""
    if adv_usd <= 0 or notional_usd <= 0:
        return 0.0
    return k * sigma_daily * np.sqrt(notional_usd / adv_usd) * 1e4


def max_notional(edge_bp: float, adv_usd: float, sigma_daily: float,
                 k: float = 0.1, decay: float = 0.5) -> tuple[float, float]:
    """反解"冲击吃掉 edge 的 decay 比例"时的单笔名义。返回 (notional, impact_bp)。"""
    budget = max(edge_bp * decay, 1e-9)
    # budget = k·σ·√(Q/ADV)·1e4  →  Q = ADV·(budget/(k·σ·1e4))²
    q = adv_usd * (budget / (k * sigma_daily * 1e4)) ** 2
    return q, impact_bp(q, adv_usd, sigma_daily, k)


def measure(symbol: str, bars, edge_bp: float, concurrency: int = 3,
            k: float = 0.1, decay: float = 0.5) -> Capacity:
    """从 1m K 线算 ADV 与日波动,给出容量。

    bars: load_1m() 的 DataFrame(需 close / volume)
    """
    px = bars["close"].astype(float)
    vol = bars["volume"].astype(float)
    dollar_1m = (px * vol).resample("1D").sum().dropna()
    adv = float(dollar_1m.tail(180).median())          # 用近半年中位数,抗异常放量
    daily_close = px.resample("1D").last().dropna()
    sig = float(daily_close.pct_change().dropna().tail(180).std())
    q, imp = max_notional(edge_bp, adv, sig, k, decay)
    return Capacity(symbol=symbol, adv_usd=adv, sigma_daily=sig, edge_bp=edge_bp,
                    notional_usd=q, impact_bp=imp, aum_usd=q * concurrency)


def report(symbol: str, bars, edge_bp: float, k: float = 0.1) -> dict:
    c = measure(symbol, bars, edge_bp, k=k)
    # 几个参考规模下的冲击,方便直观判断
    grid = {}
    for q in (1_000, 10_000, 100_000, 1_000_000):
        grid[q] = round(impact_bp(q, c.adv_usd, c.sigma_daily, k), 2)
    return {"symbol": symbol, "adv_usd": round(c.adv_usd),
            "sigma_daily": round(c.sigma_daily, 4), "edge_bp": edge_bp,
            "max_notional_usd": round(c.notional_usd),
            "impact_at_max_bp": round(c.impact_bp, 2),
            "aum_usd": round(c.aum_usd), "impact_grid_bp": grid}


def maker_capacity(bars, x_bp: float = 100.0, participation: float = 0.10,
                   window_min: int = 5, band_n: int = 20, band_k: float = 2.0) -> dict:
    """**maker 专用容量**:约束不是价格冲击,而是"跌到 −x_bp 时那段窗口的成交量"。

    做法:找出所有"5m 跌破布林下轨"的信号时刻,统计每个信号后 window_min 分钟内
    的美元成交额,取 10 分位数 × 参与率 → 单笔名义上限。

    为什么用 10 分位:容量要按"最差的那批信号"算,否则会被放量日的数据骗。
    """
    b5 = bars["close"].resample("5min").last().dropna().to_frame("close")
    ma = b5["close"].rolling(band_n).mean()
    sd = b5["close"].rolling(band_n).std()
    sig = b5["close"] < (ma - band_k * sd)
    if not sig.any():
        return {"symbol": "", "n_signals": 0, "cap_usd": 0.0}
    dollar = (bars["close"] * bars["volume"]).resample("1min").sum()
    caps = []
    for t in b5.index[sig]:
        seg = dollar.loc[t:t + np.timedelta64(window_min, "m")]
        v = float(seg.sum()) if len(seg) else 0.0
        if v > 0:
            caps.append(v)
    caps = np.array(caps)
    p10 = float(np.percentile(caps, 10))
    return {"n_signals": int(len(caps)),
            "median_5m_usd": float(np.median(caps)),
            "p10_5m_usd": p10,
            "cap_usd": p10 * participation,
            "participation": participation,
            "x_bp": x_bp}
