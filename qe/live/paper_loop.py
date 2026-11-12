"""自动纸交易循环:把处于 paper 状态的策略,按实时行情跑模拟盘。

    python -m qe.live.paper_loop --once      # 跑一轮(用于 cron / 调试)
    python -m qe.live.paper_loop --interval 300

两类策略分开处理:
  · 权重型(trend / xsec_* / funding_carry):算目标权重 → 与纸面持仓对比 → 按 mid±成本成交
  · 事件型(deep_limit):扫最近 5m K 线,识别 Boll<low 信号 → 模拟 −100bp 限价单 → 跟踪成交

状态写在 data/paper/<sid>.json(不碰 DB,DB 只存汇总统计)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .. import ledger, pipeline, registry
from . import testnet_exec as tne
from ..config import Config, Costs
from ..data import hyperliquid as hl
from ..strategies import library as S

STATE = Path(__file__).resolve().parent.parent.parent / "data" / "paper"
FEE_BP = {"deep_limit": 3.0}      # maker 往返
DEFAULT_FEE = 11.0                # taker 往返


def _state(sid):
    STATE.mkdir(parents=True, exist_ok=True)
    p = STATE / f"{sid}.json"
    if p.exists():
        try:
            return json.loads(p.read_text()), p
        except Exception as e:
            # 崩溃时可能写坏:备份坏文件并重建,避免策略永久静默失败
            bad = p.with_suffix(f".corrupt.{int(time.time())}")
            p.replace(bad)
            print(f"[paper] 状态文件损坏,已备份到 {bad.name}: {type(e).__name__}", file=sys.stderr)
    return {"eq": 10000.0, "peak": 10000.0, "trades": 0, "pnl_bp": [], "pos": {},
            "orders": {}, "day0": time.time()}, p


def _save(st, p):
    # 原子写:先写临时文件再 rename,避免进程崩溃留下半截 JSON
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False))
    os.replace(tmp, p)


def _orphans(stgy, st):
    """启动/每轮对账:交易所上存在但本地状态没有的挂单 → 撤掉。

    典型场景:进程在"已下单、未保存状态"之间崩溃,交易所留着孤儿单。
    """
    try:
        from .testnet_exec import TestnetExec
        ex = _tn()
        known = {o.get("oid") for o in (st.get("orders") or {}).values() if o.get("oid")}
        n = 0
        for o in ex.open_orders():
            if int(o["oid"]) not in {int(k) for k in known if k}:
                ex.cancel(o["coin"], o["oid"])
                n += 1
        if n:
            print(f"[paper] {stgy['name']}: 清理 {n} 个孤儿挂单", file=sys.stderr)
    except Exception as e:
        print(f"[paper] 孤儿单对账失败: {type(e).__name__}: {e}", file=sys.stderr)


def _candles(coin, interval="1d", n=400):
    """返回收盘价 Series(index=UTC 时间)。hl.candles(coin, interval, days)"""
    days = n if interval.endswith("d") else max(1, int(n / 288) + 1)   # 5m: 288 根/天
    df = hl.candles(coin, interval, days=max(days, 2))
    if df is None or df.empty:
        return pd.Series(dtype=float)
    return df["close"].astype(float)


def _weights(stgy, coins):
    """按家族算目标权重(只支持权重型)。"""
    px = pd.concat({c: _candles(c) for c in coins}, axis=1).ffill().dropna(how="all")
    if px.empty or len(px) < 120:
        return None
    fam = stgy["family"]
    if fam == "trend":
        sp = stgy["spec"] or {}
        return S.trend_following(px, fast=int(sp.get("fast", 10)), slow=int(sp.get("slow", 50)),
                                 vol_target=float(sp.get("vol_target", 0.25))).iloc[-1]
    if fam == "xsec_momentum":
        return S.xsec_momentum(px).iloc[-1]
    return None


_TN = None


def _tn():
    global _TN
    if _TN is None:
        _TN = tne.TestnetExec()
    return _TN


def tick(stgy):
    sid, name, fam = stgy["id"], stgy["name"], stgy["family"]
    _sp = stgy.get("spec") or {}
    if _sp.get("venue") in ("us_paper", "ibkr", "robinhood"):
        from ..us.loop import tick as us_tick
        return us_tick(stgy)
    if _sp.get("venue") == "testnet" and _sp.get("family") == "breakout":
        return tick_testnet_breakout(stgy)
    if _sp.get("venue") == "testnet":
        return tick_testnet(stgy)
    st, path = _state(sid)
    spec = stgy["spec"] or {}
    lev = float(spec.get("leverage") or 1.0)
    fee = FEE_BP.get(fam, DEFAULT_FEE)

    if fam in ("trend", "xsec_momentum"):
        w = _weights(stgy, stgy["symbols"])
        if w is None:
            return None
        mids = hl.all_mids()
        eq = st["eq"]
        # 目标名义 = 权重 × 权益 × 杠杆
        for c in stgy["symbols"]:
            tgt_w = float(w.get(c, 0.0)) * lev
            px = mids.get(c)
            if not px:
                continue
            cur_notional = st["pos"].get(c, 0.0) * px
            tgt_notional = tgt_w * eq
            delta = tgt_notional - cur_notional
            if abs(delta) < eq * 0.01:     # 1% 阈值,避免噪声换手
                continue
            fee_bp = fee * abs(delta) / eq
            st["eq"] -= eq * fee_bp / 1e4
            st["pos"][c] = st["pos"].get(c, 0.0) + delta / px
            st["trades"] += 1
            st["pnl_bp"].append(-fee_bp)
        st["peak"] = max(st["peak"], st["eq"])

    elif fam == "deep_limit":
        # 事件型:扫最近 5m 线,找 Boll<low;若在 08-20 UTC 且未挂单 → 记一笔模拟限价单
        for c in stgy["symbols"]:
            px5 = _candles(c, "5m", 20)
            if len(px5) < 60:
                continue
            ma = px5.rolling(20).mean()
            sd = px5.rolling(20).std(ddof=0)
            low_band = ma - 2 * sd
            up_band = ma + 2 * sd
            last_t = px5.index[-1]
            key = f"{c}:{last_t.isoformat()}"
            # 状态机(与回测 boll_state 一致):跌破下轨→1,站上上轨→-1,回到中轨→0
            st_key = f"bstate:{c}"
            prev = int(st.get(st_key, 0))
            cl, mid = float(px5.iloc[-1]), float(ma.iloc[-1])
            if cl < float(low_band.iloc[-1]):
                cur = 1
            elif cl > float(up_band.iloc[-1]):
                cur = -1
            elif prev == 1 and cl >= mid:
                cur = 0
            elif prev == -1 and cl <= mid:
                cur = 0
            else:
                cur = prev
            st[st_key] = cur
            sig = (cur > 0)
            hour = last_t.hour
            if sig and 8 <= hour < 20 and key not in st["orders"]:
                ref = float(px5.iloc[-1])
                st["orders"][key] = {"coin": c, "ref": ref, "limit": ref * (1 - 0.0100),
                                     "target": ref * (1 - 0.0100) * (1 + 0.0120),
                                     "ts": time.time(), "filled": False,
                                     "sid": stgy["id"], "sig_ts": (last_t + pd.Timedelta(minutes=5)).timestamp()}
                ledger.add(sid=stgy["id"], coin=c, source="loop", kind="signal",
                           signal_ts=last_t.timestamp(), ref_px=ref,
                           limit_px=ref * (1 - 0.0100),
                           target_px=ref * (1 - 0.0100) * (1 + 0.0120),
                           reason="signal")
        # 用 1m K 线检查成交/止盈(与回测同精度;只看 5 分钟采样会漏掉瞬时下探)
        mids = hl.all_mids()
        for k, o in list(st["orders"].items()):
            px = mids.get(o["coin"])
            c1 = _candles(o["coin"], "1m", 120)
            if c1 is None or len(c1) < 5:
                continue
            # 挂单有效期 5 分钟(与回测一致)
            if not o["filled"]:
                _ts = o.get("sig_ts") or o.get("placed_ts")
                if _ts is None:
                    st["orders"].pop(key, None)
                    continue
                sig_t = pd.Timestamp(_ts, unit="s", tz="UTC")
                # 信号 bar 收盘后才挂单 → 只在其后的 5 分钟内判定成交(避免前视)
                win = c1.loc[sig_t: sig_t + pd.Timedelta(minutes=4)]
                if len(win) == 0:
                    continue
                hit = win.index[win["low"] <= o["limit"]]
                if len(hit):
                    o["filled"] = True
                    o["fill_ts"] = hit[0].timestamp()
                elif sig_t + pd.Timedelta(minutes=5) < c1.index[-1]:
                    st["orders"].pop(k)          # 5 分钟未成交 → 撤单
                    continue
            if px is None:
                continue
            if o["filled"]:
                if px >= o["target"]:
                    net = 120 - FEE_BP["deep_limit"]
                    st["pnl_bp"].append(net)
                    st["trades"] += 1
                    ledger.add(sid=o["sid"], coin=o["coin"], source="loop", kind="trade",
                               signal_ts=o.get("sig_ts") or o.get("placed_ts"), ref_px=o["ref"], limit_px=o["limit"],
                               target_px=o["target"], fill_ts=o.get("fill_ts"),
                               fill_px=o["limit"], exit_ts=time.time(), exit_px=o["target"],
                               hold_min=(time.time() - o.get("fill_ts", time.time())) / 60,
                               gross_bp=120.0, fee_bp=FEE_BP["deep_limit"], net_bp=net,
                               reason="target")
                    st["orders"].pop(k)
                elif time.time() - o.get("fill_ts", time.time()) > 55 * 60:
                    # 超时:挂一个"回到 55 分钟收盘价"的限价单(最多等 240 分钟)
                    if not o.get("rest_px"):
                        o["rest_px"] = px
                        o["rest_ts"] = time.time()
                    elif px >= o["rest_px"]:
                        pnl = (o["rest_px"] / o["limit"] - 1) * 1e4 - FEE_BP["deep_limit"]
                        st["pnl_bp"].append(pnl)
                        st["trades"] += 1
                        ledger.add(sid=o["sid"], coin=o["coin"], source="loop", kind="trade",
                                   signal_ts=o.get("sig_ts") or o.get("placed_ts"), ref_px=o["ref"], limit_px=o["limit"],
                                   target_px=o["target"], fill_ts=o.get("fill_ts"),
                                   fill_px=o["limit"], exit_ts=time.time(), exit_px=o["rest_px"],
                                   hold_min=(time.time() - o.get("fill_ts", time.time())) / 60,
                                   gross_bp=(o["rest_px"] / o["limit"] - 1) * 1e4,
                                   fee_bp=FEE_BP["deep_limit"], net_bp=pnl, reason="timeout_maker")
                        st["orders"].pop(k)
                    elif time.time() - o["rest_ts"] > 240 * 60:
                        pnl = (px / o["limit"] - 1) * 1e4 - FEE_BP["deep_limit"]
                        st["pnl_bp"].append(pnl)
                        st["trades"] += 1
                        ledger.add(sid=o["sid"], coin=o["coin"], source="loop", kind="trade",
                                   signal_ts=o.get("sig_ts") or o.get("placed_ts"), ref_px=o["ref"], limit_px=o["limit"],
                                   target_px=o["target"], fill_ts=o.get("fill_ts"),
                                   fill_px=o["limit"], exit_ts=time.time(), exit_px=px,
                                   hold_min=(time.time() - o.get("fill_ts", time.time())) / 60,
                                   gross_bp=(px / o["limit"] - 1) * 1e4,
                                   fee_bp=FEE_BP["deep_limit"], net_bp=pnl, reason="stuck")
                        st["orders"].pop(k)
        # 清理 6 小时前的未成交挂单
        for k, o in list(st["orders"].items()):
            if not o["filled"] and time.time() - o["ts"] > 6 * 3600:
                st["orders"].pop(k)
        st["peak"] = max(st["peak"], st["eq"])

    else:
        return None

    _save(st, path)
    days = (time.time() - st["day0"]) / 86400
    dd = (st["eq"] / st["peak"] - 1) if st["peak"] else 0.0
    stats = {"days": round(days, 2), "trades": st["trades"],
             "mean_bp": round(float(np.mean(st["pnl_bp"])), 2) if st["pnl_bp"] else 0.0,
             "equity": round(st["eq"], 2), "max_dd": round(dd, 4)}
    pipeline.paper_tick(sid, stats, source="loop")
    return stats



def _mk_signal(df, name, series: bool = False):
    """按策略声明的信号口径算入场条件(与回测同一套定义)。

    series=True 返回**整段布尔序列**(供对账器重建基准);
    默认只返回最后一根(实盘 tick 用)。
    一份定义两处用,避免"实盘信号"和"对账基准"逻辑漂移。
    """
    import pandas as pd

    if df is None or len(df) < 30:
        return pd.Series(dtype=bool) if series else False
    c = df["close"]
    if name in ("rsi14<30", "rsi1430"):
        d = c.diff()
        up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        r = 100 - 100 / (1 + up / dn.replace(0, float("nan"))).fillna(50)
        cond = r < 30
    elif name == "keltner":
        ema = c.ewm(span=20, adjust=False).mean()
        tr = (df["high"] - df["low"]).ewm(alpha=1 / 20, adjust=False).mean()
        cond = c < (ema - 2 * tr)
    elif name == "vwap-2sd" and "volume" in df:
        v = df["volume"]
        vwap = (c * v).rolling(288).sum() / v.rolling(288).sum()
        sd = c.rolling(288).std()
        cond = c < (vwap - 2 * sd)
    elif name == "boll40x25":
        ma, sd = c.rolling(40).mean(), c.rolling(40).std()
        cond = c < (ma - 2.5 * sd)
    else:
        ma, sd = c.rolling(20).mean(), c.rolling(20).std()
        cond = c < (ma - 2 * sd)
    cond = cond.fillna(False).astype(bool)
    return cond if series else bool(cond.iloc[-1])


def _candles_df(coin, interval="5m", days=2, drop_forming=True):
    """取 K 线;默认**丢掉正在形成的那根**。

    为什么必须丢:HL 返回的最后一根是当前未收盘 bar,它的 close 是"现价"。
    用它判信号 = 在 bar 还没收盘时做决定,回测里根本不存在这种信号
    (对账器实测:不丢的话信号一致率只有 33%)。
    """
    try:
        df = hl.candles(coin, interval, days)
    except Exception:
        return None
    if df is None or not len(df) or not drop_forming:
        return df
    interval_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
                   "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}.get(interval, 300_000)
    now_ms = int(time.time() * 1000)
    last_start = int(df.index[-1].timestamp() * 1000)
    if now_ms < last_start + interval_ms:      # 还没收盘
        return df.iloc[:-1]
    return df


def tick_testnet_breakout(stgy):
    """venue=testnet + family=breakout:1h 突破 → 市价进;跌破中轨 → 市价出。"""
    sid = stgy["id"]
    ex = _tn()
    st, path = _state(sid)
    st.setdefault("pos", {})
    spec = stgy["spec"] or {}
    notional = float(spec.get("notional_usd") or 20.0)
    kind = spec.get("signal") or "atr"
    notes = []
    for c in stgy["symbols"]:
        df = _candles_df(c, "1h", 30)
        if df is None or len(df) < 60:
            continue
        cl = df["close"]
        last = float(cl.iloc[-1])
        ema20 = float(cl.ewm(span=20, adjust=False).mean().iloc[-1])
        if kind == "atr":
            tr = float((df["high"] - df["low"]).ewm(alpha=1 / 20, adjust=False).mean().iloc[-1])
            entry = last > ema20 + 2 * tr
        else:
            ma = float(cl.rolling(20).mean().iloc[-1])
            sd = float(cl.rolling(20).std().iloc[-1])
            entry = last > ma + 2 * sd
        pos = st["pos"].get(c)
        if pos is None and entry:
            r = ex.place_market(c, True, notional)
            st["pos"][c] = {"entry_px": r["px"], "entry_ts": time.time(), "sz": r["sz"]}
            ledger.add(sid=sid, coin=c, source="testnet", kind="signal",
                       signal_ts=df.index[-1].timestamp(), ref_px=last, limit_px=r["px"])
            notes.append(f"{c} 市价买入 {r['sz']} @ {r['px']:.2f}")
        elif pos is not None and last < ema20:
            r = ex.place_market(c, False, pos["sz"] * pos["entry_px"])
            gross = (r["px"] / pos["entry_px"] - 1) * 1e4
            net = gross - 11.0
            ledger.add(sid=sid, coin=c, source="testnet", kind="trade",
                       signal_ts=pos["entry_ts"], ref_px=pos["entry_px"], limit_px=pos["entry_px"],
                       fill_ts=pos["entry_ts"], fill_px=pos["entry_px"],
                       exit_ts=time.time(), exit_px=r["px"],
                       hold_min=(time.time() - pos["entry_ts"]) / 60.0,
                       gross_bp=gross, fee_bp=9.0, net_bp=net, reason="ma_exit")
            st["pos"].pop(c)
            notes.append(f"{c} 市价平仓 @ {r['px']:.2f} → {net:+.1f}bp")
    tr = [x for x in ledger.rows(sid, source="testnet") if x["kind"] == "trade"]
    out = {"days": 0.0, "trades": len(tr), "mean_bp": 0.0, "equity": 10000.0, "orders": len(st["pos"])}
    if tr:
        nets = [float(x["net_bp"]) for x in tr]
        first = st.setdefault("first_ts", time.time())
        days = max((time.time() - first) / 86400.0, 0.01)
        stats = {"days": round(days, 3), "trades": len(nets),
                 "mean_bp": round(sum(nets) / len(nets), 2),
                 "max_dd": round(min(0.0, min(nets) / 100.0), 4)}
        pipeline.paper_tick(sid, stats, source="loop")
        out.update(days=stats["days"], trades=stats["trades"], mean_bp=stats["mean_bp"])
    _save(st, path)
    if notes:
        print(f"  [{stgy['name']}] " + " | ".join(notes))
    return out


def _reconcile_positions(stgy, st):
    """持仓对账:交易所持仓必须与本地状态一致,否则接管或平掉。

    不一致的场景:手工测试留下的仓位、进程崩溃后状态丢失、elsewhere 平仓。
    """
    try:
        ex = _tn()
        for c in stgy["symbols"]:
            qty = float(ex.position(c) or 0.0)
            od = (st.get("orders") or {}).get(c) or {}
            if abs(qty) > 0 and not od.get("filled"):
                # 交易所有多单但我们不知道 → 接管(按当前 mid 记为入场,交给正常出场逻辑)
                mp = ex.mid(c)
                st.setdefault("orders", {})[c] = {
                    "oid": None, "coin": c, "ref": mp, "limit": mp, "tp_px": mp * 1.012,
                    "sz": abs(qty), "sig_ts": time.time(), "placed_ts": time.time(),
                    "filled": True, "fill_px": mp, "fill_ts": time.time(), "_adopted": True}
                print(f"[paper] {stgy['name']} {c} 发现未记账持仓 {qty},已接管(按 mid {mp} 记入场)",
                      file=sys.stderr)
            elif abs(qty) < 1e-9 and od.get("filled"):
                # 本地以为有仓,交易所已空(被手工平掉/爆仓)→ 记账并清状态
                mp = ex.mid(c)
                gross = (mp / od["fill_px"] - 1) * 1e4
                ledger.add(sid=stgy["id"], coin=c, source="testnet", kind="trade",
                           signal_ts=od.get("sig_ts"), ref_px=od.get("ref"),
                           limit_px=od.get("limit"), fill_ts=od.get("fill_ts"),
                           fill_px=od["fill_px"], exit_ts=time.time(), exit_px=mp,
                           hold_min=(time.time() - od["fill_ts"]) / 60,
                           gross_bp=gross, fee_bp=3.0, net_bp=gross - 3.0,
                           reason="external_close")
                st["orders"].pop(c, None)
                print(f"[paper] {stgy['name']} {c} 持仓已在别处平掉,按 mid {mp} 记账",
                      file=sys.stderr)
    except Exception as e:
        print(f"[paper] 持仓对账失败: {type(e).__name__}: {e}", file=sys.stderr)


def tick_testnet(stgy):
    """venue=testnet:真实挂单。由 pipeline 状态驱动,不用 cron。

    简化点(与回测的已知差异,对账器会量化):
      · 超时出场用市价平仓(taker),回测是挂限价再等 240 分钟 → 我们更保守
    """
    from ..strict.data import boll_state

    sid = stgy["id"]
    ex = _tn()
    st, path = _state(sid)
    st.setdefault("orders", {})
    # 把在场挂单的价格写给 L2 录制器 → 价格逼近时自动切高频
    try:
        _watch = {c: (od or {}).get("limit") for c, od in (st.get("orders") or {}).items()
                  if (od or {}).get("limit") and not (od or {}).get("filled")}
        (Path(__file__).resolve().parent.parent.parent / "data" / "l2_watch.json").write_text(
            json.dumps({k: float(v) for k, v in _watch.items()}))
    except Exception:
        pass

    ex.arm_deadman(120)                    # 进程崩溃 → 交易所侧自动撤单(死手开关)
    if not st.get("_orphan_checked"):
        _orphans(stgy, st)                 # 启动首轮清理交易所上的孤儿单
        st["_orphan_checked"] = time.time()
        _save(st, path)
    _reconcile_positions(stgy, st)
    spec = stgy["spec"] or {}
    notional = float(spec.get("notional_usd") or 20.0)
    now = time.time()
    oo_ids = {o["oid"] for o in ex.open_orders()}
    notes = []

    for c in stgy["symbols"]:
        od = st["orders"].get(c)
        pos = ex.position(c)

        # ---------- ① 已成交的持仓:管理出场 ----------
        if od and od.get("filled"):
            held = (now - od["fill_ts"]) / 60.0
            if abs(pos) < 1e-9:
                g = (od["tp_px"] / od["fill_px"] - 1) * 1e4
                net = g - 3.0
                ledger.add(sid=sid, coin=c, source="testnet", kind="trade", signal_ts=od.get("sig_ts"),
                           ref_px=od["ref"], limit_px=od["limit"], fill_ts=od["fill_ts"],
                           fill_px=od["fill_px"], exit_ts=now, exit_px=od["tp_px"],
                           hold_min=held, gross_bp=g, fee_bp=3.0, net_bp=net, reason="target")
                st["orders"].pop(c)
                notes.append(f"{c} 止盈 {net:+.1f}bp")
                continue
            if od.get("tp_oid") and od["tp_oid"] not in oo_ids:
                od["tp_oid"] = None
            if od.get("tp_oid") is None:
                r = ex.place_tp(c, od["sz"], od["tp_px"])
                od["tp_oid"] = r.get("oid")
            if held > 55 and abs(pos) > 1e-9:
                ex.close_market(c, pos)
                g = (float(ex.mid(c)) / od["fill_px"] - 1) * 1e4
                net = g - 6.5
                ledger.add(sid=sid, coin=c, source="testnet", kind="trade", signal_ts=od.get("sig_ts"),
                           ref_px=od["ref"], limit_px=od["limit"], fill_ts=od["fill_ts"],
                           fill_px=od["fill_px"], exit_ts=now, exit_px=float(ex.mid(c)),
                           hold_min=held, gross_bp=g, fee_bp=6.5, net_bp=net,
                           reason="timeout_taker")
                st["orders"].pop(c)
                notes.append(f"{c} 超时平仓 {net:+.1f}bp")
            continue

        # ---------- ② 有挂单未成交 ----------
        if od and not od.get("filled"):
            age_min = (now - od["placed_ts"]) / 60.0
            if od["oid"] not in oo_ids:
                if abs(pos) > 1e-9:
                    od.update(filled=True, fill_ts=now, fill_px=od["limit"], sz=abs(pos))
                    r = ex.place_tp(c, abs(pos), od["limit"] * (1 + 0.0120))
                    od["tp_oid"] = r.get("oid")
                    notes.append(f"{c} 成交 @ {od['limit']:.1f}")
                else:
                    st["orders"].pop(c)
                    notes.append(f"{c} 挂单消失(撤单/未成交)")
            elif age_min > 5:
                ex.cancel(c, od["oid"])
                st["orders"].pop(c)
                notes.append(f"{c} 5 分钟未成交,撤单")
            continue

        # ---------- ③ 新信号 ----------
        bdf = _candles_df(c, "5m", 2)
        if bdf is None or len(bdf) < 30:
            continue
        _sg = spec.get("signal") or "boll20"
        if not (_mk_signal(bdf, _sg) and 8 <= bdf.index[-1].hour < 20):
            continue
        r = ex.place_alo(c, True, notional, 100.0)
        if not r.get("oid"):
            notes.append(f"{c} 下单失败 {str(r.get('raw'))[:60]}")
            continue
        st["orders"][c] = {"oid": r["oid"], "coin": c, "ref": ex.mid(c), "limit": r["px"],
                           "tp_px": r["px"] * 1.0120, "sz": r["sz"],
                           "sig_ts": bdf.index[-1].timestamp(), "placed_ts": now,
                           "filled": False, "tp_oid": None}
        ledger.add(sid=sid, coin=c, source="testnet", kind="signal",
                   signal_ts=bdf.index[-1].timestamp(), ref_px=ex.mid(c),
                   limit_px=r["px"], target_px=r["px"] * 1.0120)
        notes.append(f"{c} 挂单 {r['sz']} @ {r['px']:.1f} (oid {r['oid']})")

    # 汇总到 pipeline(统一返回结构,与模拟分支一致)
    tr = [x for x in ledger.rows(sid, source="testnet") if x["kind"] == "trade"]
    out = {"days": 0.0, "trades": len(tr), "mean_bp": 0.0, "equity": 10000.0, "orders": len(st["orders"])}
    if tr:
        nets = [float(x["net_bp"]) for x in tr]
        first = st.setdefault("first_ts", now)
        days = max((now - first) / 86400.0, 0.01)
        stats = {"days": round(days, 3), "trades": len(nets),
                 "mean_bp": round(sum(nets) / len(nets), 2),
                 "max_dd": round(min(0.0, min(nets) / 100.0), 4)}
        pipeline.paper_tick(sid, stats, source="loop")
        out.update(days=stats["days"], trades=stats["trades"], mean_bp=stats["mean_bp"])
    _save(st, path)
    if notes:
        print(f"  [{stgy['name']}] " + " | ".join(notes))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=300)
    a = ap.parse_args()
    # HL SDK 的 requests 没有超时参数 —— 设全局 socket 超时,避免单次请求挂死整个循环
    import socket
    socket.setdefaulttimeout(15)
    hb = STATE / "heartbeat"
    while True:
        t0 = time.time()
        rows = [s for s in registry.all() if s["state"] == "paper"]
        for s in rows:
            try:
                st = tick(s)
                if st:
                    print(f"[paper] {s['name']:18s} days={st['days']:.1f} trades={st['trades']} "
                          f"mean={st['mean_bp']}bp eq={st['equity']}")
            except Exception as e:
                print(f"[paper] {s['name']} 失败: {type(e).__name__}: {e}")
        # 心跳:文件时间戳可被外部监控(service active 不等于循环健康)
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            hb.write_text(f"{int(time.time())} {len(rows)}\n")
        except Exception:
            pass
        if a.once:
            break
        # 漂移校正:上一轮耗时不该累加到周期里
        time.sleep(max(5, a.interval - (time.time() - t0)))


if __name__ == "__main__":
    main()
