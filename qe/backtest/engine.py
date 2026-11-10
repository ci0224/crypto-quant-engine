"""向量化权重型回测引擎。

约定:
  · prices   : index=时间(UTC), columns=币种, 值为收盘价
  · weights  : 目标权重(index 与 prices 对齐, 行内和为净敞口;做多正、做空负)
  · 信号在第 t 根收盘算好,第 t+1 根**开盘到收盘**的收益生效(避免未来函数)
  · 成本:每期换手 × (taker/maker + 滑点) bps
  · 资金费:多头付正费率、空头收;按持仓权重 × 当期累计费率扣减
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import Costs
from ..metrics import stats


@dataclass
class Result:
    equity: pd.Series
    returns: pd.Series
    weights: pd.DataFrame
    gross: pd.Series
    cost_paid: pd.Series
    funding_paid: pd.Series
    stats: dict = field(default_factory=dict)

    def summary(self) -> str:
        from ..metrics import fmt
        return fmt(self.stats)


class Backtester:
    def __init__(self, prices: pd.DataFrame, costs: Costs | None = None,
                 funding: pd.DataFrame | None = None, initial: float = 10_000.0,
                 max_leverage: float = 2.0):
        self.px = prices.sort_index()
        self.costs = costs or Costs()
        self.funding = funding.sort_index() if funding is not None else None
        self.initial = initial
        self.max_leverage = max_leverage

    # ---------- 资金费对齐到价格频率 ----------
    def _funding_per_bar(self) -> pd.DataFrame | None:
        if self.funding is None or self.funding.empty:
            return None
        f = self.funding.copy()
        idx = self.px.index
        # 每个 bar 的累计费率(bar 内所有小时费率之和)
        out = pd.DataFrame(index=idx, columns=f.columns, dtype=float)
        edges = list(idx) + [idx[-1] + (idx[-1] - idx[-2] if len(idx) > 1 else pd.Timedelta(days=1))]
        for i in range(len(idx)):
            seg = f.loc[(f.index > edges[i]) & (f.index <= edges[i + 1])]
            out.iloc[i] = seg.sum().reindex(f.columns) if len(seg) else 0.0
        return out.fillna(0.0)

    def run(self, weights: pd.DataFrame) -> Result:
        px = self.px
        w = weights.reindex(index=px.index, columns=px.columns).fillna(0.0)
        # 逐行裁剪杠杆
        gross_expo = w.abs().sum(axis=1)
        scale = np.minimum(1.0, self.max_leverage / gross_expo.replace(0, np.nan)).fillna(1.0)
        w = w.mul(scale, axis=0)

        rets = px.pct_change().fillna(0.0)
        # t 日权重 × t+1 日收益
        w_eff = w.shift(1).fillna(0.0)
        gross = (w_eff * rets).sum(axis=1)

        # 交易成本(换手 = 权重变化绝对值之和)
        dw = w.diff().abs().sum(axis=1).fillna(0.0)
        cost = dw * self.costs.one_way_bps / 10_000.0

        # 资金费:多头付正费率 → 收益 -= w * f
        fbar = self._funding_per_bar()
        if fbar is not None:
            f_al = fbar.reindex(index=px.index, columns=px.columns).fillna(0.0)
            funding = (w_eff * f_al).sum(axis=1)
        else:
            funding = pd.Series(0.0, index=px.index)

        net = gross - cost - funding
        equity = self.initial * (1 + net).cumprod()
        res = Result(equity=equity, returns=net, weights=w, gross=gross,
                     cost_paid=cost, funding_paid=funding)
        res.stats = stats(net, equity, turnover=float(dw[dw > 0].mean()) if (dw > 0).any() else 0.0)
        return res
