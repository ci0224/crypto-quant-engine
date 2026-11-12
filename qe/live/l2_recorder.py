"""L2 订单簿录制器:为队列位置建模提供真实盘口序列。

物理限制(Hyperliquid):
  · l2Book 只返回 20 档 ≈ 3bp(BTC 3.2bp / SOL 2.6bp)
  · 没有 L3(逐订单)数据 → 无法直接知道"我前面排多少量"

所以在 HL 上能做到的最好方案是**两档采样**:
  ① 常态:2Hz,记"带状累计深度" + 前 5 档 → 便宜,长期留存
  ② 逼近:当 mid 距离任一挂单价 < 10bp 时,切到**每条推送都记全档**
     —— 因为只有价格逼近时,我们的价位才进入可见窗口,这段数据才是队列建模的关键

存储:SQLite(WAL),data/l2.db
  l2_band   常态行(1 行/快照)
  l2_full   逼近行(1 行/档/快照)
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

import websocket

WS_URL = "wss://api.hyperliquid.xyz/ws"
DB = Path(__file__).resolve().parent.parent.parent / "data" / "l2.db"
WATCH_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "l2_watch.json"
COINS = ["BTC", "ETH", "SOL"]
BANDS_BP = [1, 3, 10, 30, 100]
IDLE_HZ = 2.0
HOT_BP = 10.0          # 距挂单价 < 10bp 进入热模式
HOT_HOLD_S = 120       # 热模式在最后一次逼近后保持 120s

SCHEMA = """
CREATE TABLE IF NOT EXISTS l2_band(
  ts_ms INTEGER, coin TEXT, mid REAL, spread_bp REAL,
  bid_1 REAL, bid_3 REAL, bid_10 REAL, bid_30 REAL, bid_100 REAL,
  ask_1 REAL, ask_3 REAL, ask_10 REAL, ask_30 REAL, ask_100 REAL,
  hot INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS l2_full(
  ts_ms INTEGER, coin TEXT, side TEXT, lvl INTEGER, px REAL, sz REAL, n INTEGER);
CREATE INDEX IF NOT EXISTS ix_band ON l2_band(coin, ts_ms);
CREATE INDEX IF NOT EXISTS ix_full ON l2_full(coin, ts_ms);
"""

_lock = threading.Lock()
_counts = {"band": 0, "full": 0}


def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    # WS 回调在另一个线程写库 → 必须关掉 check_same_thread
    c = sqlite3.connect(DB, timeout=15, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript(SCHEMA)
    return c


def _bands(bids, asks, mid):
    """累计美元深度(按距 mid 的 bp 分带)。"""
    out = {}
    for side, lvls, sgn in (("bid", bids, -1), ("ask", asks, 1)):
        for bp in BANDS_BP:
            lim = mid * (1 + sgn * bp / 1e4)
            tot = 0.0
            for x in lvls:
                px, sz = float(x["px"]), float(x["sz"])
                if (sgn < 0 and px >= lim) or (sgn > 0 and px <= lim):
                    tot += px * sz
            out[f"{side}_{bp}"] = tot
    return out


def _watch_prices():
    try:
        return [float(v) for v in json.loads(WATCH_FILE.read_text()).values() if v]
    except Exception:
        return []


def handle(conn, msg, state):
    if msg.get("channel") != "l2Book":
        return
    d = msg.get("data") or {}
    coin = d.get("coin")
    levels = d.get("levels") or []
    if coin not in COINS or len(levels) < 2 or not levels[0] or not levels[1]:
        return
    bids, asks = levels[0], levels[1]
    mid = (float(bids[0]["px"]) + float(asks[0]["px"])) / 2
    if mid <= 0:
        return
    _mids[coin] = mid
    now = int(time.time() * 1000)
    spread_bp = (float(asks[0]["px"]) - float(bids[0]["px"])) / mid * 1e4

    # 热模式判定:mid 与任一挂单价的距离
    hot = False
    for w in _watch_prices():
        if abs(mid - w) / mid * 1e4 < HOT_BP:
            hot = True
            state["hot_until"] = time.time() + HOT_HOLD_S
            break
    if time.time() < state.get("hot_until", 0):
        hot = True

    # 常态:限频
    if not hot and now - state.get("last_band", 0) < int(1000 / IDLE_HZ):
        return
    state["last_band"] = now

    b = _bands(bids, asks, mid)
    with _lock:
        conn.execute(
            "INSERT INTO l2_band VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, coin, mid, spread_bp,
             b["bid_1"], b["bid_3"], b["bid_10"], b["bid_30"], b["bid_100"],
             b["ask_1"], b["ask_3"], b["ask_10"], b["ask_30"], b["ask_100"],
             1 if hot else 0))
        _counts["band"] += 1
        if hot:
            rows = []
            for side, lvls in (("bid", bids), ("ask", asks)):
                for i, x in enumerate(lvls):
                    rows.append((now, coin, side, i, float(x["px"]), float(x["sz"]), int(x.get("n", 0))))
            conn.executemany("INSERT INTO l2_full VALUES (?,?,?,?,?,?,?)", rows)
            _counts["full"] += len(rows)
        conn.commit()       # 每批提交:避免长事务导致 "database is locked"




# ---------------- 热模式:REST 高频轮询 ----------------
REST = "https://api.hyperliquid.xyz/info"
HOT_POLL_HZ = 10.0          # 100ms 一次;3 币全热也只有 1800/min,HL 上限约 1200/min
                            # → 只对"热"的那一个币轮询,实际 600/min
_mids = {}
_hot_stop = threading.Event()


def _rest_l2(coin: str):
    import urllib.request
    body = json.dumps({"type": "l2Book", "coin": coin}).encode()
    req = urllib.request.Request(REST, data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _prune(conn, full_days: int = 14, band_days: int = 90):
    """定期清理:l2_full 体积大只留 14 天,l2_band 留 90 天。"""
    now = int(time.time() * 1000)
    with _lock:
        conn.execute("DELETE FROM l2_full WHERE ts_ms < ?", (now - full_days * 86400_000,))
        conn.execute("DELETE FROM l2_band WHERE ts_ms < ?", (now - band_days * 86400_000,))
        conn.commit()


def hot_loop(conn):
    """当 mid 逼近任一挂单价(<HOT_BP)时,用 REST 以 HOT_POLL_HZ 抓全档。"""
    last_prune = 0.0
    while not _hot_stop.is_set():
        if time.time() - last_prune > 3600:
            try:
                _prune(conn)
            except Exception:
                pass
            last_prune = time.time()
        watches = _watch_prices()
        if not watches:
            time.sleep(0.5)
            continue
        hot_coins = [c for c, m in list(_mids.items())
                     if m and any(abs(m - w) / m * 1e4 < HOT_BP for w in watches)]
        if not hot_coins:
            time.sleep(0.3)
            continue
        for coin in hot_coins:
            try:
                d = _rest_l2(coin)
                levels = d.get("levels") or []
                if len(levels) < 2 or not levels[0] or not levels[1]:
                    continue
                bids, asks = levels[0], levels[1]
                mid = (float(bids[0]["px"]) + float(asks[0]["px"])) / 2
                now = int(time.time() * 1000)
                b = _bands(bids, asks, mid)
                rows = []
                for side, lvls in (("bid", bids), ("ask", asks)):
                    for i, x in enumerate(lvls):
                        rows.append((now, coin, side, i, float(x["px"]),
                                     float(x["sz"]), int(x.get("n", 0))))
                with _lock:
                    conn.execute(
                        "INSERT INTO l2_band VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (now, coin, mid, (float(asks[0]["px"]) - float(bids[0]["px"])) / mid * 1e4,
                         b["bid_1"], b["bid_3"], b["bid_10"], b["bid_30"], b["bid_100"],
                         b["ask_1"], b["ask_3"], b["ask_10"], b["ask_30"], b["ask_100"], 2))
                    conn.executemany("INSERT INTO l2_full VALUES (?,?,?,?,?,?,?)", rows)
                    conn.commit()
                _counts["full"] += len(rows)
            except Exception:
                pass
            time.sleep(1.0 / HOT_POLL_HZ)


def main():
    conn = db()
    state = {}
    threading.Thread(target=hot_loop, args=(conn,), daemon=True).start()
    stop = {"v": False}

    def _sig(*_):
        stop["v"] = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    def on_open(ws):
        for c in COINS:
            ws.send(json.dumps({"method": "subscribe",
                                "subscription": {"type": "l2Book", "coin": c}}))
        print(f"[l2] 已订阅 {COINS}", file=sys.stderr, flush=True)

    def on_message(ws, message):
        try:
            handle(conn, json.loads(message), state)
        except Exception as e:  # 单条坏消息不该杀死录制器
            print(f"[l2] 解析失败: {e}", file=sys.stderr, flush=True)

    while not stop["v"]:
        try:
            ws = websocket.WebSocketApp(
                WS_URL, on_open=on_open, on_message=on_message,
                on_error=lambda w, e: print(f"[l2] ws 错误: {e!r}"[:200], file=sys.stderr, flush=True),
                on_close=lambda w, *a: print("[l2] ws 关闭", file=sys.stderr, flush=True))
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"[l2] 断线: {e}", file=sys.stderr, flush=True)
        if stop["v"]:
            break
        time.sleep(3)
    conn.commit()
    conn.close()
    print(f"[l2] 退出,共写入 band={_counts['band']} full={_counts['full']}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
