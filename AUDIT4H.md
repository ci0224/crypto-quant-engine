# AUDIT4H — 全框架逐服务自审(2026-11-12)

> 范围:qe-web / qe-paper / registry / pipeline / gate / strict engine + 数据层 /
> live+testnet 执行层 / ledger+reconcile / 部署层(Caddy·systemd·cron·secrets)
> 方法:逐个服务读源码 + 实机验证(改完必须跑出证据),不只看文档承诺
> 结论:**修复 18 个问题,其中 1 个致命(SOL/ETH 下单全部被拒)**

---

## 一、致命问题(会导致策略"看着在跑其实没交易")

### A1 🔴 价格精度写死 → ETH/SOL 全部下单被拒

```python
# 之前
px = float(round(px)) if coin == "BTC" else round(px, 2)
```

HL 的规则是 **价格小数位 = 6 − szDecimals − 1**:

| 币 | szDecimals | 允许小数位 | 之前的行为 |
|---|---|---|---|
| BTC | 5 | 0(整数) | ✅ 正确 |
| ETH | 4 | 1 | ❌ 2 位被拒 `Price must be divisible by tick size` |
| SOL | 2 | 3 | ❌ 2 位被拒 |

**后果**:6 条 SOL/ETH 的 testnet 策略从上线起**一单都没能挂出去**,而 state 文件里
照样记"已挂单",看板显示"纸交易中"。这正是"纸交易数据来源不可信"的根源之一。

**修复**:`px_dec(coin) = max(0, 6 - szDecimals - 1)`,统一用于 `place_alo / place_tp / place_market`。
**验证**:三个币各挂一笔 → `BTC 73587.0 ✅ / ETH 1993.5 ✅ / SOL 84.728 ✅`。

### A2 🔴 数量精度写死 5 位

SOL 的 szDecimals=2,`round(0.167, 5)=0.167` 会被拒。已改为按 `self._dec[coin]` 取整。

---

## 二、qe-web(对外暴露面最大)

| # | 问题 | 严重度 | 修复 | 验证 |
|---|---|---|---|---|
| W1 | **无 socket 超时** → 慢连接可耗尽线程(slowloris) | 高 | `timeout=15` + `HTTP/1.1` | 正常 200 |
| W2 | 令牌比较非恒定时间(时序侧信道) | 中 | `hmac.compare_digest` | 错令牌 403 |
| W3 | **`paper_tick` 后门仍可通过 API 调用** | 高 | 返回 410 并说明原因 | 实测 410 |
| W4 | POST body 无上限(内存 DoS) | 中 | 64KB 上限 | 实测 413 |
| W5 | HEAD 返回 body(协议违规) | 低 | 只回头 | `size=0` |
| W6 | **完全无访问/错误日志** | 中 | 4xx/5xx + 写操作落 stderr | journal 可见 |
| W7 | 限速表 `HITS` 无界增长 | 低 | 4096 上限 | — |
| W8 | `_reconcile()` 静默吞异常 | 中 | 落日志 | — |

---

## 三、qe-paper(调度与崩溃恢复)

| # | 问题 | 严重度 | 修复 |
|---|---|---|---|
| P1 | 状态文件**非原子写** → 崩溃留半截 JSON | 高 | `tmp` + `os.replace` |
| P2 | 状态损坏后**永久静默失败**(每轮都 catch 到同一异常) | 高 | 备份坏文件 + 重建 + 告警 |
| P3 | **无孤儿单对账** → 进程在"下单后、存状态前"崩溃,交易所留单 | 高 | 启动首轮清理交易所侧未知挂单 |
| P4 | HL SDK 的 requests **无超时** → 单次请求挂死整个循环 | 高 | 全局 `socket.setdefaulttimeout(15)` |
| P5 | 固定 `sleep(interval)` → 执行耗时累加导致漂移 | 低 | 漂移校正 |
| P6 | 无心跳 → `systemctl active` 不等于循环健康 | 中 | `data/paper/heartbeat` |
| P7 | 服务与手工脚本同时下单 → **nonce 冲突** | 高 | 跨进程文件锁(见 D2) |

---

## 四、registry / pipeline(审计可溯源)

| # | 问题 | 严重度 | 修复 | 验证 |
|---|---|---|---|---|
| R1 | `events` 可被 UPDATE/DELETE → "不可篡改"不成立 | 高 | append-only 触发器 | 实测:两种操作都被 ABORT |
| R2 | 事件按 `ts` 排序,同秒事件顺序不确定 | 中 | 按 `id` 排序 | — |
| R3 | `approve` 是 check-then-act → 并发双批/与 halt 交错 | 高 | `registry.transition()` 条件更新 `WHERE state=?` | 实测:状态不符时抛错 |
| R4 | 状态改了但事件没写(崩溃窗口)→ 审计断链 | 高 | 同一事务内更新+写事件 | — |
| R5 | 批准事件没记"基于什么数据" | 中 | 事件里写入 `n/均值/CI/纸交易天数笔数` 快照 | — |

---

## 五、部署层

| # | 问题 | 严重度 | 修复 |
|---|---|---|---|
| D1 | `MemoryMax=infinity` → 内存泄漏可拖垮整机 | 中 | qe-web 512M / qe-paper 1G |
| D2 | **`PrivateTmp=yes` 让跨进程锁失效**(各服务独立 /tmp) | 高 | 锁文件移到 `data/.hl_order.lock` |
| D3 | `ProtectSystem=no` → 服务可写整个文件系统 | 中 | `ProtectSystem=full` + `ReadWritePaths` |
| D4 | `RestartSec` 默认 100ms → 崩溃循环空转 | 低 | `RestartSec=5` |
| D5 | secrets 权限 | ✅ 已合规 | 全部 600 |

---

## 六、数据层(审计通过)

```
BTC  3,598,560 根  缺口 0.04%   重复 0  OHLC 违例 0  单调 ✅
ETH  3,592,800 根  缺口 0.04%   重复 0  OHLC 违例 0  单调 ✅
SOL  3,215,100 根  缺口 0.27%   重复 0  OHLC 违例 0  单调 ✅
```

---

## 七、未修但已记录(按优先级)

| 优先级 | 项 | 说明 |
|---|---|---|
| 高 | **启动时持仓对账** | 交易所持仓 vs state 不一致时目前只报错,未自动接管/平仓 |
| 高 | **订单 client-id(cloid)** | 无幂等键,网络重试可能重复下单 |
| 中 | 重试/退避 | HL API 429/5xx 时无退避,直接失败 |
| 中 | 容量门禁 G12 | 单笔名义 vs 5 分钟成交额,当前只有参数未入门禁 |
| 中 | 回测-实盘逐笔成本回归 | 真实成交价 vs 假设,目前只有汇总对账 |
| 低 | `boll_state` 的 Python 循环 | 719k 根耗时 ~10s,可向量化 |
| 低 | 每连接 `executescript(SCHEMA)` | DDL 每次连接都跑,可移到启动一次 |
| 低 | 8090/8091 绑 0.0.0.0 | 依赖云安全组;建议加 iptables 限制来源 |

---

## 八、联网调研(4 份,待并入)

1. Python 内部服务框架选型(2026)
2. SQLite 多进程与不可篡改审计日志
3. Hyperliquid 交易机器人工程实践
4. 量化回测框架现状与借鉴点

> 调研结果并入后,本节替换为结论表(是否迁移/是否换框架/必补项)。
