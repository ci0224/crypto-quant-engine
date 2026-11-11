import os, sqlite3, time

def bench(mode, sync, n=2000, rows_per_tx=1):
    db = f"/tmp/b_{mode}_{sync}.db"
    for s in ("", "-wal", "-shm", "-journal"):
        try: os.unlink(db + s)
        except FileNotFoundError: pass
    c = sqlite3.connect(db, isolation_level=None, timeout=10)
    c.execute("PRAGMA journal_mode=" + ("WAL" if mode == "wal" else "DELETE"))
    c.execute(f"PRAGMA synchronous={sync}")
    c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, a TEXT, b REAL)")
    t0 = time.time()
    for i in range(n):
        c.execute("BEGIN IMMEDIATE")
        for k in range(rows_per_tx):
            c.execute("INSERT INTO t(a,b) VALUES(?,?)", ("x" * 40, k * 1.5))
        c.execute("COMMIT")
    dt = time.time() - t0
    sz = os.path.getsize(db) / 1e6
    wal = os.path.getsize(db + "-wal") / 1e6 if os.path.exists(db + "-wal") else 0
    print(f"{mode:6s} sync={sync:6s} tx={n:5d} rows/tx={rows_per_tx:3d} "
          f"{n/dt:8.0f} tx/s  {(n*rows_per_tx)/dt:9.0f} rows/s  db={sz:.1f}MB wal={wal:.2f}MB")
    c.close()

for mode in ("wal", "delete"):
    for sync in ("FULL", "NORMAL"):
        bench(mode, sync)
bench("wal", "NORMAL", n=200, rows_per_tx=50)
