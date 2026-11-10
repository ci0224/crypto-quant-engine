"""绩效指标。"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 365  # 加密 7×24


def drawdown(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return equity / peak - 1.0


def stats(returns: pd.Series, equity: pd.Series, turnover: float = 0.0,
          freq: int = TRADING_DAYS, rf: float = 0.0) -> dict:
    r = returns.dropna()
    if len(r) == 0 or equity.iloc[-1] <= 0:
        return {"cagr": -1.0, "sharpe": np.nan, "max_dd": -1.0, "calmar": np.nan,
                "vol": np.nan, "total": -1.0, "hit": np.nan, "n": 0}
    n_years = max(len(r) / freq, 1e-9)
    total = float(equity.iloc[-1] / equity.iloc[0] - 1)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / n_years) - 1)
    vol = float(r.std() * np.sqrt(freq))
    excess = r - rf / freq
    sharpe = float(excess.mean() / r.std() * np.sqrt(freq)) if r.std() > 0 else np.nan
    downside = r[r < 0].std()
    sortino = float(excess.mean() / downside * np.sqrt(freq)) if downside and downside > 0 else np.nan
    dd = drawdown(equity)
    max_dd = float(dd.min())
    calmar = float(cagr / abs(max_dd)) if max_dd < 0 else np.nan
    monthly = (1 + r).resample("ME").prod() - 1 if hasattr(r.index, "resample") else None
    return {
        "total": total,
        "cagr": cagr,
        "vol": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_dd": max_dd,
        "calmar": calmar,
        "hit": float((r > 0).mean()),
        "turnover": float(turnover),
        "best_m": float(monthly.max()) if monthly is not None and len(monthly) else np.nan,
        "worst_m": float(monthly.min()) if monthly is not None and len(monthly) else np.nan,
        "n": int(len(r)),
    }


def fmt(s: dict) -> str:
    def p(x, d=2):
        return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x*100:.{d}f}%"
    def n(x, d=2):
        return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.{d}f}"
    return (f"总收益 {p(s['total']):>8s} · CAGR {p(s['cagr']):>7s} · 年化波动 {p(s['vol']):>6s} · "
            f"Sharpe {n(s['sharpe']):>5s} · Sortino {n(s['sortino']):>5s} · "
            f"最大回撤 {p(s['max_dd']):>7s} · Calmar {n(s['calmar']):>5s} · "
            f"胜率 {p(s['hit']):>5s} · 换手/期 {n(s['turnover']):>4s}")
