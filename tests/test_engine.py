"""核心逻辑测试:回测引擎的会计正确性与防未来函数。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qe.backtest.engine import Backtester
from qe.config import Costs
from qe.metrics import stats
from qe.strategies import library as S


@pytest.fixture()
def prices() -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=120, freq="D", tz="UTC")
    rng = np.random.default_rng(42)
    data = {}
    for c, drift in (("BTC", 0.002), ("ETH", 0.001)):
        r = rng.normal(drift, 0.03, len(idx))
        data[c] = 100 * np.exp(np.cumsum(r))
    return pd.DataFrame(data, index=idx)


def test_buy_and_hold_matches_price_return(prices):
    """满仓持有 BTC:净值应≈价格涨幅(忽略成本),验证引擎的收益核算。"""
    w = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    w["BTC"] = 1.0
    bt = Backtester(prices, costs=Costs(0, 0, 0), initial=1000.0)
    res = bt.run(w)
    # 约定:w[t] 在 t 收盘建仓,赚 t→t+1 的收益,故总收益 = P[-1]/P[0]
    expected = 1000.0 * prices["BTC"].iloc[-1] / prices["BTC"].iloc[0]
    assert abs(res.equity.iloc[-1] - expected) / expected < 0.02


def test_costs_reduce_returns(prices):
    """同样的权重,有成本时收益必须更低。"""
    w = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    w.iloc[10:, 0] = 1.0
    free = Backtester(prices, costs=Costs(0, 0, 0), initial=1000.0).run(w)
    costly = Backtester(prices, costs=Costs(20, 20, 10), initial=1000.0).run(w)
    assert costly.equity.iloc[-1] < free.equity.iloc[-1]


def test_no_lookahead(prices):
    """把未来价格改掉,不应影响过去的权重/收益(引擎 shift(1) 生效)。"""
    w = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    w["BTC"] = 1.0
    base = Backtester(prices, costs=Costs(0, 0, 0), initial=1000.0).run(w)
    mutated = prices.copy()
    mutated.iloc[-10:, 0] *= 1.5
    alt = Backtester(mutated, costs=Costs(0, 0, 0), initial=1000.0).run(w)
    # 前 100 根的收益必须完全一致
    assert np.allclose(base.returns.iloc[:100], alt.returns.iloc[:100])


def test_leverage_cap(prices):
    w = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    w["BTC"] = 5.0
    w["ETH"] = -5.0
    res = Backtester(prices, costs=Costs(0, 0, 0), initial=1000.0, max_leverage=2.0).run(w)
    assert res.weights.abs().sum(axis=1).max() <= 2.0 + 1e-9


def test_strategies_produce_valid_weights(prices):
    fd = pd.DataFrame({"BTC": 1e-5, "ETH": 2e-5},
                      index=pd.date_range("2024-01-01", periods=120, freq="h", tz="UTC"))
    for name, w in [
        ("momentum", S.xsec_momentum(prices, lookback=10, top_n=1, rebalance=7)),
        ("reversal", S.xsec_reversal(prices, lookback=1, top_n=1, rebalance=1)),
        ("trend", S.trend_following(prices, fast=5, slow=20, rebalance=1)),
        ("carry", S.funding_carry(prices, fd, lookback_days=3, top_n=1, rebalance=7)),
    ]:
        assert w.shape == prices.shape, name
        assert np.isfinite(w.values).all(), f"{name} 含 NaN/Inf"
        assert w.abs().sum(axis=1).max() <= 3.0, f"{name} 权重异常大"


def test_stats_on_flat_series():
    idx = pd.date_range("2024-01-01", periods=100, freq="D", tz="UTC")
    r = pd.Series(0.0, index=idx)
    s = stats(r, 1000 * (1 + r).cumprod())
    assert s["total"] == 0.0 and s["max_dd"] == 0.0
