import sqlite3, hashlib, json, time, os

DB = "/tmp/poc_audit.db"
for s in ("", "-wal", "-shm"):
    try: os.unlink(DB + s)
    except FileNotFoundError: pass

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE strategies(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, spec TEXT NOT NULL, created INTEGER NOT NULL);

CREATE TABLE transitions(from_state TEXT NOT NULL, to_state TEXT NOT NULL,
  PRIMARY KEY(from_state, to_state));
INSERT INTO transitions VALUES
 ('draft','backtested'),('draft','rejected'),
 ('backtested','reviewed'),('backtested','rejected'),('backtested','backtested'),
 ('reviewed','paper'),('reviewed','rejected'),
 ('paper','awaiting_approval'),('paper','halted'),('paper','paper'),
 ('awaiting_approval','live'),('awaiting_approval','paper'),('awaiting_approval','halted'),
 ('live','halted'),('live','live'),
 ('halted','paper'),('halted','retired'),
 ('rejected','backtested'),('rejected','retired');

CREATE TABLE events(
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  sid         TEXT NOT NULL REFERENCES strategies(id),
  ts          INTEGER NOT NULL,
  actor       TEXT NOT NULL CHECK(length(actor) > 0),
  kind        TEXT NOT NULL,
  from_state  TEXT,
  to_state    TEXT NOT NULL,
  payload     TEXT NOT NULL,          -- canonical JSON: the evidence the decision used
  payload_sha TEXT NOT NULL CHECK(length(payload_sha)=64),
  prev_hash   TEXT NOT NULL,
  hash        TEXT NOT NULL UNIQUE CHECK(length(hash)=64)
);
CREATE INDEX ix_ev ON events(sid, seq DESC);

-- 1. no forks / no rebuild: chain must extend the current head
CREATE TRIGGER ev_chain BEFORE INSERT ON events
BEGIN
  SELECT RAISE(ABORT,'chain broken: prev_hash != head')
  WHERE NEW.prev_hash <> COALESCE((SELECT hash FROM events ORDER BY seq DESC LIMIT 1),'genesis');
  SELECT RAISE(ABORT,'bad transition')
  WHERE NEW.from_state IS NOT NULL
    AND NOT EXISTS(SELECT 1 FROM transitions t
                   WHERE t.from_state=NEW.from_state AND t.to_state=NEW.to_state);
  -- 2. provenance gate: demo/manual data can never authorize live trading
  SELECT RAISE(ABORT,'approval requires source=loop paper data')
  WHERE NEW.to_state='live'
    AND (json_extract(NEW.payload,'$.paper.source') IS NOT 'loop'
         OR json_extract(NEW.payload,'$.paper.demo') IS NOT 0);
END;

CREATE TRIGGER ev_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT,'events is append-only'); END;
CREATE TRIGGER ev_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT,'events is append-only'); END;

CREATE VIEW v_state AS
  SELECT e.sid, e.to_state AS state, e.seq
  FROM events e WHERE e.seq=(SELECT MAX(seq) FROM events WHERE sid=e.sid);
"""

con = sqlite3.connect(DB, isolation_level=None)
con.executescript(DDL)
con.execute("INSERT INTO strategies VALUES('s1','mom','{}',?)", (int(time.time()),))


def canon(d): return json.dumps(d, sort_keys=True, separators=(",", ":"))


def append(sid, actor, kind, to_state, payload, from_state=None):
    con.execute("BEGIN IMMEDIATE")
    try:
        head = con.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev = head[0] if head else "genesis"
        p = canon(payload)
        body = "|".join([prev, sid, str(int(time.time())), actor, kind,
                         from_state or "", to_state, p])
        h = hashlib.sha256(body.encode()).hexdigest()
        con.execute("""INSERT INTO events(sid,ts,actor,kind,from_state,to_state,payload,payload_sha,prev_hash,hash)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (sid, int(time.time()), actor, kind, from_state, to_state, p,
                     hashlib.sha256(p.encode()).hexdigest(), prev, h))
        con.execute("COMMIT")
        return True
    except sqlite3.DatabaseError as e:
        con.execute("ROLLBACK")
        return str(e)


print("1 register      :", append("s1", "human:alice", "register", "draft", {"spec": {}}))
print("2 draft->live   :", append("s1", "human:eve", "approve", "live", {"paper": {"source": "loop", "demo": 0}}, "draft"))
print("3 ->backtested  :", append("s1", "svc:qe-batch", "backtest", "backtested", {"n": 240, "t": 2.9}, "draft"))
print("4 approve w/demo :", append("s1", "human:alice", "approve", "live", {"paper": {"source": "manual", "demo": 1}}, "awaiting_approval"))
print("5 approve real   :", append("s1", "human:alice", "approve", "live", {"paper": {"source": "loop", "demo": 0}}, "awaiting_approval"))

for sql in ("UPDATE events SET actor='x' WHERE seq=1", "DELETE FROM events WHERE seq=1"):
    try:
        con.execute(sql); print("!! allowed:", sql)
    except sqlite3.DatabaseError as e:
        print("blocked:", sql, "->", e)

print("state:", con.execute("SELECT * FROM v_state").fetchall())
print("chain:", [(r[0], r[1][:8], r[2][:8]) for r in con.execute("SELECT seq,prev_hash,hash FROM events")])
