"""严格回测框架 · 执行层 v2(忠实复刻原规则 + 叠加严格层)。

基础规则(与 crypto-quant-trade/research/backtest_maker.run 一致):
  · 信号:5m 收盘 < 布林下轨,且 UTC 8-20 点
  · 入场:信号 bar 之后 5 根 1m K 线内,挂 ref×(1−x_bp) 限价;持仓期间不再开新仓
  · 止盈:入场后 1..55 分钟内 high ≥ 入场×(1+tgt_bp)
  · 超时:第 55 分钟收盘价挂限价,再等最多 240 分钟回来成交;仍不成交 → 按市价记
严格层(可选开关,默认全开):
  A through_bp  价格须**穿过**挂单价才算成交(队列)
  B fill_rate   队列抽样通过率(确定性哈希,可复现)
  C funding     按真实历史 8h 费率:持仓跨越结算点即付全额
  D taker_blend 超时腿按 taker_prob 概率以 taker 成交(+滑点+冲击)
  E capacity    单笔名义 ≤ 该 5 分钟美元成交额 × cap_frac
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class StrictCfg:
    x_bp: float = 100.0
    tgt_bp: float = 120.0
    hold_cap: int = 55
    overrun: int = 240
    # ---- 严格层 ----
    through_bp: float = 0.0        # 0 = 复刻原规则
    fill_rate: float = 1.0         # 1.0 = 复刻原规则
    taker_exit_prob: float = 0.0   # 0 = 复刻原规则;0.514 = 11 号报告实测
    maker_bp: float = 1.5
    taker_bp: float = 4.5
    slip_base_bp: float = 2.0
    impact_k: float = 2.0
    notional: float = 10_000.0
    cap_frac: float = 0.01
    hours: tuple = (8, 20)



def _u(ts) -> float:
    return int(hashlib.sha256(str(ts).encode()).hexdigest()[:12], 16) / float(16 ** 12)


def _funding_sum(t0, t1, f: pd.DataFrame) -> float:
    if f is None or f.empty:
        return 0.0
    seg = f.loc[(f.index > t0) & (f.index <= t1), "rate"]
    return float(seg.sum()) if len(seg) else 0.0


def run(bars1m: pd.DataFrame, bars5: pd.DataFrame, signal: pd.Series,
        funding: pd.DataFrame, cfg: StrictCfg) -> dict:
    closed = bars1m["close"].values
    high = bars1m["high"].values
    low = bars1m["low"].values
    t1 = bars1m.index
    N = len(bars1m)

    # 每个 5m bar 对应"之后 5 根 1m"的起始下标
    pos = np.searchsorted(t1.values, (bars5.index + pd.Timedelta(minutes=5)).values, side="left")

    hours = set(range(cfg.hours[0], cfg.hours[1]))
    c5 = bars5["close"].values
    sig = np.asarray(signal.fillna(False).values, dtype=bool)
    sig = sig & np.array([ts.hour in hours for ts in bars5.index])
    sig &= np.isfinite(c5)

    trades = []
    free_from = 0
    n_sig = int(sig.sum())
    for k in np.where(sig)[0]:
        p = int(pos[k])
        if p < 0 or p + 5 >= N or p < free_from:
            continue
        ref = c5[k]
        lim = ref * (1 - cfg.x_bp * 1e-4)
        seg_l = low[p:p + 5]
        thr = lim * (1 - cfg.through_bp * 1e-4)
        hit = seg_l <= thr
        if not hit.any():
            continue
        if _u(bars5.index[k]) >= cfg.fill_rate:      # 队列抽样
            continue
        fi = p + int(np.argmax(hit))
        ep = lim
        end = min(fi + cfg.hold_cap, N - 1)
        tp = ep * (1 + cfg.tgt_bp * 1e-4)

        xi, xp, reason = None, None, None
        for j in range(fi + 1, end + 1):
            if high[j] >= tp:
                xi, xp, reason = j, tp, "target"
                break
        if xi is None:
            px = closed[end]
            stop = min(end + cfg.overrun, N - 1)
            for j in range(end + 1, stop + 1):
                if high[j] >= px:
                    xi, xp, reason = j, px, "timeout_maker"
                    break
            if xi is None:
                xi, xp, reason = stop, closed[stop], "stuck"

        gross_bp = (xp / ep - 1) * 1e4
        # 出场费:target/timeout_maker = maker;stuck 或 taker_blend 抽样 → taker
        is_taker = (reason == "stuck") or (
            reason == "timeout_maker" and _u(f"{xi}-taker") < cfg.taker_exit_prob)
        fee_bp = cfg.maker_bp + (cfg.taker_bp if is_taker else cfg.maker_bp)
        impact_bp = 0.0
        if is_taker:
            dv = float(bars1m["volume"].iloc[xi] * xp) or 1.0
            notional = min(cfg.notional, cfg.cap_frac * float(bars5["dollar"].iloc[k]))
            impact_bp = cfg.slip_base_bp + cfg.impact_k * np.sqrt(notional / max(dv, 1.0))
        fund_bp = _funding_sum(t1[fi], t1[xi], funding) * 1e4
        net_bp = gross_bp - fee_bp - fund_bp - impact_bp
        mae_bp = (ep - float(low[fi:xi + 1].min())) / ep * 1e4

        trades.append({
            "t": t1[fi], "exit_t": t1[xi], "hold_min": float(xi - fi), "reason": reason,
            "gross_bp": round(gross_bp, 2), "fee_bp": round(fee_bp, 2),
            "funding_bp": round(fund_bp, 2), "impact_bp": round(impact_bp, 2),
            "net_bp": round(net_bp, 2), "mae_bp": round(mae_bp, 1),
        })
        free_from = xi + 1

    df = pd.DataFrame(trades)
    return {"signals": n_sig, "trades": df, "fills": len(df),
            "fill_of_signals": (len(df) / n_sig) if n_sig else 0.0}
