# SQLite 多进程写入 + 不可篡改审计:结论与落地方案

**TL;DR**:规模完全够用(实测 WAL+FULL ≈1967 提交/秒;你现在 6746 行台账、<1 写/秒、库 96KB/932KB)。但现有代码有 3 个真问题:`ledger.db` 不是 WAL;`approve` 的 check-then-update 可双重批准(已复现);状态与事件分两个事务写,崩溃即丢痕。**先修这三点,不要迁库。**

## 1. 这种规模用 SQLite 合理,但有硬上限

- WAL 下**同时只有一个 writer**,读写互不阻塞;但所有进程必须同机、不能放网络文件系统([wal.html](https://sqlite.org/wal.html))。
- 你有两个库文件:跨库事务**不具备原子性**(同上),所以"改状态 + 写事件"必须落同一个库、同一个事务。
- 本机实测(4 进程 × 300 事务):DELETE 模式 1.64s,WAL 0.05s;单进程 WAL+FULL 1967 tx/s、WAL+NORMAL 42763 tx/s、DELETE+FULL 672 tx/s。
- 必设 PRAGMA:`journal_mode` 是**持久化**的([wal.html](https://sqlite.org/wal.html) §3.3),这正好解释了为什么 `pipeline.db` 是 wal 而 `ledger.db` 仍是 delete —— 后者从没设过。

```python
c = sqlite3.connect(DB, timeout=15, isolation_level=None)  # 关掉隐式 BEGIN DEFERRED
c.execute("PRAGMA journal_mode=WAL")     # 持久,但每次设可防被回滚
c.execute("PRAGMA busy_timeout=15000")   # timeout= 等价,显式写更清楚
c.execute("PRAGMA synchronous=NORMAL")   # WAL 下断电不坏库,最多丢最后几个事务
c.execute("PRAGMA foreign_keys=ON")      # 默认关闭,必须每连接开
c.execute("PRAGMA wal_autocheckpoint=1000")
```

- **最关键的坑**:`BEGIN DEFERRED`(python 默认)下"先读后写"升级写锁会**直接** `database is locked`,busy_timeout 完全救不了(实测 3000ms 仍立刻失败)。所有写事务一律 `BEGIN IMMEDIATE`([lang_transaction](https://sqlite.org/lang_transaction.html))。

## 2. 不可篡改:触发器 + hash chain + 外部锚点

SQLite **没有** GRANT/REVOKE,权限只有文件系统权限([omitted.html](https://sqlite.org/omitted.html)),所以"不可篡改"只能三层叠加:

1. `BEFORE UPDATE/DELETE` 触发器 `RAISE(ABORT, …)`;
2. `prev_hash` 链 + `hash UNIQUE`:插入必须接在当前 head 上,改任何一行都要重算全链;
3. **锚点**:每天把 `(seq, hash)` 写进 git commit 或带签名的只追加文件。库文件的所有者仍能 `DROP TRIGGER` 重写全链,锚点让重写可被发现(即 RFC 6962 append-only 日志的思路)。

PoC 已跑通:非法转移、演示数据审批、UPDATE/DELETE 全部被拦。**结论:触发器 + 链足够挡住"误改/应用层篡改",但"有文件写权限的人"只能靠锚点,要真权限模型才需要 Postgres。**

## 3. 状态机实现对比

| 方案 | 留痕 | 回滚 | 评价 |
|---|---|---|---|
| 显式转移表(py dict) | 要自己写 | 好 | 逻辑清楚,但规则**不在库里**,并发下不设防 |
| `transitions` 库 | 无 | 一般 | 只是内存 FSM,状态仍存 strategies,双写问题不变 |
| DB CHECK / 触发器 | 强 | 差(无历史) | 能挡非法值,答不了"谁推的、基于什么" |
| **事件溯源** | 天然 | 天然(追加补偿事件) | **推荐** |

结论:`events` 作唯一事实源,`strategies` 不再存 `state`,用视图派生 —— 直接消灭"改状态/写事件"双写。回滚 = 追加一条 `live→halted→paper` 事件,而不是改历史。

```sql
CREATE TABLE transitions(from_state TEXT NOT NULL, to_state TEXT NOT NULL,
  PRIMARY KEY(from_state,to_state));
CREATE TABLE events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  sid TEXT NOT NULL REFERENCES strategies(id),
  ts INTEGER NOT NULL,
  actor TEXT NOT NULL,                -- human:alice / svc:qe-paper / svc:qe-batch
  kind TEXT NOT NULL,
  from_state TEXT, to_state TEXT NOT NULL,
  payload TEXT NOT NULL,              -- 决策依据的数据快照
  payload_sha TEXT NOT NULL CHECK(length(payload_sha)=64),
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL UNIQUE);
CREATE VIEW v_state AS SELECT e.sid, e.to_state AS state FROM events e
 WHERE e.seq=(SELECT MAX(seq) FROM events WHERE sid=e.sid);

CREATE TRIGGER ev_chain BEFORE INSERT ON events BEGIN
  SELECT RAISE(ABORT,'chain broken') WHERE NEW.prev_hash <>
    COALESCE((SELECT hash FROM events ORDER BY seq DESC LIMIT 1),'genesis');
  SELECT RAISE(ABORT,'illegal transition') WHERE NEW.from_state IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM transitions WHERE from_state=NEW.from_state AND to_state=NEW.to_state);
  -- 演示/手工数据不得用于审批;且 live 必须人批
  SELECT RAISE(ABORT,'approval needs loop data') WHERE NEW.to_state='live' AND (
    json_extract(NEW.payload,'$.paper.source') IS NOT 'loop'
    OR json_extract(NEW.payload,'$.paper.demo') IS NOT 0);
  SELECT RAISE(ABORT,'live requires human actor') WHERE NEW.to_state='live'
    AND NEW.actor NOT LIKE 'human:%';
END;
CREATE TRIGGER ev_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER ev_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
```

## 4. 什么时候该迁

量化门槛,**任一命中**才迁 Postgres:

- 持续写 > 200 tx/s(FULL 实测 1967 的 10% 安全线),或 > 20 个常驻写进程;
- 需要多机/HA,或需要"应用账号不能改审计表"的权限模型(SQLite 结构上做不到);
- `events` > 5000 万行 / 库 > 10GB 且要做时间范围 OLAP → 此时 **DuckDB 只当只读分析副本**(单写多读),不当在线库。

你现在离第一条差约 3 个数量级(84 条事件 / 18 个策略)。**别迁**。

## 5. 可直接落地的 8 条

1. `qe/ledger.py:conn()` 加 `PRAGMA journal_mode=WAL`(当前是 delete,写入时阻塞 web 读)。
2. `approve` 改原子守卫 —— 已复现:naive 写法下 alice/bob **同时批准成功、留下 2 条 approve 事件**;加守卫后只有 1 条:

```sql
BEGIN IMMEDIATE;
UPDATE strategies SET state='live', approve_by=?, approve_at=?
 WHERE id=? AND state='awaiting_approval';   -- rowcount 必须 = 1,否则 ROLLBACK
```

3. 状态与事件合并为一个 `conn()`(`isolation_level=None` + `BEGIN IMMEDIATE`),消灭 `registry.update()` 与 `_ev()` 之间的崩溃窗口。
4. `events` 增加 `actor/from_state/to_state/payload/hash` 列,存量回填 `actor='unknown'`(`ALTER TABLE ... ADD COLUMN` 在 SQLite 是 O(1))。
5. 加第 3 节的三个触发器(PoC:`research/sqlite_poc/poc_audit.py`)。
6. `ledger` 加约束:`CHECK(source IN ('loop','testnet','backtest','demo'))`、`CHECK(kind IN ('signal','trade'))`;审批前只认 `source='loop' AND demo=0`(现在只有 `server.py` 删接口这一层软防护)。
7. 每日锚点:把 `(seq, hash)` 追加到 git 或 `/var/log/qe/anchor.log`(只追加权限),作为链外证据。
8. 若要保留两个库文件,在 `ledger` 加 `event_seq` 逻辑外键指向 `events.seq`;否则接受台账无法与状态变更同事务提交。

**网络说明**:pypi.org 与 duckdb.org 文档被 JS 挑战/重定向挡下,故 `transitions` 改引 GitHub 仓库、DuckDB 只引其 concurrency 页而不逐字引用;sqlite.org、postgresql.org、rfc-editor.org 直连正常。

## 出处

- WAL 并发/持久性/跨库非原子:<https://sqlite.org/wal.html>
- busy_timeout / journal_mode / synchronous:<https://sqlite.org/pragma.html#pragma_busy_timeout>
- BEGIN IMMEDIATE:<https://sqlite.org/lang_transaction.html>
- FK 默认关闭:<https://sqlite.org/foreignkeys.html>
- 无 GRANT/REVOKE:<https://sqlite.org/omitted.html>
- 何时该用 client/server:<https://sqlite.org/whentouse.html>
- PG 隔离级别 / FOR UPDATE:<https://www.postgresql.org/docs/current/transaction-iso.html>
- append-only 审计日志:<https://www.rfc-editor.org/rfc/rfc6962.txt>
- transitions:<https://github.com/pytransitions/transitions>
