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
    registry.update(sid, audit=res, state="reviewed" if res["passed"] else "rejected")
    registry.event(sid, "review", f"score={res['score']} blockers={res['blockers'][:4]}")
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


def paper_tick(sid, stats: dict):
    """纸交易心跳:写入统计;达标则转 awaiting_approval(仍需人工批准)。"""
    s = registry.get(sid)
    if not s or s["state"] != "paper":
        return None
    m = s.get("metrics") or {}
    registry.update(sid, paper_json=stats)
    ok = (stats.get("days", 0) >= PAPER_MIN_DAYS
          and stats.get("trades", 0) >= PAPER_MIN_TRADES
          and (stats.get("mean_bp") or 0) > 0
          and abs(min(stats.get("max_dd", 0), 0)) <= abs(m.get("max_dd") or 1) * PAPER_MAX_DD_MULT)
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
    if s["state"] != "awaiting_approval":
        raise IllegalTransition(f"只有 awaiting_approval 可批准(当前 {s['state']})")
    if not by:
        raise ValueError("approve_by 不能为空:必须记录谁批准的")
    registry.update(sid, state="live", approve_by=by, approve_at=int(time.time()))
    registry.event(sid, "approve", f"by={by} {note}")
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
