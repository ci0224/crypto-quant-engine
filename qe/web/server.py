"""策略流水线可视化:stdlib HTTP + JSON API(零依赖,与 options_lens 同风格)。

默认只监听 127.0.0.1:8091(实盘审批接口不能裸奔公网)。
如需外网访问:用 Caddy/nginx 反代 + Basic Auth,再设 QE_TOKEN。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent.parent))

from qe import gate, pipeline, registry  # noqa: E402

STATIC = ROOT / "static"
TOKEN = os.environ.get("QE_TOKEN", "")
HITS: dict[str, list[float]] = {}
LOCK = threading.Lock()


def _rate_ok(ip: str, limit: int = 120) -> bool:
    now = time.time()
    with LOCK:
        dq = HITS.setdefault(ip, [])
        while dq and now - dq[0] > 60:
            dq.pop(0)
        if len(dq) >= limit:
            return False
        dq.append(now)
    return True


def _clean(o):
    """把 NaN/Inf 变成 None —— 它们不是合法 JSON,JS 的 JSON.parse 会直接报错。"""
    if isinstance(o, float):
        return None if (o != o or o in (float("inf"), float("-inf"))) else o
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def _json(obj, code=200):
    body = json.dumps(_clean(obj), ensure_ascii=False, default=str).encode()
    return code, "application/json; charset=utf-8", body


def _strategies():
    out = []
    for s in registry.all():
        out.append({
            "id": s["id"], "name": s["name"], "family": s["family"],
            "symbols": s["symbols"], "state": s["state"],
            "spec": s.get("spec"), "metrics": s.get("metrics") or {},
            "audit": s.get("audit") or {},
            # 注意:必须返回 None 而不是 {}(空对象在 JS 里是真值,会让前端误判"有数据但来源未知")
            "paper": (s.get("paper_json") if s.get("paper_json") else None),
            "live_checks": gate.live_checks(s.get("metrics") or {}),
            "paper_source": (s.get("paper_json") or {}).get("source"),
            "approve_by": s.get("approve_by"),
            "approve_at": s.get("approve_at"), "note": s.get("note"),
            "updated": s.get("updated"),
        })
    return out




def _reconcile():
    """读最近一次对账结果(每个策略取最新一条)。"""
    import sqlite3
    from pathlib import Path
    db = Path(__file__).resolve().parent.parent.parent / "data" / "ledger.db"
    out = {}
    if not db.exists():
        return out
    try:
        c = sqlite3.connect(db, timeout=5)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT r.* FROM reconcile_runs r JOIN (SELECT sid, MAX(ts) mt FROM reconcile_runs GROUP BY sid) "
            "m ON r.sid=m.sid AND r.ts=m.mt").fetchall()
        for r in rows:
            out[r["sid"]] = {"ts": r["ts"], "days": r["days"], "parity": r["parity"],
                             "trades_loop": r["trades_loop"], "trades_bt": r["trades_bt"],
                             "paired": r["paired"], "mean_diff": r["mean_diff"],
                             "missing": r["missing"], "extra": r["extra"]}
    except Exception:
        pass
    return out

def handle_api(method: str, path: str, query: dict, payload: dict | None, headers=None):
    if method == "GET" and path == "/api/reconcile":
        return _json({"items": _reconcile()})
    if method == "GET" and path == "/api/strategies":
        return _json({"items": _strategies(),
                      "policies": {"paper_min_days": pipeline.PAPER_MIN_DAYS,
                                   "paper_min_trades": pipeline.PAPER_MIN_TRADES,
                                   "paper_max_dd_mult": pipeline.PAPER_MAX_DD_MULT,
                                   "t_min": gate.T_MIN, "mae_buffer": gate.MAE_BUFFER,
                                   "cost_stress": gate.COST_STRESS}})

    if method == "GET" and path.startswith("/api/strategy/"):
        sid = path.rsplit("/", 1)[-1]
        s = registry.get(sid)
        if not s:
            return _json({"error": "not found"}, 404)
        return _json({"item": s, "events": registry.events(sid, 40),
                      "checks": (s.get("audit") or {}).get("checks", []),
                      "live_checks": gate.live_checks(s.get("metrics") or {})})

    if method == "POST" and path == "/api/action":
        p = payload or {}
        sid, action, by = p.get("sid", ""), p.get("action", ""), (p.get("by") or "").strip()
        # 令牌可放 body(token)或 header(X-QE-Token);header 更规范(不进请求体日志)
        given = p.get("token") or (headers or {}).get("X-QE-Token") or ""
        if TOKEN and given != TOKEN:
            return _json({"error": "invalid token"}, 403)
        try:
            if action == "review":
                s, res = pipeline.review(sid)
                return _json({"ok": True, "state": s["state"], "audit": res})
            if action == "promote_paper":
                return _json({"ok": True, "state": pipeline.promote_paper(sid)["state"]})
            if action == "paper_tick":
                # 演示用:用一个 tick 模拟纸交易进度(实盘由 paper loop 写)
                st = p.get("stats") or {}
                s = pipeline.paper_tick(sid, st)
                return _json({"ok": True, "state": (s or {}).get("state")})
            if action == "approve":
                s = pipeline.approve(sid, by or "unknown", p.get("note", ""))
                return _json({"ok": True, "state": s["state"], "approve_by": s["approve_by"]})
            if action == "reset_paper":
                return _json({"ok": True, "state": pipeline.reset_paper(sid, p.get("note", ""))["state"]})
            if action == "halt":
                return _json({"ok": True, "state": pipeline.halt(sid, p.get("note", ""))["state"]})
            if action == "retire":
                return _json({"ok": True, "state": pipeline.retire(sid, p.get("note", ""))["state"]})
            return _json({"error": f"unknown action {action}"}, 400)
        except Exception as e:
            return _json({"error": f"{type(e).__name__}: {e}"}, 400)

    return _json({"error": "not found"}, 404)


class H(BaseHTTPRequestHandler):
    server_version = "qe"
    sys_version = ""

    def _send(self, code, ctype, body, head=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if not head:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self):
        u = urlparse(self.path)
        ip = self.client_address[0]
        if u.path.startswith("/api/") and not _rate_ok(ip):
            return self._send(429, "application/json", b'{"error":"slow down"}')
        if u.path.startswith("/api/"):
            code, ct, body = handle_api("GET", u.path, parse_qs(u.query), None)
            return self._send(code, ct, body)
        alias = {"/": "index.html", "": "index.html", "/transfer": "transfer.html"}
        rel = alias.get(u.path, u.path.lstrip("/"))
        if ".." in rel:
            return self._send(404, "text/plain", b"not found")
        fp = (STATIC / rel).resolve()
        if not str(fp).startswith(str(STATIC.resolve())) or not fp.is_file():
            return self._send(404, "text/plain", b"not found")
        ctype = "text/html; charset=utf-8" if fp.suffix == ".html" else "application/octet-stream"
        self._send(200, ctype, fp.read_bytes())

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        ip = self.client_address[0]
        if not _rate_ok(ip):
            return self._send(429, "application/json", b'{"error":"slow down"}')
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            payload = {}
        code, ct, body = handle_api("POST", u.path, parse_qs(u.query), payload, self.headers)
        self._send(code, ct, body)

    def log_message(self, *a):
        pass


def main():
    port = int(os.environ.get("QE_PORT", "8091"))
    # 默认 0.0.0.0:8091 —— 容器要经 docker 网关(172.17.0.1)访问,**不能只绑 loopback**。
    # 对外安全靠两层:云安全组不放行 8091 + Caddy 的 Basic Auth + 写操作 QE_TOKEN。
    host = os.environ.get("QE_HOST", "0.0.0.0")
    srv = ThreadingHTTPServer((host, port), H)
    print(f"pipeline UI: http://{host}:{port}  (token {'on' if TOKEN else 'off'})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
