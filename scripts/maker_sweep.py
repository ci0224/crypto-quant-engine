#!/usr/bin/env python3
"""maker 家族的信号变体扫描:6 种入场信号 × 3 标的,严格 1m 引擎。

规则固定(唯一通过预注册样本外的那套):
  maker 限价 −100bp 挂 5 分钟 → 目标 +120bp → 55 分钟超时
  成本 3bp 往返 | 穿透 2bp 才成交 | 队列抽样 85% | 真实资金费 | 51.4% 出场 taker
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe.strict.data import boll_state, load_1m, load_funding, resample
from qe.strict.engine import StrictCfg, run


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan)).fillna(50)


def signals(b5):
    c = b5["close"]
    out = {}
    out["boll20"] = boll_state(c, 20, 2.0) > 0
    out["rsi14<30"] = rsi(c, 14) < 30
    ema20 = c.ewm(span=20, adjust=False).mean()
    tr = pd.concat([b5["high"] - b5["low"], (b5["high"] - c.shift()).abs(),
                    (b5["low"] - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 20, adjust=False).mean()
    out["keltner"] = c < ema20 - 2 * atr
    out["down3"] = (c.diff() < 0).rolling(3).sum() == 3
    vwap = (c * b5["volume"]).rolling(288).sum() / b5["volume"].rolling(288).sum()
    sd = c.rolling(288).std()
    out["vwap-2sd"] = c < vwap - 2 * sd
    out["boll40x2.5"] = boll_state(c, 40, 2.5) > 0
    return {k: v.fillna(False).to_numpy(bool) for k, v in out.items()}


def stats(tr):
    if tr is None or len(tr) == 0:
        return None
    p = tr["net_bp"].to_numpy(float)
    ts = pd.DatetimeIndex(pd.to_datetime(tr["t"], utc=True))
    day = pd.Series(p, index=ts.normalize()).groupby(level=0).sum()
    days = pd.date_range(ts.min().normalize(), ts.max().normalize(), freq="D", tz="UTC")
    d = day.reindex(days, fill_value=0.0)
    sd = d.std(ddof=1) or 1e-9
    rng = np.random.default_rng(11)
    boot = [rng.choice(p, len(p), replace=True).mean() for _ in range(1500)]
    win, los = p[p > 0], p[p < 0]
    oos = p[ts <= "2022-12-31"]
    val = p[ts >= "2023-01-01"]
    rec = p[ts >= (ts.max() - pd.Timedelta(days=365))]
    eq = np.cumprod(1 + p / 1e4)
    cagr = (eq[-1] ** (365 / max((ts.max() - ts.min()).days, 1)) - 1) * 100
    return dict(n=len(p), mean=float(p.mean()), t=float(d.mean() / sd * np.sqrt(len(d))),
                ci=float(np.percentile(boot, 2.5)), hit=float((p > 0).mean()),
                payoff=float(win.mean() / abs(los.mean())) if len(los) and len(win) else np.nan,
                oos=float(oos.mean()) if len(oos) else np.nan, val=float(val.mean()) if len(val) else np.nan,
                rec_n=len(rec), rec=float(rec.mean()) if len(rec) else np.nan,
                cagr=float(cagr), sh=float(d.mean() / sd * np.sqrt(365)),
                dd=float((eq / np.maximum.accumulate(eq) - 1).min() * 100))


def main():
    cfg = StrictCfg(x_bp=100.0, tgt_bp=120.0, through_bp=2.0, fill_rate=0.85, taker_exit_prob=0.514)
    rows = []
    for sym in ("BTC", "ETH", "SOL"):
        b1 = load_1m(sym)
        b5 = resample(b1, "5")
        f = load_funding(sym)
        sigs = signals(b5)
        for name, s in sigs.items():
            res = run(b1, b5, pd.Series(s, index=b5.index), f, cfg)
            st = stats(res["trades"])
            if not st:
                continue
            rows.append(dict(sym=sym, sig=name, **st))
            print(f"{sym:4s} {name:12s} n={st['n']:5d} mean={st['mean']:7.2f} t={st['t']:5.2f} "
                  f"ci={st['ci']:7.2f} hit={st['hit']:.2f} rec={st['rec']:7.2f} "
                  f"CAGR={st['cagr']:6.1f}% sh={st['sh']:5.2f} dd={st['dd']:6.1f}%", flush=True)
    df = pd.DataFrame(rows)
    df.to_csv("/tmp/maker_sweep.csv", index=False)
    ok = df[(df.t >= 2.0) & (df.ci > 0) & (df.cagr > 0) & (df.sh >= 1.0) & (df.dd > -60) & (df.rec > 0)]
    print(f"\n=== 通过 (t≥2, CI>0, CAGR>0, Sharpe≥1, DD>-60%, 近12月>0): {len(ok)} / {len(df)} ===")
    if len(ok):
        print(ok.to_string(index=False, float_format=lambda x: f"{x:8.2f}"))
    json.dump(rows, open("/tmp/maker_sweep.json", "w"), ensure_ascii=False, default=str)


if __name__ == "__main__":
    main()
