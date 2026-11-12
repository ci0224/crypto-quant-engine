"""策略注册表(SQLite):一条策略从研究到实盘的完整生命周期。

状态机:
    draft → backtested → reviewed → paper → awaiting_approval → live
                              ↓ rejected            ↓ halted
                            retired

设计原则:
  · 每次状态变化都写 events 表(可审计的时间线)
  · 指标/审计结果以 JSON 存(结构自由演进)
  · 实盘必须经过 awaiting_approval + 人工批准(approve_by 记录谁批的)
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "pipeline.db"

STATES = ("draft", "backtested", "reviewed", "rejected", "paper",
          "awaiting_approval", "live", "halted", "retired")

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  family TEXT NOT NULL,            -- trend / funding_carry / deep_limit / ...
  symbols TEXT NOT NULL,
  state TEXT NOT NULL,
  spec TEXT NOT NULL,              -- 声明:leverage / hold_horizon / cost_bps
  metrics TEXT,                    -- 回测指标 JSON
  audit TEXT,                      -- 审计清单 JSON
  equity TEXT,                     -- 权益曲线 [[ts, v], ...] 精简
  paper_json TEXT,                 -- 纸交易统计
  approve_by TEXT,
  approve_at INTEGER,
  note TEXT,
  created INTEGER NOT NULL,
  updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id TEXT NOT NULL,
  ts INTEGER NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT,
  prev_hash TEXT,                  -- 前一条事件的 hash(哈希链)
  hash TEXT                        -- sha256(prev_hash|id|ts|sid|kind|detail)
);


CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN
  SELECT RAISE(ABORT, 'events 是 append-only 审计日志,禁止修改');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN
  SELECT RAISE(ABORT, 'events 是 append-only 审计日志,禁止删除');
END;CREATE INDEX IF NOT EXISTS ix_ev ON events(strategy_id, ts DESC);
"""


@contextmanager
def conn():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=15)
    try:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        yield c
        c.commit()
    finally:
        c.close()


def _ev(c, sid, kind, detail=""):
    """写一条事件并串进哈希链。

    触发器能挡住 UPDATE/DELETE,但挡不住"先 DROP TRIGGER 再改"。哈希链保证
    任何历史改动都能被 verify_chain() 发现。
    """
    import hashlib
    ts = int(time.time())
    prev = c.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = (prev[0] if prev and prev[0] else "genesis")
    # 先算 id 再插入:append-only 触发器禁止 INSERT 后再 UPDATE hash
    eid = c.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM events").fetchone()[0]
    h = hashlib.sha256(f"{prev_hash}|{eid}|{ts}|{sid}|{kind}|{detail}".encode()).hexdigest()
    c.execute("INSERT INTO events (id, strategy_id, ts, kind, detail, prev_hash, hash) "
              "VALUES (?,?,?,?,?,?,?)", (eid, sid, ts, kind, detail, prev_hash, h))
    return eid


def verify_chain(limit: int = 0):
    """从头校验哈希链。返回 (ok, 坏掉的 id 或 None, 总条数)。"""
    import hashlib
    with conn() as c:
        rows = c.execute("SELECT id, ts, strategy_id, kind, detail, prev_hash, hash "
                         "FROM events ORDER BY id").fetchall()
    prev = "genesis"
    for i, r in enumerate(rows):
        h = hashlib.sha256(f"{prev}|{r['id']}|{r['ts']}|{r['strategy_id']}|{r['kind']}|{r['detail']}".encode()).hexdigest()
        if r["prev_hash"] != prev or r["hash"] != h:
            return False, r["id"], len(rows)
        prev = r["hash"]
        if limit and i + 1 >= limit:
            break
    return True, None, len(rows)

def add(name, family, symbols, spec, note="", state="draft"):
    sid = uuid.uuid4().hex[:12]
    now = int(time.time())
    with conn() as c:
        c.execute("INSERT INTO strategies(id,name,family,symbols,state,spec,note,created,updated) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  (sid, name, family, json.dumps(symbols), state,
                   json.dumps(spec), note, now, now))
        _ev(c, sid, "register", f"family={family} symbols={symbols}")
    return sid


def update(sid, **fields):
    allowed = {"state", "metrics", "audit", "equity", "paper_json", "approve_by",
               "approve_at", "note", "spec"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            raise KeyError(k)
        sets.append(f"{k}=?")
        vals.append(json.dumps(v) if k in {"metrics", "audit", "equity", "paper_json", "spec"} else v)
    sets.append("updated=?")
    vals.append(int(time.time()))
    vals.append(sid)
    with conn() as c:
        cur = c.execute(f"UPDATE strategies SET {', '.join(sets)} WHERE id=?", vals)
        if cur.rowcount == 0:
            raise KeyError(f"no strategy {sid}")


def event(sid, kind, detail=""):
    with conn() as c:
        _ev(c, sid, kind, detail)


def get(sid):
    with conn() as c:
        r = c.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
        return _row(r) if r else None


def all():
    with conn() as c:
        return [_row(r) for r in c.execute("SELECT * FROM strategies ORDER BY created DESC")]


def events(sid, limit=50):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM events WHERE strategy_id=? ORDER BY id DESC LIMIT ?", (sid, limit))]


def _row(r):
    d = dict(r)
    for k in ("spec", "metrics", "audit", "equity", "paper_json"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    d["symbols"] = json.loads(d["symbols"]) if d.get("symbols") else []
    # 前端统一读 s["paper"];库里列名是 paper_json
    d["paper"] = d.get("paper_json")
    return d


def transition(sid, from_state, to_state, kind, detail="", **fields):
    """原子状态转移:条件更新 + 事件写入在同一个事务里。

    解决两个问题:
      ① check-then-act 竞态(两个进程同时批准)
      ② 状态改了但事件没写(崩溃窗口 → 审计断链)
    """
    return _transition(sid, from_state, to_state, kind, detail, fields)


def _transition(sid, from_state, to_state, kind, detail, fields):
    allowed = {"approve_by", "approve_at", "note", "metrics", "audit", "paper_json", "spec"}
    with conn() as c:
        sets, vals = ["state=?", "updated=?"], [to_state, int(time.time())]
        for k, v in fields.items():
            if k not in allowed:
                raise KeyError(k)
            sets.append(f"{k}=?")
            vals.append(json.dumps(v) if k in {"metrics", "audit", "paper_json", "spec"} else v)
        vals += [sid, from_state]
        cur = c.execute(f"UPDATE strategies SET {', '.join(sets)} WHERE id=? AND state=?", vals)
        if cur.rowcount != 1:
            raise RuntimeError(f"状态转移失败:{sid} 不是 {from_state}(可能已被其他进程改动)")
        _ev(c, sid, kind, detail)
        c.commit()
    return get(sid)
