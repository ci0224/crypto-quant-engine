"""Empirical: 2 processes x N writes, delete vs WAL; deferred->write upgrade; trigger+hash chain."""
import os, sqlite3, sys, time, multiprocessing as mp, hashlib, json, random

N = 300

def worker(db, mode, wid, start_at, q):
    c = sqlite3.connect(db, timeout=5.0, isolation_level=None)
    c.execute("PRAGMA busy_timeout=5000")
    if mode == "wal":
        c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    while time.time() < start_at:
        time.sleep(0.001)
    err = 0
    t0 = time.time()
    for i in range(N):
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT INTO t(w, i) VALUES(?,?)", (wid, i))
            c.execute("COMMIT")
        except sqlite3.OperationalError as e:
            err += 1
            try: c.execute("ROLLBACK")
            except Exception: pass
    q.put((err, time.time() - t0))

def bench(mode):
    db = f"/tmp/conc_{mode}.db"
    for suffix in ("", "-wal", "-shm", "-journal"):
        try: os.unlink(db + suffix)
        except FileNotFoundError: pass
    c = sqlite3.connect(db, isolation_level=None)
    c.execute("PRAGMA journal_mode=" + ("WAL" if mode == "wal" else "DELETE"))
    c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, w INT, i INT)")
    c.close()
    q = mp.Queue(); start_at = time.time() + 0.5
    ps = [mp.Process(target=worker, args=(db, mode, k, start_at, q)) for k in range(4)]
    [x.start() for x in ps]; [x.join() for x in ps]
    res = [q.get() for _ in ps]
    total_err = sum(r[0] for r in res)
    c = sqlite3.connect(db)
    n = c.execute("select count(*) from t").fetchone()[0]
    c.close()
    print(f"{mode:7s} rows={n:4d}/{4*N} errors={total_err:4d} wall={max(r[1] for r in res):.2f}s")

def deferred_upgrade():
    """read-then-write in DEFERRED tx while another conn commits -> SQLITE_BUSY_SNAPSHOT"""
    db = "/tmp/upgrade.db"
    for s in ("", "-wal", "-shm"):
        try: os.unlink(db + s)
        except FileNotFoundError: pass
    a = sqlite3.connect(db, isolation_level=None); a.execute("PRAGMA journal_mode=WAL")
    a.execute("CREATE TABLE s(id INT PRIMARY KEY, state TEXT)"); a.execute("INSERT INTO s VALUES(1,'awaiting_approval')")
    b = sqlite3.connect(db, isolation_level=None, timeout=3); b.execute("PRAGMA busy_timeout=3000")
    a.execute("BEGIN DEFERRED"); print("A reads:", a.execute("select state from s where id=1").fetchone())
    b.execute("BEGIN IMMEDIATE"); b.execute("UPDATE s SET state='halted' WHERE id=1"); b.execute("COMMIT")
    try:
        a.execute("UPDATE s SET state='live' WHERE id=1")
        a.execute("COMMIT"); print("A upgrade: OK (unexpected)")
    except sqlite3.OperationalError as e:
        print("A upgrade failed:", e)

if __name__ == "__main__":
    bench("delete"); bench("wal"); deferred_upgrade()
