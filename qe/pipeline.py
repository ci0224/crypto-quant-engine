"""流水线状态机:research → backtest → review → paper → 人工批准 → live。

每个动作都有守卫条件,非法转移直接抛异常(而不是静默改状态)。
"""
from __future__ import annotations

import time

from . import gate, registry


class IllegalTransition(RuntimeError):
    pass


# 纸交易转正的达标线(可由 UI 调整;改这里 = 改政策)
PAPER_MIN_DAYS = 14
PAPER_MIN_TRADES = 20
PAPER_MAX_DD_MULT = 1.5      # 纸交易回撤 ≤ 回测回撤 × 该倍数


def register(name, family, symbols, spec, metrics=None, equity=None, note=""):
    """登记策略。带 metrics 视为"已完成回测"→ 直接进 backtested。"""
    sid = registry.add(name, family, symbols, spec, note=note,
                       state="backtested" if metrics else "draft")
    if metrics:
        registry.update(sid, metrics=metrics, equity=equity or [])
        registry.event(sid, "backtest", f"n={metrics.get('n_trades')} t={metrics.get('t_stat')}")
    return registry.get(sid)


def backtest(sid, metrics, equity=None, note=""):
    s = registry.get(sid)
    if not s:
        raise KeyError(sid)
    if s["state"] in ("live", "retired", "halted"):
        raise IllegalTransition(f"{s['state']} 状态不能重跑回测")
    registry.update(sid, metrics=metrics, equity=equity or [], state="backtested")
    registry.event(sid, "backtest", note or f"n={metrics.get('n_trades')} t={metrics.get('t_stat')}")
    return registry.get(sid)


def review(sid, note=""):
    """跑审计闸门:全过 → reviewed;任一不过 → rejected。"""
    s = registry.get(sid)
    if not s:
        raise KeyError(sid)
    if s["state"] not in ("backtested", "rejected", "reviewed"):
        raise IllegalTransition(f"{s['state']} 不能做 review")
    res = gate.evaluate(s.get("metrics") or {}, s.get("spec") or {})
    registry.event(sid, "review", f"score={res['score']} blockers={res['blockers'][:4]}")
    if not res["passed"]:
        registry.update(sid, audit=res, state="rejected")
        return registry.get(sid), res
    # 审计通过 → 自动进入沙盘(paper 状态);venue 默认 testnet(HL 真实撮合)
    sp = dict(s.get("spec") or {})
    sp.setdefault("venue", "testnet")
    registry.update(sid, audit=res, state="paper", spec=sp)
    registry.event(sid, "auto_promote", "审计通过 → 自动进入沙盘(venue=%s)" % sp["venue"])
    return registry.get(sid), res


def promote_paper(sid, note=""):
    s = registry.get(sid)
    if not s:
        raise KeyError(sid)
    if s["state"] != "reviewed":
        raise IllegalTransition(f"只有 reviewed 才能进纸交易(当前 {s['state']})")
    registry.update(sid, state="paper")
    registry.event(sid, "promote", note or "进入纸交易")
    return registry.get(sid)


def paper_tick(sid, stats: dict, source: str = "api"):
    """纸交易心跳:写入统计;达标则转 awaiting_approval(仍需人工批准)。

    source 记录数据来源:
      · "loop" —— 真实纸交易循环写入(唯一可用于审批的来源)
      · 其它   —— 手工/调试写入,永久标记为不可审批(防止"编数据上进实盘")
    """
    s = registry.get(sid)
    if not s or s["state"] != "paper":
        return None
    m = s.get("metrics") or {}
    stats = dict(stats or {})
    stats["source"] = source
    registry.update(sid, paper_json=stats)
    ok = (stats.get("days", 0) >= PAPER_MIN_DAYS
          and stats.get("trades", 0) >= PAPER_MIN_TRADES
          and (stats.get("mean_bp") or 0) > 0
          and abs(min(stats.get("max_dd", 0), 0)) <= abs(m.get("max_dd") or 1) * PAPER_MAX_DD_MULT)
    if ok and source != "loop":
        registry.event(sid, "paper_blocked",
                       f"手工写入达标但不推进状态(source={source});仅 loop 写入可用于审批")
        return registry.get(sid)
    if ok:
        registry.update(sid, state="awaiting_approval")
        registry.event(sid, "paper_pass",
                       f"days={stats.get('days')} trades={stats.get('trades')} "
                       f"mean={stats.get('mean_bp')}bp")
        return registry.get(sid)
    return s


def approve(sid, by: str, note=""):
    """人工批准 → 实盘。必须处于 awaiting_approval。"""
    s = registry.get(sid)
    if not s:
        raise KeyError(sid)
    paper = s.get("paper") or {}
    if paper.get("source") != "loop":
        raise IllegalTransition(
            f"拒绝审批:纸交易数据来源 = {paper.get('source')!r},不是真实纸交易循环写入的"
            "(手工/演示数据不可用于推进实盘)")
    if s["state"] != "awaiting_approval":
        raise IllegalTransition(f"只有 awaiting_approval 可批准(当前 {s['state']})")
    if not by:
        raise ValueError("approve_by 不能为空:必须记录谁批准的")
    # 实盘门禁(L 层):近期有效性必须成立,否则只许继续纸交易
    lc = gate.live_checks(s.get("metrics") or {})
    bad = [c for c in lc if not c["passed"]]
    if bad:
        raise IllegalTransition(
            "拒绝上实盘 —— 近期有效性未通过:" + "; ".join(f"{c['id']} {c['detail']}" for c in bad)
            + "。请继续纸交易积累证据(≥20 笔)后再评。")

    m = s.get("metrics") or {}
    evidence = (f"n={m.get('n_trades')} 均值={m.get('mean_bp')}bp CI低={m.get('ci_low')} "
                f"纸交易={paper.get('days')}天/{paper.get('trades')}笔")
    registry.transition(sid, "awaiting_approval", "live", "approve",
                        f"by={by} {note} | 批准时证据: {evidence}",
                        approve_by=by, approve_at=int(time.time()))
    return registry.get(sid)


def halt(sid, reason=""):
    s = registry.get(sid)
    if not s:
        raise KeyError(sid)
    registry.update(sid, state="halted", note=reason)
    registry.event(sid, "halt", reason)
    return registry.get(sid)


def retire(sid, reason=""):
    registry.update(sid, state="retired", note=reason)
    registry.event(sid, "retire", reason)
    return registry.get(sid)

def reset_paper(sid: str, note: str = ""):
    """退回纸交易并清空进度(用于:演示数据污染、或想重跑一轮纸交易)。"""
    s = registry.get(sid)
    if not s:
        raise IllegalTransition(f"策略不存在: {sid}")
    if s["state"] not in ("paper", "awaiting_approval", "halted"):
        raise IllegalTransition(f"{s['state']} → paper(只有 paper/awaiting_approval/halted 可以退回纸交易)")
    registry.update(sid, state="paper", paper_json=None)
    registry.event(sid, "reset_paper", note or "退回纸交易,清空历史进度")
    return registry.get(sid)
