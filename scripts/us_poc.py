#!/usr/bin/env python3
"""美股 PoC:Nasdaq 免费日线 → 直接喂给现有引擎。"""
import sys, json, time, urllib.request
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qe.config import Costs
from qe.backtest.engine import Backtester

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}

def daily(sym, frm="2015-01-01"):
    u = f"https://api.nasdaq.com/api/quote/{sym}/historical?assetclass=stocks&fromdate={frm}&limit=9999"
    req = urllib.request.Request(u, headers=UA)
    d = json.load(urllib.request.urlopen(req, timeout=30))["data"]
    rows = d["tradesTable"]["rows"]
    df = pd.DataFrame(rows)
    for c in ("close", "open", "high", "low"):
        df[c] = df[c].str.replace(r"[$,]", "", regex=True).astype(float)
    df["volume"] = df["volume"].str.replace(",", "", regex=False).astype(float)
    df["date"] = pd.to_datetime(df["date"], format="%m/%d/%Y")
    return df.sort_values("date").set_index("date")

SYMS = ["AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AMD","NFLX","AVGO"]

def run():
    rows = []
    eqs = {}
    for s in SYMS:
        try:
            d = daily(s)
        except Exception as e:
            print(f"  {s} 失败: {e}"); continue
        c = d["close"]
        sma = c.rolling(200).mean()
        w = (c > sma).astype(float)
        bt = Backtester(c.to_frame("X"), Costs(taker_bps=0.0, slippage_bps=1.0),
                        initial=10000.0, max_leverage=1.0)
        r = bt.run(w.to_frame("X"))
        eq = pd.Series(np.asarray(r.equity, dtype=float), index=c.index)
        ret = eq.pct_change().dropna()
        yrs = (c.index[-1] - c.index[0]).days / 365.25
        cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
        sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0
        dd = (eq / eq.cummax() - 1).min()
        bh = (c.iloc[-1] / c.iloc[0]) ** (1 / yrs) - 1
        rows.append((s, len(d), cagr, sharpe, dd, bh))
        eqs[s] = ret
    print(f"{'标的':6s} {'年数':>5s} {'策略CAGR':>9s} {'Sharpe':>7s} {'回撤':>8s} {'买入持有':>9s}")
    for s, n, cagr, sh, dd, bh in rows:
        print(f"{s:6s} {n/252:5.1f} {cagr*100:8.1f}% {sh:7.2f} {dd*100:7.1f}% {bh*100:8.1f}%")
    port = pd.DataFrame(eqs).mean(axis=1)
    eqp = (1 + port).cumprod()
    yrs = len(port) / 252
    print(f"\n等权组合: CAGR {((eqp.iloc[-1])**(1/yrs)-1)*100:.1f}% | "
          f"Sharpe {port.mean()/port.std()*np.sqrt(252):.2f} | "
          f"回撤 {((eqp/eqp.cummax()-1).min())*100:.1f}%")

if __name__ == "__main__":
    run()
