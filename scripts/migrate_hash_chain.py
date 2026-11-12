#!/usr/bin/env python3
"""一次性迁移:给 events 表加 prev_hash/hash 并回填哈希链。

回填必须临时删掉 append-only 触发器(它就是用来阻止这类改动的)——
这正是需要哈希链的原因:触发器可以被 DROP,链不能。
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

DB = Path("/home/ubuntu/apps/crypto-quant-engine/data/pipeline.db")


def main() -> int:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    cols = {r["name"] for r in c.execute("PRAGMA table_info(events)")}
    if "hash" not in cols:
        c.execute("ALTER TABLE events ADD COLUMN prev_hash TEXT")
        c.execute("ALTER TABLE events ADD COLUMN hash TEXT")
        print("已加列 prev_hash / hash")

    c.executescript("""
        DROP TRIGGER IF EXISTS events_no_update;
        DROP TRIGGER IF EXISTS events_no_delete;
    """)
    rows = c.execute("SELECT id, ts, strategy_id, kind, detail FROM events ORDER BY id").fetchall()
    prev = "genesis"
    n = 0
    for r in rows:
        h = hashlib.sha256(
            f"{prev}|{r['id']}|{r['ts']}|{r['strategy_id']}|{r['kind']}|{r['detail']}".encode()
        ).hexdigest()
        c.execute("UPDATE events SET prev_hash=?, hash=? WHERE id=?", (prev, h, r["id"]))
        prev = h
        n += 1
    c.executescript("""
        CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
        BEGIN SELECT RAISE(ABORT, 'events 是 append-only 审计日志,禁止修改'); END;
        CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
        BEGIN SELECT RAISE(ABORT, 'events 是 append-only 审计日志,禁止删除'); END;
    """)
    c.commit()
    print(f"回填 {n} 条,已重建触发器")
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
