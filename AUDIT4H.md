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


---

## 九、第二批修复(执行层可靠性)

| # | 问题 | 修复 | 验证 |
|---|---|---|---|
| E1 | **无幂等键**,网络重试可能重复下单 | 确定性 cloid(blake2b)+ 客户端前置去重 | 连下两次 → 同一 oid,交易所只有 1 单 |
| E2 | 实测发现 **HL 不拒绝重复 cloid**(两笔都挂上) | 改为"下单前查挂单里有无同 cloid" | 同上 |
| E3 | 无重试/退避,429/网络抖动直接失败 | `_retry` 指数退避 0.4/0.8/1.6s,业务拒绝不重试 | — |
| E4 | **无持仓对账** → 手工测试/崩溃留下的仓位无人管 | 每轮对账:交易所有仓而本地无 → 接管并交给出场逻辑;本地有仓而交易所已平 → 记账 `external_close` | 代码路径已接,待真实触发 |

## 十、实测数据(这次审计产出的真实成本/行为)

```
$20 市价往返 ETH testnet:  -8.7bp   (回测假设 11bp → 假设偏保守)
下单被拒原因分布:          价格 tick(SOL/ETH 全中)、最小名义 $10
BTC 挂单最小名义:          $12 即可(0.0002 × 73k)
```


---

## 十一、闸门升级:G15/G16 + 重新审计(reaudit)

### 新增两条闸门(来自文献:Harvey-Liu-Zhu 2016 / Bailey & López de Prado 2014)

| 闸门 | 内容 | 实现要点 |
|---|---|---|
| **G15** | t 阈值按**试验次数** Bonferroni 调整 | `t_gate = Φ⁻¹(1 − 0.05/(2N))`;N=1→2.00,N=18→2.99,N=45→3.26 |
| **G16** | **Deflated Sharpe Ratio > 0.95** | Bailey-LdP 公式;**注意必须把年化 Sharpe 换算成日频**(我第一版量纲错了,DSR 全 1.000,已修) |

### 关键实现:reaudit(闸门升级后必须重判在跑的策略)

```
pipeline.reaudit(sid) → 用当前闸门重算;若在 paper/awaiting_approval/live 且不通过 → 自动 halted
```

### 重判结果(这才是最诚实的部分)

| 状态 | 策略 | 说明 |
|---|---|---|
| **paper(4)** | deep_limit_btc / deep_limit_sol / maker_sol_keltner / maker_sol_rsi1430 | DSR 通过 |
| **halted(6)** | maker_btc_rsi1430 / maker_btc_keltner / maker_sol_vwap-2sd / maker_sol_boll40x25 / atr_breakout / boll_breakout_eth | **全部只差 G16**(DSR 0.48~0.94) |
| rejected(6) | ETH 深挂、趋势、资金费×2、SOL 布林突破 | 更早失败 |
| retired(2) | 与 deep_limit 重复的两条 | — |

**两个必须说清的点**:

1. **同样的规则,BTC 深挂被判"通过"而 maker_btc_boll20 被"退役"** —— 因为前者来自预注册的 9 组参数搜索(n_trials=9),后者来自我 18 组扫单(n_trials=18),阈值不同。这是多重检验的正确逻辑,但说明"发现路径"会影响判决。
2. **6 条 halted 里,4 条是 Sharpe 1.19~1.57 的实打实候选**,只因 18 次试验下 DSR 没到 0.95。这不是"策略差",而是"证据还不够独立"。**它们应该继续留在样本外观察,而不是上实盘。**


---

## 十二、联网调研结论(4 份,均已核实)

### ① Python 内部服务框架 → **不迁移,补 hardening**

| 项 | 实测/结论 |
|---|---|
| ThreadingHTTPServer 对 HTTP/1.1 仍回 **HTTP/1.0**(无 keep-alive) | 已设 `protocol_version = "HTTP/1.1"` |
| `StreamRequestHandler.timeout = None` → 半截请求头挂住线程 | 已设 `timeout = 15` |
| **handler 抛异常 → 客户端收到 0 字节**(只 catch TimeoutError) | 已加 `_guard()` → 500 + 日志 |
| `request_queue_size = 5` | 已改 128 + daemon 线程 |
| 官方立场:"not recommended for production" | 单用户内部工具可接受;破千行/需 SSE/多用户才迁 **Starlette** |
| 认证排序 | Caddy basic_auth(已用)> Tailscale > mTLS > OIDC(单用户纯负担) |

2026-11 版本:FastAPI 0.162.4 / Starlette 1.10.0 / Litestar 2.25.0 / uvicorn 0.54.0 / Caddy 2.11.9。

### ② SQLite → **不仅够用,而且发现我们 3 个真 bug**

| 发现 | 状态 |
|---|---|
| `ledger.db` 是 `journal_mode=delete`(WAL 从未设过)→ 写阻塞读 | ✅ 已改 WAL + synchronous=NORMAL |
| **双批准可复现**(alice/bob 同时批准 → 都成功 + 2 条事件) | ✅ 已用 `transition()` 条件更新修复 |
| `update()` 与 `_ev()` 两个事务 → 状态变了没留痕 | ✅ approve 已合并;其余待迁 |
| `BEGIN DEFERRED` 读后升级写锁会立刻 locked,**busy_timeout 无效** | 已记录:写入路径用条件 UPDATE(不先读) |

