"""审计闸门:把 crypto-quant-trade/research/09 的检查项变成可执行判定。

输入 = 回测指标 + 策略声明;输出 = 逐项 pass/fail + 总判定。
任何一项 fail → 不允许进入纸交易;纸交易达标后仍需**人工批准**才能上实盘。
"""
from __future__ import annotations

from dataclasses import dataclass

# 阈值(集中在这里,改阈值 = 改政策,须记录在案)
T_MIN = 2.0              # t 值下限(单策略;多策略同时检验需 Bonferroni 加严)
CI_LOW_MIN = 0.0         # bootstrap CI 下界须 > 0
COST_STRESS = 2.0        # 成本压测倍数
MAE_BUFFER = 0.9         # MAE 须 < 强平距离 × 0.9(留 10% 缓冲,对齐你 11 号报告 3x 的 398bp 缓冲口径)
MIN_TRADES = 100
MIN_DAYS = 730


@dataclass
class Check:
    id: str
    label: str
    passed: bool
    detail: str = ""

    def to_dict(self):
        return {"id": self.id, "label": self.label, "passed": self.passed, "detail": self.detail}


def liquidation_distance_bp(leverage: float) -> float:
    """逐仓 x 倍:价格逆行 ~1/x 即强平(忽略维持保证金,保守取整)。"""
    return 10000.0 / max(leverage, 1e-9)


def evaluate(metrics: dict, spec: dict) -> dict:
    """返回 {passed, checks[], score, blockers[]}。"""
    m = metrics or {}
    s = spec or {}
    checks: list[Check] = []

    def add(cid, label, ok, detail=""):
        checks.append(Check(cid, label, bool(ok), detail))

    # G1 声明前置(K6)
    lev = s.get("leverage")
    hold = s.get("hold_horizon")
    add("G1", "声明完整(杠杆/持仓/成本)",
        bool(lev) and bool(hold) and s.get("cost_bps") is not None,
        f"lev={lev} hold={hold} cost={s.get('cost_bps')}bp")

    # G2 样本量
    n = m.get("n_trades") or 0
    days = m.get("n_days") or 0
    add("G2", f"样本量(≥{MIN_TRADES} 笔 或 ≥{MIN_DAYS} 天)",
        n >= MIN_TRADES or days >= MIN_DAYS, f"{n} 笔 / {days} 天")

    # G3 显著性
    t = m.get("t_stat")
    add("G3", f"t 值 ≥ {T_MIN}", t is not None and t >= T_MIN, f"t={t}")

    # G4 bootstrap CI
    ci = m.get("ci_low")
    add("G4", "bootstrap CI 下界 > 0", ci is not None and ci > CI_LOW_MIN, f"CI低={ci}")

    # G5/G6 样本外与验证段
    oos = m.get("oos_mean")
    val = m.get("val_mean")
    add("G5", "预注册样本外 > 0", oos is not None and oos > 0, f"{oos}")
    add("G6", "验证段 > 0", val is not None and val > 0, f"{val}")

    # G7 极值依赖
    drop5 = m.get("drop_best5")
    add("G7", "剔除最好 5 天后仍 > 0", drop5 is not None and drop5 > 0,
        "无数据(未计算)" if drop5 is None else f"{drop5:.2f}bp/笔")

    # G8 成本压测
    stress = m.get("cost_stress_mean")
    add("G8", f"{COST_STRESS:g}× 成本下仍 > 0",
        stress is not None and stress > 0,
        "无数据(未计算)" if stress is None else f"{stress:.2f}bp/笔")

    # G9 清算安全
    if lev:
        dist = liquidation_distance_bp(lev)
        mae = m.get("max_mae_bp")
        ok = mae is not None and mae < dist * MAE_BUFFER
        add("G9", f"MAE < 强平距离×{MAE_BUFFER:g}({dist*MAE_BUFFER:.0f}bp)",
            ok, f"MAE={mae}bp / 强平={dist:.0f}bp")
    else:
        add("G9", "MAE < 强平距离", False, "无杠杆声明")

    # G10 命中率 或 盈亏比(趋势策略低命中高盈亏比是合法的)
    hit = m.get("hit_rate")
    aw, al = m.get("avg_win_bp"), m.get("avg_loss_bp")
    if hit is not None:
        rr = (aw / abs(al)) if (aw and al) else None
        ok = hit >= 0.5 or (rr is not None and rr >= 2.0)
        add("G10", "命中率 ≥ 50% 或 盈亏比 ≥ 2",
            ok, f"命中 {hit*100:.1f}%" + (f", 盈亏比 {rr:.2f}" if rr else ""))

    passed = all(c.passed for c in checks)
    score = sum(c.passed for c in checks) / len(checks) if checks else 0.0
    blockers = [c.label for c in checks if not c.passed]
    return {"passed": passed, "score": round(score, 3),
            "checks": [c.to_dict() for c in checks], "blockers": blockers}

# ---------------- 实盘门禁(L 层):只在"批准上实盘"时检查 ----------------
RECENT_MIN_N = 20          # 近 12 个月至少要 20 笔才算有证据
RECENT_NOTE = "近 12 个月均值 95% CI 下界必须 > 0 —— 证明 edge 在今天还活着,而不只是三年前"


def live_checks(metrics: dict) -> list[dict]:
    """实盘前置门禁。回测不过可以继续纸交易攒证据;这一层不过不许动真钱。"""
    out: list[dict] = []
    n = metrics.get("recent_n")
    mean = metrics.get("recent_mean_bp")
    ci = metrics.get("recent_ci_low")

    def add(cid, label, passed, detail):
        out.append({"id": cid, "label": label, "passed": bool(passed), "detail": str(detail)})

    add("G11", f"近 12 个月 CI 下界 > 0(样本 ≥{RECENT_MIN_N} 笔)",
        n is not None and n >= RECENT_MIN_N and ci is not None and ci > 0,
        ("无近期窗口数据(未跑 windows.py)" if n is None else f"n={n} 均值={mean}bp CI低={ci}bp"))
    return out
