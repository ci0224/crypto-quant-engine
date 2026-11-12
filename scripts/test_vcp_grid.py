#!/usr/bin/env python3
"""VCP 参数网格:找 n>=30 的可统计组合(注意多重检验)。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.test_vcp import COST, SYMS, daily, simulate, vcp_signal
from qe.backtest.engine import Backtester

GRID = [(vm, hf, tp, tr) for vm in (1.0, 1.3)
        for hf in (0.65, 0.75)
        for tp in (0.30, 0.40)
        for tr in (2.0, 3.0)]


def main() -> int:
    data = {}
    for s in SYMS:
        try:
            d = daily(s)
            if len(d) >= 300:
                data[s] = d
        except Exception:
            pass
    rows = []
    for vm, hf, tp, tgt in GRID:
        allnet = []
        cagrs, dds = [], []
        for s, d in data.items():
            sig = vcp_signal(d, tight_pct=tp, vol_mult=vm, hi_frac=hf)
            w, tr = simulate(d, sig, target_r=tgt)
            if tr:
                allnet += [t["net"] for t in tr]
            try:
                r = Backtester(d[["close"]].rename(columns={"close": "X"}), COST,
                               initial=10_000.0, max_leverage=1.0).run(w.rename("X").to_frame())
                eq = pd.Series(r.equity, index=d.index).dropna()
                yrs = (d.index[-1] - d.index[0]).days / 365.25
                cagrs.append((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1)
                dds.append(float((eq / eq.cummax() - 1).min()))
            except Exception:
                pass
        if not allnet:
            continue
        a = np.array(allnet)
        n = len(a)
        t = a.mean() / (a.std(ddof=1) / np.sqrt(n)) if n > 2 and a.std(ddof=1) > 0 else np.nan
        rows.append(dict(vol=vm, hi=hf, tight=tp, tgt=tgt, n=n, mean=a.mean(),
                         t=t, hit=(a > 0).mean(),
                         cagr=np.mean(cagrs) * 100 if cagrs else np.nan,
                         dd=np.mean(dds) * 100 if dds else np.nan))
    df = pd.DataFrame(rows).sort_values("t", ascending=False)
    pd.set_option("display.width", 200)
    print(df.to_string(index=False,
                       formatters={"mean": "{:.1f}".format, "t": "{:.2f}".format,
                                   "hit": "{:.1%}".format, "cagr": "{:.1f}".format,
                                   "dd": "{:.1f}".format}))
    ok = df[(df["n"] >= 30) & (df["t"] >= 3.0)]
    print(f"\n样本>=30 且 t>=3.0 的组合: {len(ok)} / {len(df)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
