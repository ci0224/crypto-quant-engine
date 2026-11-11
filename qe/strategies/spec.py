"""策略声明(StrategySpec):杠杆与持仓时长由策略自己定,且必须**前置**。

规则来源:crypto-quant-trade/research/09 §5 (K1/K2/K6, 2026-11-11 变更)
  · 不再有全局「≤55 分钟」「9x」上限
  · 但声明之后,必须给出该声明下的完整风险证据(MAE/穿仓/清算缓冲、hold 分布)
  · 声明写在这里(配置),回测脚本只读不写 —— 防止看到结果再调杠杆
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategySpec:
    name: str
    hold_horizon: str        # 人类可读的持仓周期,如 "days-数周"
    leverage: float          # 名义敞口上限(声明值,回测与实盘都按它)
    rationale: str = ""      # 为什么是这个杠杆/周期

    def describe(self) -> str:
        return f"{self.name}: 持仓 {self.hold_horizon}, 杠杆上限 {self.leverage:g}x"


# ---- 已声明策略(改这里 = 改声明;需在跑回测前确定)----
SPECS: dict[str, StrategySpec] = {
    "trend_btc_eth": StrategySpec(
        name="trend_btc_eth",
        hold_horizon="数天到数周(日线 10/50 交叉,换手 ~22x/年)",
        leverage=1.0,
        rationale="日线趋势跟踪,信号稀疏;1x 名义避免在震荡市被杠杆放大回撤",
    ),
    "xsec_momentum": StrategySpec(
        name="xsec_momentum",
        hold_horizon="1-2 周(每周调仓)",
        leverage=1.0,
        rationale="横截面多空,市场中性;1x 即可表达,无需杠杆",
    ),
    "funding_carry": StrategySpec(
        name="funding_carry",
        hold_horizon="1 周(每周调仓,持仓跨多个资金费结算点)",
        leverage=1.0,
        rationale="吃资金费,价格风险已用 beta 对冲;杠杆只放大基差风险,不做",
    ),
    "xsec_reversal": StrategySpec(
        name="xsec_reversal",
        hold_horizon="1-3 根 K 线",
        leverage=1.0,
        rationale="短周期反转,换手极高;不加杠杆(成本已占大头)",
    ),
}


def get(name: str) -> StrategySpec:
    if name not in SPECS:
        raise KeyError(f"策略 {name} 没有声明 spec;请在 qe/strategies/spec.py 里先声明杠杆与持仓周期")
    return SPECS[name]
