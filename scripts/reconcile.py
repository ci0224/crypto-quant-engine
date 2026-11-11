#!/usr/bin/env python3
"""回测-实盘对账:同一时间窗,逐条比对信号与成交。

    python scripts/reconcile.py --strategy deep_limit_btc
    python scripts/reconcile.py --strategy deep_limit_btc --days 30

输出 5 个偏差指标:
  ① 信号一致率  ② 成交率  ③ 成交价偏差  ④ 每笔净收益偏差  ⑤ 漏单/多单清单
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe import ledger, registry
from qe.strict.data import boll_state, load_1m, load_funding, resample
from qe.strict.engine import StrictCfg, run

BUCKET = 300  # 5 分钟桶


def bucket(ts: float) -> int:
    return int(ts // BUCKET)



def replay_loop(b1: pd.DataFrame, b5: pd.DataFrame, sid: str, coin: str) -> int:
    """按【修改后的纸交易规则】重放(1m 精度 + 5 分钟挂单到期 + 超时挂限价)。"""
    close5 = b5["close"]
    ma = close5.rolling(20).mean()
    sd = close5.rolling(20).std(ddof=0)
    lo_b, up_b = ma - 2 * sd, ma + 2 * sd
    state, st = 0, []
    for i in range(len(b5)):
        if not np.isfinite(ma.iloc[i]):
            st.append(0); continue
        c = float(close5.iloc[i])
        if c < float(lo_b.iloc[i]): state = 1
        elif c > float(up_b.iloc[i]): state = -1
        elif state == 1 and c >= float(ma.iloc[i]): state = 0
        elif state == -1 and c <= float(ma.iloc[i]): state = 0
        st.append(state)
    state = pd.Series(st, index=b5.index)
    sig = (state > 0) & (b5.index.hour >= 8) & (b5.index.hour < 20)

    n, free_from = 0, pd.Timestamp.min.tz_localize("UTC")
    for t0 in b5.index[sig]:
        ref = float(close5.loc[t0])
        limit = ref * 0.99
        target = limit * 1.012
        ledger.add(sid=sid, coin=coin, source="loop", kind="signal",
                   signal_ts=t0.timestamp(), ref_px=ref, limit_px=limit,
                   target_px=target, reason="signal")
        sig_t = t0 + pd.Timedelta(minutes=5)          # 信号在 bar 收盘时刻生效
        if sig_t < free_from:                          # 持仓期间不开新仓
            continue
        win = b1.loc[sig_t: sig_t + pd.Timedelta(minutes=4)]
        if win.empty:
            continue
        hit = win.index[win["low"] <= limit]
        if not len(hit):
            continue                          # 5 分钟未成交 → 撤单
        ft = hit[0]
        hold = b1.loc[ft + pd.Timedelta(minutes=1): ft + pd.Timedelta(minutes=55)]
        if hold.empty:
            continue
        win2 = hold.index[hold["high"] >= target]
        if len(win2):
            xt, xp, reason = win2[0], target, "target"
        else:
            rest_px = float(b1.loc[hold.index[-1], "close"])
            after = b1.loc[hold.index[-1] + pd.Timedelta(minutes=1):
                          hold.index[-1] + pd.Timedelta(minutes=240)]
            w3 = after.index[after["high"] >= rest_px] if len(after) else []
            if len(w3):
                xt, xp, reason = w3[0], rest_px, "timeout_maker"
            elif len(after):
                xt, xp, reason = after.index[-1], float(after["close"].iloc[-1]), "stuck"
            else:
                xt, xp, reason = hold.index[-1], rest_px, "stuck"
        net = (xp / limit - 1) * 1e4 - 3.0
        ledger.add(sid=sid, coin=coin, source="loop", kind="trade",
                   signal_ts=t0.timestamp(), ref_px=ref, limit_px=limit, target_px=target,
                   fill_ts=ft.timestamp(), fill_px=limit, exit_ts=xt.timestamp(), exit_px=xp,
                   hold_min=(xt - ft).total_seconds() / 60,
                   gross_bp=(xp / limit - 1) * 1e4, fee_bp=3.0, net_bp=net, reason=reason)
        n += 1
        free_from = xt + pd.Timedelta(minutes=1)       # 出场后才允许下一笔
    return n

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--days", type=int, default=30, help="对账回溯天数(默认 30)")
    ap.add_argument("--backfill", action="store_true",
                    help="用历史数据按【纸交易规则】重放,填充 source=loop(用于验证逻辑一致性)")
    a = ap.parse_args()

    s = next((x for x in registry.all() if x["name"] == a.strategy), None)
    if not s:
        print(f"❌ 策略不存在: {a.strategy}")
        return 1
    coin = (s["symbols"] or ["BTC"])[0]
    sid = s["id"]

    since = time.time() - a.days * 86400
    loop_sig = ledger.rows(sid, "loop", "signal", since)
    loop_tr = ledger.rows(sid, "loop", "trade", since)
    print(f"【{s['name']}】窗口 = 最近 {a.days} 天 | 标的 {coin}")
    print(f"  实盘/纸交易记录:信号 {len(loop_sig)} | 成交 {len(loop_tr)}")

    # ---- 跑同窗口回测,写入 source=backtest ----
    b1 = load_1m(coin)
    start = pd.Timestamp(since, unit="s", tz="UTC") - pd.Timedelta(days=1)
    b1 = b1.loc[start:]
    b5 = resample(b1, 5)
    if a.backfill:
        ledger.clear(sid, "loop")
        n_r = replay_loop(b1, b5, sid, coin)
        loop_sig = ledger.rows(sid, "loop", "signal", since)
        loop_tr = ledger.rows(sid, "loop", "trade", since)
        print(f"  🔁 已按纸交易规则回放:信号 {len(loop_sig)} / 成交 {n_r}")
    sig = (boll_state(b5["close"], 20, 2.0) > 0).to_numpy(dtype=bool)
    cfg = StrictCfg(through_bp=2.0, fill_rate=0.85, taker_exit_prob=0.514)
    r = run(b1, b5, sig, load_funding(coin), cfg)
    td = r["trades"]
    ledger.clear(sid, "backtest")
    for _, t in td.iterrows():
        ledger.add(sid=sid, coin=coin, source="backtest", kind="trade",
                   signal_ts=t["t"].timestamp(), fill_ts=t["t"].timestamp(),
                   exit_ts=t["exit_t"].timestamp(), hold_min=t["hold_min"],
                   gross_bp=t["gross_bp"], fee_bp=t["fee_bp"], funding_bp=t["funding_bp"],
                   impact_bp=t["impact_bp"], net_bp=t["net_bp"], reason=t["reason"])
    print(f"  同窗口回测:{(sig & (b5.index.hour >= 8) & (b5.index.hour < 20)).sum()} 信号 "
          f"| {len(td)} 成交")

    if not len(td) and not loop_tr:
        print("\n  ⚠️ 窗口内两边都没有成交 —— 样本不足,只能比对信号数。")
        return 0

    # ---- ① 信号一致率(按 5 分钟桶对齐) ----
    ls = {bucket(x["signal_ts"]) for x in loop_sig}
    bs_ts = set()
    lo_idx = b5.index[(boll_state(b5["close"], 20, 2.0) > 0).to_numpy(dtype=bool) &
                      (b5.index.hour >= 8) & (b5.index.hour < 20)]
    bs_ts = {bucket(t.timestamp()) for t in lo_idx}
    inter = ls & bs_ts
    print(f"\n  ① 信号一致率:实盘 {len(ls)} / 回测 {len(bs_ts)} / 交集 {len(inter)} "
          f"→ {'%.1f%%' % (len(inter)/max(len(bs_ts),1)*100) if bs_ts else 'n/a'}")

    # ---- ② 成交率 ----
    fr_loop = len(loop_tr) / max(len(ls), 1)
    fr_bt = len(td) / max(len(bs_ts), 1)
    print(f"  ② 成交率:实盘 {fr_loop*100:.1f}% | 回测 {fr_bt*100:.1f}%")

    # ---- ③④ 每笔偏差(按成交时间 ±10 分钟容差配对) ----
    def ts_of_loop(r):
        return r.get("fill_ts") or r.get("signal_ts") or 0

    bt = [(float(t["t"].timestamp()), t) for _, t in td.iterrows()]
    used, pairs, miss = set(), [], []
    for r in loop_tr:
        lt = ts_of_loop(r)
        cand = [(i, ts, t) for i, (ts, t) in enumerate(bt)
                if i not in used and abs(ts - lt) <= 600]
        if cand:
            i, ts, t = min(cand, key=lambda x: abs(x[1] - lt))
            used.add(i)
            pairs.append((r, t))
        else:
            miss.append(r)
    extra = [t for i, (ts, t) in enumerate(bt) if i not in used]
    if pairs:
        dnet = [float(r["net_bp"]) - float(t["net_bp"]) for r, t in pairs]
        print(f"  ③ 配对 {len(pairs)}/{len(loop_tr)} 笔(±10 分钟) | 净收益偏差:"
              f"均值 {np.mean(dnet):+.2f}bp | 最大 {max(dnet, key=abs):+.2f}bp")
        same = sum(1 for d in dnet if abs(d) < 0.01)
        print(f"     完全一致 {same}/{len(pairs)} 笔")
    else:
        print("  ③ 无可配对的成交")
    print(f"  ④ 均净收益:实盘 {np.mean([x['net_bp'] for x in loop_tr]) if loop_tr else 0:+.2f}bp "
          f"| 回测 {td['net_bp'].mean() if len(td) else 0:+.2f}bp")

    # ---- ⑤ 漏单/多单 ----
    print(f"  ⑤ 回测有实盘没有(漏单) {len(extra)} | 实盘有回测没有(多单) {len(miss)}")
    for r in miss[:3]:
        print(f"      多: {pd.Timestamp(ts_of_loop(r), unit='s', tz='UTC')} 实盘净 {r['net_bp']:+.1f}bp")
    for t in extra[:3]:
        print(f"      漏: {t['t']} 回测净 {t['net_bp']:+.1f}bp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
