"""策略库 v2:统一使用「组合级波动率目标」定仓。

关键修正(v1 的问题):v1 把权重除以资产数量 → 实际敞口只有 0.3x,收益被稀释。
v2 先用信号构造原始权重,再按**组合过去 N 日已实现波动**缩放到目标波动率,
这样每个策略都在可比的 risk budget 下比较(默认年化 25%)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _rebalance_dates(index: pd.DatetimeIndex, every: int, warmup: int) -> list[pd.Timestamp]:
    return [dt for i, dt in enumerate(index) if i >= warmup and i % every == 0]


def vol_target_scale(w_raw: pd.DataFrame, px: pd.DataFrame, target: float = 0.25,
                     lookback: int = 30, max_lev: float = 2.0, freq: int = 365) -> pd.DataFrame:
    """按组合已实现波动缩放权重(只用 t 及之前的数据)。"""
    rets = px.pct_change().fillna(0.0)
    # 原始权重的模拟组合收益(shift(1) 避免未来函数)
    port = (w_raw.shift(1) * rets).sum(axis=1)
    realized = port.rolling(lookback).std() * np.sqrt(freq)
    scale = (target / realized.replace(0, np.nan)).clip(upper=max_lev).shift(1)
    return w_raw.mul(scale, axis=0).fillna(0.0)


def _book(px: pd.DataFrame, every: int, warmup: int, pick) -> pd.DataFrame:
    """通用:在再平衡日用 pick(dt) 返回 {币: 权重}。"""
    w = pd.DataFrame(0.0, index=px.index, columns=px.columns)
    for dt in _rebalance_dates(px.index, every, warmup):
        for coin, wt in (pick(dt) or {}).items():
            w.loc[dt, coin] = wt
    return w.replace(0.0, np.nan).ffill().fillna(0.0)


# ---------------------------------------------------------------- A 横截面动量
def xsec_momentum(px: pd.DataFrame, lookback: int = 30, top_n: int = 5, rebalance: int = 7,
                  skip: int = 0, vol_target: float = 0.25) -> pd.DataFrame:
    ret = px.shift(skip).pct_change(lookback)

    def pick(dt):
        s = ret.loc[dt].dropna()
        if len(s) < 2 * top_n:
            return {}
        out = {c: 0.5 / top_n for c in s.nlargest(top_n).index}
        out.update({c: -0.5 / top_n for c in s.nsmallest(top_n).index})
        return out

    w = _book(px, rebalance, lookback + skip + 1, pick)
    return vol_target_scale(w, px, vol_target)


# ---------------------------------------------------------------- D 短期反转
def xsec_reversal(px: pd.DataFrame, lookback: int = 1, top_n: int = 5, rebalance: int = 1,
                  vol_target: float = 0.25) -> pd.DataFrame:
    """短线反转:做多过去 N 日跌得最狠的,做空涨得最多的(流动性溢价)。"""
    ret = px.pct_change(lookback)

    def pick(dt):
        s = ret.loc[dt].dropna()
        if len(s) < 2 * top_n:
            return {}
        out = {c: 0.5 / top_n for c in s.nsmallest(top_n).index}
        out.update({c: -0.5 / top_n for c in s.nlargest(top_n).index})
        return out

    w = _book(px, rebalance, lookback + 2, pick)
    return vol_target_scale(w, px, vol_target)


# ---------------------------------------------------------------- B 资金费套利(beta 中性)
def funding_carry(px: pd.DataFrame, funding: pd.DataFrame, lookback_days: int = 7,
                  top_n: int = 5, rebalance: int = 7, beta_window: int = 60,
                  beta_neutral: bool = True, vol_target: float = 0.25) -> pd.DataFrame:
    """做空高资金费率(收租)+ 做多低费率;再用 BTC 对冲掉净 beta,只留资金费收益。"""
    if funding is None or funding.empty:
        return pd.DataFrame(0.0, index=px.index, columns=px.columns)

    f = funding.copy()
    f.index = pd.to_datetime(f.index, utc=True)
    f_daily = f.resample("1D").sum().reindex(px.index, method="ffill")
    f_mean = f_daily.rolling(lookback_days).mean()

    rets = px.pct_change()
    mkt = rets.mean(axis=1)  # 等权市场因子
    betas = {}
    for c in px.columns:
        cov = rets[c].rolling(beta_window).cov(mkt)
        var = mkt.rolling(beta_window).var()
        betas[c] = (cov / var.replace(0, np.nan)).fillna(1.0)
    beta_df = pd.DataFrame(betas)

    def pick(dt):
        s = f_mean.loc[dt].dropna()
        s = s[s.index.isin(px.columns)]
        if len(s) < 2 * top_n:
            return {}
        out = {c: -0.5 / top_n for c in s.nlargest(top_n).index}
        out.update({c: 0.5 / top_n for c in s.nsmallest(top_n).index})
        if beta_neutral:
            b = beta_df.loc[dt]
            net_beta = sum(out[c] * b.get(c, 1.0) for c in out)
            hedge = "BTC" if "BTC" in px.columns else ("ETH" if "ETH" in px.columns else None)
            if hedge:
                out[hedge] = out.get(hedge, 0.0) - net_beta / max(b.get(hedge, 1.0), 0.2)
        return out

    w = _book(px, rebalance, max(lookback_days, beta_window) + 2, pick)
    return vol_target_scale(w, px, vol_target)


# ---------------------------------------------------------------- C 趋势跟踪
def trend_following(px: pd.DataFrame, fast: int = 20, slow: int = 100,
                    rebalance: int = 1, vol_target: float = 0.25) -> pd.DataFrame:
    sma_f, sma_s = px.rolling(fast).mean(), px.rolling(slow).mean()
    sig = np.sign(sma_f - sma_s).fillna(0.0)
    vol = px.pct_change().rolling(30).std() * np.sqrt(365)
    raw = sig * (1.0 / vol.replace(0, np.nan)).clip(upper=5).fillna(0.0)
    raw = raw.div(raw.abs().sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)

    mask = pd.Series(False, index=px.index)
    for dt in _rebalance_dates(px.index, rebalance, slow + 1):
        mask[dt] = True
    w = raw.where(mask, np.nan).ffill().fillna(0.0)
    return vol_target_scale(w, px, vol_target)


REGISTRY = {
    "xsec_momentum": xsec_momentum,
    "xsec_reversal": xsec_reversal,
    "funding_carry": funding_carry,
    "trend_following": trend_following,
}
