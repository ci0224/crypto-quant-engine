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
  kind TEXT NOT NULL,              -- register/backtest/review/promote/approve/halt/retire/paper_tick
  detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_ev ON events(strategy_id, ts DESC);
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
    c.execute("INSERT INTO events(strategy_id, ts, kind, detail) VALUES(?,?,?,?)",
              (sid, int(time.time()), kind, detail))


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
            "SELECT * FROM events WHERE strategy_id=? ORDER BY ts DESC LIMIT ?", (sid, limit))]


def _row(r):
    d = dict(r)
    for k in ("spec", "metrics", "audit", "equity", "paper_json"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    d["symbols"] = json.loads(d["symbols"]) if d.get("symbols") else []
    return d
