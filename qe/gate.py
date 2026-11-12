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
MAE_BUFFER = 0.9
CAP_MIN_USD = 5_000      # 单笔名义容量下限(低于此规模不值得上实盘)         # MAE 须 < 强平距离 × 0.9(留 10% 缓冲,对齐你 11 号报告 3x 的 398bp 缓冲口径)
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
    hold = s.get("hold_horizon") or s.get("hold_horizon_days") or s.get("hold_horizon_hours")
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
        rr = (aw / abs(al)) if (aw and al) else m.get("payoff")
        ok = hit >= 0.5 or (rr is not None and rr >= 2.0)
        add("G10", "命中率 ≥ 50% 或 盈亏比 ≥ 2",
            ok, f"命中 {hit*100:.1f}%" + (f", 盈亏比 {rr:.2f}" if rr else ""))

    # ---- 多重检验:试验次数越多,阈值越高;并按 DSR 通缩 ----
    n_trials = int(m.get("n_trials") or 1)
    t_gate = 2.0 if n_trials <= 1 else max(2.0, _norm_ppf(1 - 0.05 / (2 * n_trials)))
    t_val = m.get("t_stat")
    add("G15", f"t >= {t_gate:.2f} ({n_trials} trials, Bonferroni)",
        t_val is not None and float(t_val) >= t_gate, f"t={t_val} thr={t_gate:.2f}")
    # 注意:DSR 公式要求"逐期"(日频)Sharpe,而 metrics.sharpe 是年化的 → 必须换算
    sr_pp = float(m.get("sharpe") or 0.0) / (365 ** 0.5)
    dsr = deflated_sharpe(sr_pp, int(m.get("n_obs") or 1500),
                          n_trials, float(m.get("skew") or 0.0), float(m.get("kurt") or 3.0))
    add("G16", "Deflated Sharpe > 0.95", dsr > 0.95, f"DSR={dsr:.3f} (trials={n_trials})")

    # G17 容量:单笔名义上限(metrics 里的 cap_usd,由 capacity.py 算出)
    cap = m.get("cap_usd")
    add("G17", f"单笔容量 >= ${CAP_MIN_USD:,}",
        cap is not None and float(cap) >= CAP_MIN_USD,
        f"cap=${cap:,.0f}" if cap is not None else "缺容量数据")

    passed = all(c.passed for c in checks)
    score = sum(c.passed for c in checks) / len(checks) if checks else 0.0
    blockers = [c.label for c in checks if not c.passed]
    # ---- 引擎级闸门:必须看复利后的净值,不能只看逐笔算术均值 ----
    cagr = m.get("cagr")
    dd = m.get("max_dd")
    shp = m.get("sharpe")
    add("G12", "复利后 CAGR > 0", cagr is not None and cagr > 0,
        "缺数据" if cagr is None else f"CAGR {cagr:.1f}%")
    add("G13", "最大回撤 > -60%", dd is not None and dd > -60,
        "缺数据" if dd is None else f"DD {dd:.1f}%")
    add("G14", "引擎 Sharpe ≥ 1.0", shp is not None and shp >= 1.0,
        "缺数据" if shp is None else f"Sharpe {shp:.2f}")
    return {"passed": passed, "score": round(score, 3),
            "checks": [c.to_dict() for c in checks], "blockers": blockers}

# ---------------- 实盘门禁(L 层):只在"批准上实盘"时检查 ----------------
RECENT_MIN_N = 20          # 近 12 个月至少要 20 笔才算有证据
RECENT_NOTE = "近 12 个月均值 95% CI 下界必须 > 0 —— 证明 edge 在今天还活着,而不只是三年前"



# ---------------- 多重检验:G15 试验次数调整 / G16 Deflated Sharpe ----------------
def _norm_ppf(p):
    import math
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    if p <= 0:
        return -1e9
    if p < 0.02425:
        q = (-2 * math.log(p)) ** 0.5
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > 1 - 0.02425:
        q = (-2 * math.log(1 - p)) ** 0.5
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def _norm_cdf(x):
    import math
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def deflated_sharpe(sharpe, n_obs, n_trials, skew=0.0, kurt=3.0):
    """Bailey & López de Prado (2014):按试验次数通缩后的 Sharpe 显著概率。"""
    import math
    if not sharpe or n_obs < 30 or n_trials < 1:
        return 0.0
    g = 0.5772156649
    e = math.e
    if n_trials == 1:
        sr0 = 0.0
    else:
        sr0 = (math.sqrt(1.0 / max(n_obs - 1, 1)) *
               ((1 - g) * _norm_ppf(1 - 1.0 / n_trials) +
                g * _norm_ppf(1 - 1.0 / (n_trials * e))))
    denom = math.sqrt(max(1e-12, 1 - skew * sharpe + (kurt - 1) / 4.0 * sharpe ** 2))
    return _norm_cdf((sharpe - sr0) * math.sqrt(max(n_obs - 1, 1)) / denom)

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
