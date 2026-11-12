"""Walk-forward 验证:滚动训练/测试,带 purge 与 embargo。

为什么必须做:
  单次"全样本回测 + 挑最好参数"会系统性高估。walk-forward 模拟"真实决策顺序":
  在每个时点只用**过去**的数据选参数,再在**未来**评估。

   purge   —— 训练集末端要剔掉与测试集重叠的持仓期,否则信息泄漏
   embargo —— 测试集开始时再空一段(>= 特征回看窗),防止边界处仍受训练期影响
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Fold:
    i_train: range
    i_test: range
    train_end: pd.Timestamp
    test_start: pd.Timestamp


def folds(index: pd.DatetimeIndex, train_days: int = 730, test_days: int = 365,
          purge_days: int = 30, embargo_days: int = 30) -> list[Fold]:
    """按日历天生成滚动折。"""
    out = []
    t0 = index[0]
    end = index[-1]
    cur = t0 + pd.Timedelta(days=train_days)
    while cur + pd.Timedelta(days=test_days) <= end:
        tr_end = cur - pd.Timedelta(days=purge_days)
        te_start = cur + pd.Timedelta(days=embargo_days)
        i_tr = np.where((index >= t0) & (index <= tr_end))[0]
        i_te = np.where((index >= te_start) & (index <= cur + pd.Timedelta(days=test_days)))[0]
        if len(i_tr) > 100 and len(i_te) > 30:
            out.append(Fold(range(i_tr[0], i_tr[-1] + 1), range(i_te[0], i_te[-1] + 1),
                            tr_end, te_start))
        cur = cur + pd.Timedelta(days=test_days)
    return out


def sharpe(ret: pd.Series) -> float:
    if len(ret) < 20 or ret.std() == 0:
        return 0.0
    return float(ret.mean() / ret.std() * np.sqrt(365))


def run(px: pd.DataFrame, weights_fn, param_grid, cost_bp_per_turn: float = 11.0,
        **fold_kw) -> dict:
    """weights_fn(px, **params) -> 权重表。每折在训练段选最好参数,在测试段评估。

    返回:每折的 (选中的参数, 训练 Sharpe, 测试 Sharpe) + 汇总统计。
    """
    rets = px.pct_change()
    fl = folds(px.index, **fold_kw)
    rows = []
    for f in fl:
        tr = px.iloc[list(f.i_train)]
        te = px.iloc[list(f.i_test)]
        best, best_sh = None, -9e9
        for p in param_grid:
            w = weights_fn(tr, **p).shift(1).fillna(0.0)
            turn = w.diff().abs().sum(axis=1).fillna(0.0)
            r = (w * rets.iloc[list(f.i_train)]).sum(axis=1) - turn * cost_bp_per_turn / 1e4
            s = sharpe(r.dropna())
            if s > best_sh:
                best, best_sh = p, s
        w = weights_fn(te, **best).shift(1).fillna(0.0)
        turn = w.diff().abs().sum(axis=1).fillna(0.0)
        r = (w * rets.iloc[list(f.i_test)]).sum(axis=1) - turn * cost_bp_per_turn / 1e4
        rows.append({"test_start": str(te.index[0].date()), "test_end": str(te.index[-1].date()),
                     "best_param": str(best), "train_sharpe": round(best_sh, 2),
                     "test_sharpe": round(sharpe(r.dropna()), 2)})
    df = pd.DataFrame(rows)
    return {"folds": df,
            "n_folds": len(df),
            "mean_train": float(df["train_sharpe"].mean()) if len(df) else 0.0,
            "mean_test": float(df["test_sharpe"].mean()) if len(df) else 0.0,
            "test_positive": int((df["test_sharpe"] > 0).sum()) if len(df) else 0}
