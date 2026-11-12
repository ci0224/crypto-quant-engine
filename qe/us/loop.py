"""美股策略循环:由 pipeline 的 venue 驱动(不是 cron)。

venue=us_paper → PaperBroker(本地模拟成交)
venue=ibkr     → IBKRBroker(需 IB Gateway)
venue=robinhood→ RobinhoodBroker(非官方,需显式开关)

月度调仓的横截面动量:每 21 个交易日,按 12-1 动量取前 20% 等权持有。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .. import ledger
from .broker import Broker, get_broker
from .data import get_many, load_daily
from .signals import mom_12_1

STATE = Path(__file__).resolve().parent.parent.parent / "data" / "us"
REBAL_DAYS = 21


def _state(sid):
    STATE.mkdir(parents=True, exist_ok=True)
    f = STATE / f"loop_{sid}.json"
    if f.exists():
        try:
            return json.loads(f.read_text()), f
        except Exception:
            f.replace(f.with_suffix(".bad"))
    return {"last_rebal": 0, "trades": 0}, f


def _save(s, f):
    t = f.with_suffix(".tmp")
    t.write_text(json.dumps(s, indent=1))
    t.replace(f)


def targets(universe: list[str], top_frac: float = 0.2) -> dict[str, float]:
    """按 12-1 动量选前 top_frac,等权。"""
    data = get_many(universe)
    sc = {}
    for sym, df in data.items():
        try:
            s = mom_12_1(df)
            if bool(s.iloc[-1]):
                c = df["close"]
                sc[sym] = float(c.iloc[-22] / c.iloc[-253] - 1)
        except Exception:
            continue
    if not sc:
        return {}
    k = max(1, int(len(data) * top_frac))
    top = sorted(sc, key=lambda x: -sc[x])[:k]
    return {s: 1.0 / len(top) for s in top}


def tick(stgy: dict, force: bool = False) -> dict:
    """一轮:是否需要调仓 → 卖不在目标的 → 买目标中的。"""
    sid, spec = stgy["id"], (stgy.get("spec") or {})
    venue = spec.get("venue", "us_paper")
    uni = spec.get("universe_list") or []
    st, f = _state(sid)
    now = time.time()
    if not force:
        if not Broker.market_open():
            return {"skipped": "非交易时段"}
        if now - float(st.get("last_rebal", 0)) < REBAL_DAYS * 86400:
            return {"skipped": f"距上次调仓 {(now-st['last_rebal'])/86400:.0f} 天 < {REBAL_DAYS}"}
    if not uni:
        return {"skipped": "universe 为空"}
    b = get_broker(venue)
    tg = targets(uni, float(spec.get("top_frac", 0.2)))
    if not tg:
        return {"skipped": "无目标(动量全负)"}
    eq = b.account()["equity"]
    acted = []
    # 卖出:不在目标里的持仓
    pos = {s: b.position(s) for s in set(uni) | set(tg)}
    for s, q in pos.items():
        if q > 1e-9 and s not in tg:
            r = b.sell(s, q)
            if r.get("ok"):
                px = r.get("avg_px") or 0
                ledger.add(sid=sid, coin=s, source=venue, kind="trade",
                           signal_ts=now, exit_ts=now, exit_px=px, reason="rebalance_out")
                acted.append(f"卖 {s} {q:.2f}")
    # 买入:目标里没持仓的
    for s, w in tg.items():
        if pos.get(s, 0) > 1e-9:
            continue
        px = b.quote(s).get("last") or 0
        if px <= 0:
            continue
        qty = (eq * w) / px
        if qty * px < 1.0:
            continue
        r = b.buy(s, qty)
        if r.get("ok"):
            ledger.add(sid=sid, coin=s, source=venue, kind="trade",
                       signal_ts=now, fill_ts=now, fill_px=r.get("avg_px"),
                       reason="rebalance_in")
            acted.append(f"买 {s} {qty:.2f}@{r.get('avg_px'):.2f}")
    st["last_rebal"] = now
    st["trades"] = st.get("trades", 0) + len(acted)
    _save(st, f)
    return {"ok": True, "acted": acted, "n_targets": len(tg), "equity": round(eq, 2)}