容量实测:WAL+FULL 1,967 tx/s、WAL+NORMAL 42,763 tx/s;现网 84 事件/6,746 行 → **离迁移门槛差 3 个数量级**。
不可篡改三层(触发器 + hash chain + 外部锚点):我们已有触发器 ✅,hash chain 待做。

### ③ Hyperliquid 机器人 → **两个致命坑,已修**

| 坑 | 后果 | 修复 |
|---|---|---|
| **SDK 用 `base_url == MAINNET_API_URL` 字符串相等判网络** | 多一个斜杠 → 按测试网签名 → "User or API Wallet does not exist",极难排查 | ✅ 加模块级断言 |
| **孤儿单** | 进程 kill -9 后挂单留在交易所 | ✅ `scheduleCancel(now+120s)` 死手开关,每轮续期 |
| 价格需满足 **5 位有效数字**(ETH 2456.78 非法) | 下单被拒 | ✅ `px_dec` 同时受 szDecimals 与有效数字约束 |
| SDK `API.post()` 无超时、`get_timestamp_ms()` 无原子自增 | 挂死 / nonce 撞 | ✅ 全局 socket 超时 + 跨进程文件锁 |

主网迁移 checklist(调研给出 9 条):新 agent key、Arbitrum USDC、**1 USDC 激活费**、运行时读 szDecimals、kill -9 演练、−100bp 成交率不可外推。

### ④ 回测框架 → **不迁移,借 5 个模块**

| 框架 | 最新版本 | 判断 |
|---|---|---|
| vectorbt (OSS) | 1.2.0 / 2026-10-19 | 活跃,bar/权重级,无逐笔 |
| nautilus_trader | 1.231.0 / 2026-08-02;2.0.0rc9 | 最活跃,有 L2/L3;2.0 仍在 RC |
| backtrader | 1.9.78.123 / 2023-04-19 | **事实停维,勿选** |
| zipline-reloaded | 3.2.1 / 2026-10-25 | 复活,股票向无资金费 |
| hftbacktest | 2.4.4 / 2025-12-10 | **唯一支持队列位置+延迟成交** ← 值得借鉴 |

**结论:不迁移。** 瓶颈不在引擎而在统计与容量层;自建栈已有真实资金费、预注册 OOS、逐笔台账、100% 信号对账 —— 迁移要 2-3 人月且丢掉已验证基线。

**建议补的 5 个模块**:`walkforward`(purge/embargo)、`capacity`(impact = k·σ·√(Q/ADV))、`portfolio`(风险预算+换手惩罚)、`dq_gate`(完整性门禁)、`pbo/dsr`(已有 DSR)。


---

## 十三、中高风险项全部修完(收尾)

### ① 哈希链(高)—— 防"先删触发器再改历史"

触发器只能挡 DB 内的 UPDATE/DELETE;有写权限的人可以 `DROP TRIGGER` 再改,**触发器就失效了**。
现在每条事件都串进哈希链:`hash = sha256(prev_hash|id|ts|sid|kind|detail)`。

```
回填 124 条 → verify_chain() = ✅ 完整
写入新事件 → 链仍然完整 ✅
篡改测试(先 DROP TRIGGER 再 UPDATE id=50)→ ✅ 在 id=50 发现断链
```

注意实现细节:**必须插入前算好 id 再算 hash**,否则"插入后 UPDATE hash"会被 append-only 触发器挡住。

### ② 全部状态转移改原子(中)

`promote_paper / paper_pass / halt / retire / reset_paper` 从"update + event 两个事务"改为
`registry.transition()`(条件更新 `WHERE state=?` + 同事务写事件)。
现在 `registry.update(sid, state=...)` 的调用数为 **0**。

### ③ 容量:两个口径,别用错(中)

| 口径 | 适用 | 实测 |
|---|---|---|
| **平方根冲击** `k·σ·√(Q/ADV)` | **taker** 进出 | BTC $4.9B —— 说明对 maker 完全无约束力 |
| **maker 专用** P10(信号后 5 分钟成交额)×10% | 挂单在 −100bp | **BTC $1.74M / SOL $85K / ETH $613K** |

**结论:容量不是我们的瓶颈**(单笔 $20-500 远低于 $85K 下限),**成交率才是**。
新增 **G17**:单笔容量 ≥ $5,000。全部 18 条策略通过。

### ④ Walk-forward(purge 30d / embargo 30d)(中)

对横截面家族(8 币 × 参数网格 16 组)做真实"先选参数、再评估":

| 测试年 | 训练选中的参数 | 训练 Sharpe | **测试 Sharpe** |
|---|---|---|---|
| 2022 | lb=60 k=4 | 1.49 | **-1.18** |
| 2023 | lb=60 k=4 | 1.03 | +1.10 |
| 2024 | lb=60 k=4 | 0.83 | +0.21 |
| 2025 | lb=7 k=2 | 0.87 | +0.75 |

**训练均值 1.05 → 测试均值 0.22(衰减 79%)。**
这是"全样本调参"会骗人的实证:样本内 1.05 的夏普,换到未来只剩 0.22。

### ⑤ 逐笔成本回归(中,部分覆盖)

对账器已逐笔比对 `mean_diff`,ledger 每笔记录 `fee_bp / funding_bp / impact_bp`;
**尚未**做"实际成交价 vs 挂单价"的逐笔回归(需要 testnet 积累样本后才有意义)。
