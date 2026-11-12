#!/usr/bin/env python3
"""VCP 波动收缩形态(Minervini/J Law 口径)量化回测 —— 8 币日线。

规则:
  趋势模板: close>SMA150>SMA200 且 SMA200 上升;close>SMA50;
            close>=1.30x252日低;close>=0.75x252日高
  波动收缩: 布林带宽 100 日分位 <30%;ATR% 100 日分位 <40%
  收窄    : 20 日振幅 <15%
  突破    : close > 前 20 日最高;成交量 > 1.5x20日均量
  风控    : 止损=前 10 日最低,单笔风险 <=8%,否则不进场
  出场    : 跌破止损 / 跌破 SMA50 / 达到 2R
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe.backtest.engine import Backtester
from qe.config import Costs
from qe.strict.data import load_1m

COST = Costs(taker_bps=4.5, slippage_bps=1.0)


SYMS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "AVAX", "LINK"]


def daily(sym):
    b = load_1m(sym)
    d = b.resample("1D").agg({"open": "first", "high": "max", "low": "min",
                              "close": "last", "volume": "sum"}).dropna()
    return d[d["volume"] > 0]


def pct_rank(s: pd.Series, win: int = 100) -> pd.Series:
    return s.rolling(win).apply(lambda a: float((a[-1] >= a).mean()), raw=True)


def vcp_signal(d: pd.DataFrame, tight_pct: float = 0.20,
               bbw_pct: float = 0.30, atr_pct: float = 0.40,
               vol_mult: float = 1.5, hi_frac: float = 0.75,
               trend_mode: str = "strict") -> pd.Series:
    """波动收缩用**自身历史分位**衡量(加密振幅远大于股票,绝对阈值会把样本清空)。"""
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]
    sma50, sma150, sma200 = (c.rolling(n).mean() for n in (50, 150, 200))
    hi252, lo252 = h.rolling(252).max(), l.rolling(252).min()
    bbw = c.rolling(20).std() * 4 / c.rolling(20).mean()
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atrp = tr.rolling(14).mean() / c
    amp20 = (h.rolling(20).max() - l.rolling(20).min()) / c
    if trend_mode == "loose":
        trend = (c > sma200) & (sma200 > sma200.shift(20)) & (c > sma50)
    else:
        trend = ((c > sma150) & (sma150 > sma200) & (sma200 > sma200.shift(20))             & (c > sma50) & (c >= 1.30 * lo252) & (c >= hi_frac * hi252))
    contract = (pct_rank(bbw) < bbw_pct) & (pct_rank(atrp) < atr_pct)
    # VCP 的正确时序:过去 lookback 天内出现过"收缩",今天才突破
    contracted_recently = (pct_rank(amp20) < tight_pct).rolling(15).max().fillna(0) > 0
    tight = contracted_recently
    brk = (c > h.shift(1).rolling(20).max()) & (v > vol_mult * v.rolling(20).mean())
    stop = l.rolling(10).min()
    risk_ok = (c - stop) / c <= 0.12
    return (trend & contract & tight & brk & risk_ok).fillna(False)


def simulate(d: pd.DataFrame, sig: pd.Series, target_r: float = 2.0,
             risk_frac: float = 1.0):
    """状态机:入场次日收盘,止损/2R/SMA50 出场。"""
    c, l = d["close"].values, d["low"].values
    sma50 = d["close"].rolling(50).mean().values
    stop_ref = d["low"].rolling(5).min().values
    w = np.zeros(len(d))
    trades = []
    i, n = 0, len(d)
    while i < n - 1:
        if not sig.iloc[i]:
            i += 1
            continue
        entry_i = i + 1
        ep = c[entry_i]
        stop = stop_ref[i]
        # 止损若已高于入场价(缺口),跳过
        if not np.isfinite(stop) or stop >= ep:
            i += 1
            continue
        risk = ep - stop
        tgt = ep + target_r * risk
        j = entry_i + 1
        exit_i, reason = None, "eod"
        while j < n:
            if l[j] <= stop:
                exit_i, reason = j, "stop"
                break
            if c[j] >= tgt:
                exit_i, reason = j, "target"
                break
            if np.isfinite(sma50[j]) and c[j] < sma50[j]:
                exit_i, reason = j, "ma50"
                break
            j += 1
        if exit_i is None:
            exit_i, reason = n - 1, "eod"
        gross_bp = (c[exit_i] / ep - 1) * 1e4
        net_bp = gross_bp - 11.0
        trades.append(dict(entry=d.index[entry_i], exit=d.index[exit_i], ep=ep,
                           xp=c[exit_i], net=net_bp, hold=int(exit_i - entry_i),
                           reason=reason, rmult=net_bp / (risk / ep * 1e4) if risk > 0 else 0))
        # 按风险定仓:risk_frac=0.01 → 每笔最多亏 1% 权益
        pos = min(1.0, risk_frac / max(risk / ep, 1e-6)) if risk_frac < 1.0 else 1.0
        for k in range(entry_i, exit_i + 1):
            w[k] = pos
        i = exit_i + 1
    return pd.Series(w, index=d.index), trades


def main() -> int:
    rows = []
    for sym in SYMS:
        try:
            d = daily(sym)
        except Exception as e:
            print(f"{sym}: 加载失败 {e}")
            continue
        if len(d) < 300:
            continue
        sig = vcp_signal(d)
        w, tr = simulate(d, sig)
        n_setup = int(sig.sum())
        yrs = (d.index[-1] - d.index[0]).days / 365.25
        if tr:
            t = pd.DataFrame(tr)
            nets = t["net"].values
            n = len(nets)
            mean = float(np.mean(nets))
            sd = float(np.std(nets, ddof=1)) if n > 1 else np.nan
            tstat = mean / (sd / np.sqrt(n)) if n > 1 and sd > 0 else np.nan
            hit = float((nets > 0).mean())
            med_hold = float(np.median(t["hold"]))
            win = nets[nets > 0].mean() if (nets > 0).any() else 0.0
            loss = abs(nets[nets <= 0].mean()) if (nets <= 0).any() else 1.0
            payoff = win / loss if loss else np.nan
            rmed = float(np.median(t["rmult"]))
        else:
            n, mean, tstat, hit, med_hold, payoff, rmed = 0, 0.0, np.nan, 0.0, 0.0, np.nan, 0.0
        try:
            r = Backtester(d[["close"]].rename(columns={"close": "X"}), COST,
                           initial=10_000.0, max_leverage=1.0).run(w.rename("X").to_frame())
            eq = pd.Series(r.equity, index=d.index).dropna()
            tot = eq.iloc[-1] / eq.iloc[0] - 1
            cagr = (1 + tot) ** (1 / yrs) - 1 if yrs > 0 else np.nan
            dd = float((eq / eq.cummax() - 1).min())
        except Exception:
            cagr, dd = np.nan, np.nan
        bh = d["close"].iloc[-1] / d["close"].iloc[0] - 1
        rows.append(dict(sym=sym, setups=n_setup, n=n, mean=mean, t=tstat, hit=hit,
                         payoff=payoff, hold=med_hold, rmed=rmed,
                         cagr=cagr * 100 if cagr == cagr else np.nan, dd=dd * 100,
                         bh=bh * 100))
        print(f"  {sym:5s} 形态 {n_setup:>3} | 成交 {n:>3} | 均值 {mean:>8.2f}bp | t {tstat:>5.2f} "
              f"| 命中 {hit*100:>4.1f}% | 盈亏比 {payoff:>4.2f} | 持仓 {med_hold:>4.0f}d "
              f"| CAGR {cagr*100 if cagr==cagr else float('nan'):>6.1f}% | DD {dd*100:>6.1f}% | 买入持有 {bh*100:>7.1f}%")
    if rows:
        df = pd.DataFrame(rows)
        print("\n=== 汇总 ===")
        print(f"  总形态 {int(df['setups'].sum())} | 总成交 {int(df['n'].sum())} "
              f"| 加权均值 {np.average(df['mean'], weights=df['n'].clip(lower=1)):.2f}bp")
        print(f"  正收益标的 {int((df['mean']>0).sum())}/{len(df)} | 中位 CAGR {df['cagr'].median():.1f}% "
              f"| 中位 DD {df['dd'].median():.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
