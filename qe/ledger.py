"""逐笔台账(ledger):所有信号与成交明细,供回测-实盘对账。

两张语义合一表:
  kind='signal' —— 信号(不论是否成交),用于比对"漏单/多单"
  kind='trade'  —— 完整一笔(入场→出场),含成本拆解,用于比对"每笔盈亏"
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "ledger.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  sid       TEXT NOT NULL,
  coin      TEXT NOT NULL,
  source    TEXT NOT NULL,          -- loop | backtest
  kind      TEXT NOT NULL,          -- signal | trade
  signal_ts REAL,
  ref_px    REAL,
  limit_px  REAL,
  target_px REAL,
  fill_ts   REAL,
  fill_px   REAL,
  exit_ts   REAL,
  exit_px   REAL,
  hold_min  REAL,
  gross_bp  REAL,
  fee_bp    REAL,
  funding_bp REAL,
  impact_bp REAL,
  net_bp    REAL,
  reason    TEXT,
  created   REAL
);
CREATE INDEX IF NOT EXISTS ix_ledger ON ledger (sid, source, kind, signal_ts);
"""


@contextmanager
def conn():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        c.executescript(SCHEMA)
        yield c
        c.commit()
    finally:
        c.close()


def add(**kw) -> int:
    kw.setdefault("created", time.time())
    cols = ",".join(kw)
    ph = ",".join("?" * len(kw))
    with conn() as c:
        cur = c.execute(f"INSERT INTO ledger ({cols}) VALUES ({ph})", tuple(kw.values()))
        return int(cur.lastrowid)


def rows(sid: str, source: str = "loop", kind: str | None = None,
         since: float | None = None) -> list[dict]:
    q = "SELECT * FROM ledger WHERE sid=? AND source=?"
    args: list = [sid, source]
    if kind:
        q += " AND kind=?"
        args.append(kind)
    if since:
        q += " AND signal_ts>=?"
        args.append(since)
    q += " ORDER BY signal_ts"
    with conn() as c:
        return [dict(r) for r in c.execute(q, args)]


def clear(sid: str, source: str = "loop") -> int:
    with conn() as c:
        cur = c.execute("DELETE FROM ledger WHERE sid=? AND source=?", (sid, source))
        return cur.rowcount


def summary(sid: str, source: str = "loop", since: float | None = None) -> dict:
    rs = rows(sid, source, "trade", since)
    sig = rows(sid, source, "signal", since)
    n = len(rs)
    if not n:
        return {"trades": 0, "signals": len(sig), "mean_bp": 0.0, "hit": 0.0,
                "fill_rate": 0.0, "hold_med": 0.0}
    nets = [r["net_bp"] or 0.0 for r in rs]
    return {
        "trades": n, "signals": len(sig),
        "mean_bp": sum(nets) / n,
        "hit": sum(1 for x in nets if x > 0) / n,
        "fill_rate": n / len(sig) if sig else 0.0,
        "hold_med": sorted(r["hold_min"] or 0 for r in rs)[n // 2],
        "sum_bp": sum(nets),
    }
