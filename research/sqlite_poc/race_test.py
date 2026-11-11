"""Reproduce check-then-update race in qe.pipeline.approve() pattern on a COPY of pipeline.db."""
import os, shutil, sqlite3, time, multiprocessing as mp

SRC = "/home/ubuntu/apps/crypto-quant-engine/data/pipeline.db"
DB = "/tmp/race.db"





def naive(who, q, start_at):
    SID = os.environ["SID"]
    c = sqlite3.connect(DB, timeout=10)          # same as registry.conn()
    c.execute("PRAGMA busy_timeout=10000")
    while time.time() < start_at:
        time.sleep(0.001)
    try:
        st = c.execute("SELECT state FROM strategies WHERE id=?", (SID,)).fetchone()[0]  # check
        if st != "awaiting_approval":
            q.put((who, "rejected-precheck", st)); return
        time.sleep(0.15)                                                   # work / gate checks
        n = c.execute("UPDATE strategies SET state='live',approve_by=? WHERE id=?", (who, SID)).rowcount
        c.execute("INSERT INTO events(strategy_id,ts,kind,detail) VALUES(?,?,'approve',?)",
                  (SID, int(time.time()), f"by={who}"))
        c.commit()
        q.put((who, "approved", n))
    except Exception as e:
        q.put((who, "error", str(e)))
    finally:
        c.close()


def guarded(who, q, start_at):
    SID = os.environ["SID"]
    c = sqlite3.connect(DB, timeout=10, isolation_level=None)   # explicit tx control
    c.execute("PRAGMA busy_timeout=10000")
    while time.time() < start_at:
        time.sleep(0.001)
    try:
        c.execute("BEGIN IMMEDIATE")
        n = c.execute("UPDATE strategies SET state='live',approve_by=? "
                      "WHERE id=? AND state='awaiting_approval'", (who, SID)).rowcount
        time.sleep(0.15)
        if n != 1:
            c.execute("ROLLBACK"); q.put((who, "rejected-precheck", n)); return
        c.execute("INSERT INTO events(strategy_id,ts,kind,detail) VALUES(?,?,'approve',?)",
                  (SID, int(time.time()), f"by={who}"))
        c.execute("COMMIT")
        q.put((who, "approved", n))
    except Exception as e:
        q.put((who, "error", str(e)))
    finally:
        c.close()


def run(fn, label):
    shutil.copy(SRC, DB)
    for s in ("-wal", "-shm"):
        try: os.unlink(DB + s)
        except FileNotFoundError: pass
    c = sqlite3.connect(DB)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("UPDATE strategies SET state='awaiting_approval' WHERE id=?", (os.environ["SID"],))
    c.commit(); c.close()
    q = mp.Queue(); start = time.time() + 0.4
    ps = [mp.Process(target=fn, args=(w, q, start)) for w in ("alice", "bob")]
    [p.start() for p in ps]; [p.join() for p in ps]
    res = [q.get() for _ in ps]
    c = sqlite3.connect(DB)
    ev = c.execute("SELECT count(*) FROM events WHERE kind='approve'").fetchone()[0]
    c.close()
    print(f"{label:8s} outcomes={res} approve_rows={ev}")


if __name__ == "__main__":
    _c = sqlite3.connect(SRC); SID = _c.execute("select id from strategies limit 1").fetchone()[0]; _c.close()
    os.environ["SID"] = SID
    run(naive, "naive")
    run(guarded, "guarded")
