#!/usr/bin/env python3
"""VCP 30 币验证:直接从 Binance Vision 日线 zip 读,不依赖 1m 缓存。"""
import sys, glob, zipfile, io
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.test_vcp import simulate, pct_rank


def vcp_sig(d, tight_pct=0.35, vol_mult=1.2, lookback=15, max_risk=0.12):
    """与 validate_vcp 完全一致的宽松口径(振幅分位收缩 + 宽松趋势)。"""
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]
    sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
    amp20 = (h.rolling(20).max() - l.rolling(20).min()) / c
    trend = (c > sma200) & (sma200 > sma200.shift(20)) & (c > sma50)
    contracted = (pct_rank(amp20) < tight_pct).rolling(lookback).max().fillna(0) > 0
    brk = c > h.shift(1).rolling(20).max()
    volok = v > vol_mult * v.rolling(20).mean()
    # 止损放"收缩区低点"(5 日低),阈值按自身波动率缩放 —— 股票的 8% 在加密不成立
    stop = l.rolling(5).min()
    risk = (c - stop) / c
    atr = (h - l).rolling(14).mean() / c
    cap = (2.5 * atr).fillna(0.25).clip(0.04, 0.30)
    ok_risk = (risk > 0) & (risk <= cap)
    return (trend & contracted & brk & volok & ok_risk).fillna(False)
from qe.config import Costs
from qe.backtest.engine import Backtester

DDIR = Path("/home/ubuntu/apps/crypto-quant-trade/data/daily")
SYMS = ["BTC","ETH","SOL","BNB","XRP","DOGE","AVAX","LINK","ADA","DOT","LTC","BCH","TRX",
        "ATOM","NEAR","APT","ARB","OP","SUI","TIA","INJ","SEI","WIF","PEPE","SHIB","ETC",
        "FIL","UNI","AAVE","MATIC"]

def load_daily(sym):
    files = sorted(glob.glob(str(DDIR / f"{sym}USDT-1d-*.zip")))
    if not files:
        return None
    frames = []
    for f in files:
        try:
            with zipfile.ZipFile(f) as z:
                raw = z.read(z.namelist()[0]).decode()
            rows = []
            for ln in raw.strip().splitlines():
                p = ln.split(",")
                if len(p) < 6:
                    continue
                try:
                    rows.append((int(p[0]), float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])))
                except ValueError:
                    continue
            if rows:
                frames.append(pd.DataFrame(rows, columns=["t","open","high","low","close","volume"]))
        except Exception:
            continue
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True).drop_duplicates("t").sort_values("t")
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    return df.set_index("t").astype(float)

def main():
    all_tr, per_sym, rets = [], {}, {}
    for sym in SYMS:
        d = load_daily(sym)
        if d is None or len(d) < 300:
            print(f"  {sym:6s} 跳过(数据不足)"); continue
        sg = vcp_sig(d)
        w, tr = simulate(d, sg, 2.0, risk_frac=0.01)
        all_tr += tr
        per_sym[sym] = len(tr)
        r = Backtester(d["close"].rename("X").to_frame(), Costs(), initial=1e4, max_leverage=1.0, funding=None).run(w.rename("X").to_frame())
        rets[sym] = pd.Series(np.asarray(r.returns, dtype=float), index=d.index)
        eq = pd.Series(np.asarray(r.equity, dtype=float), index=d.index)
        dd = float((eq / eq.cummax() - 1).min())
        print(f"  {sym:6s} bars={len(d):4d} 形态={len(tr):3d} 均值={np.mean([x['net'] for x in tr]) if tr else 0:8.1f}bp DD={dd*100:6.1f}%")

    port = pd.DataFrame(rets) if rets else None
    p = np.array([x["net"] for x in all_tr])
    if len(p) < 5:
        print("样本太少"); return 1
    t = p.mean() / (p.std(ddof=1) / np.sqrt(len(p)))
    wins, losses = p[p > 0], p[p <= 0]
    payoff = (wins.mean() / abs(losses.mean())) if len(losses) and len(wins) else 0.0
    rng = np.random.default_rng(7)
    bs = np.array([rng.choice(p, len(p), replace=True).mean() for _ in range(2000)])
    ci = np.percentile(bs, [2.5, 97.5])
    dts = pd.to_datetime([x["entry"] for x in all_tr])
    oos, val = p[dts.year <= 2022], p[dts.year >= 2023]
    top5 = np.sort(p)[-5:]
    print(f"\n合计 {len(p)} 笔 | 均值 {p.mean():.1f}bp | t={t:.2f} | 命中 {(p>0).mean()*100:.1f}% | 盈亏比 {payoff:.2f}")
    print(f"bootstrap 95% CI [{ci[0]:.0f}, {ci[1]:.0f}] | 样本外(<=2022) n={len(oos)} {oos.mean() if len(oos) else 0:.0f}bp | 验证(>=2023) n={len(val)} {val.mean() if len(val) else 0:.0f}bp")
    print(f"剔除最好 5 笔: {(p.sum()-top5.sum())/max(len(p)-5,1):.0f}bp | 成本x2: {(p-11).mean():.0f}bp")
    print("每标的笔数:", per_sym)
    if port is not None:
        pr = port.fillna(0).mean(axis=1)     # 等权组合日收益
        eq = (1 + pr).cumprod()
        days = (eq.index[-1] - eq.index[0]).days
        cagr = eq.iloc[-1] ** (365 / days) - 1
        dr = eq.pct_change().dropna()
        shp = dr.mean() / dr.std() * np.sqrt(365) if dr.std() > 0 else 0
        dd = (eq / eq.cummax() - 1).min()
        print(f"组合(等权 {len(port.columns)} 币): CAGR {cagr*100:.1f}% | Sharpe {shp:.2f} | DD {dd*100:.1f}%")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
