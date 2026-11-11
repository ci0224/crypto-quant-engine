import numpy as np
import pandas as pd


def segments(w):
    s = np.sign(w).astype(int)
    out, i, n = [], 0, len(s)
    while i < n:
        if s[i] == 0:
            i += 1
            continue
        j = i
        while j + 1 < n and s[j + 1] == s[i]:
            j += 1
        out.append((i, j, s[i]))
        i = j + 1
    return out


def trade_stats(bars, w, cost_bp=11.0, n_boot=2000, seed=7):
    o = bars["open"].to_numpy(float)
    h = bars["high"].to_numpy(float)
    l = bars["low"].to_numpy(float)
    idx = bars.index
    s = np.sign(np.asarray(w, dtype=float))
    tr = []
    for a, b, side in segments(s):
        if b + 1 >= len(o):
            break
        ep, xp = o[a + 1], o[b + 1]
        gross = (xp / ep - 1) * 1e4 * side
        seg = slice(a + 1, b + 2)
        mae = ((l[seg].min() / ep - 1) if side > 0 else (h[seg].max() / ep - 1)) * 1e4
        tr.append(dict(t=idx[b + 1], gross=gross, net=gross - cost_bp,
                       hold=(b - a + 1), mae=mae, side=side))
    if not tr:
        return None
    net = np.array([x["net"] for x in tr])
    ts = pd.DatetimeIndex([x["t"] for x in tr])
    day = pd.Series(net, index=ts.normalize()).groupby(level=0).sum()
    days = pd.date_range(idx[0].normalize(), idx[-1].normalize(), freq="D", tz="UTC")
    d = day.reindex(days, fill_value=0.0)
    sd = d.std(ddof=1) or 1e-9
    rng = np.random.default_rng(seed)
    boots = [rng.choice(net, len(net), replace=True).mean() for _ in range(n_boot)]
    lo, hi = float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))
    win = net[net > 0]
    los = net[net < 0]
    oos = net[ts <= "2022-12-31"]
    val = net[ts >= "2023-01-01"]
    rec = net[ts >= (ts.max() - pd.Timedelta(days=365))]
    order = np.argsort(net)[::-1]
    keep = np.ones(len(net), bool)
    keep[order[:5]] = False
    return dict(
        n=int(len(net)), mean=float(net.mean()), med=float(np.median(net)),
        std=float(net.std(ddof=1)), t=float(d.mean() / sd * np.sqrt(len(d))),
        ci_lo=lo, ci_hi=hi, hit=float((net > 0).mean()),
        payoff=float(win.mean() / abs(los.mean())) if len(los) and len(win) else np.nan,
        mae=float(min(x["mae"] for x in tr)), hold_med=float(np.median([x["hold"] for x in tr])),
        oos=float(oos.mean()) if len(oos) else np.nan, oos_t=float(oos.mean() / (oos.std(ddof=1) / np.sqrt(len(oos)))) if len(oos) > 2 else np.nan,
        val=float(val.mean()) if len(val) else np.nan, val_t=float(val.mean() / (val.std(ddof=1) / np.sqrt(len(val)))) if len(val) > 2 else np.nan,
        rec_n=int(len(rec)), rec=float(rec.mean()) if len(rec) else np.nan,
        drop5=float(net[keep].mean()), sharpe_daily=float(d.mean() / sd * np.sqrt(365)),
        trades=tr,
    )
