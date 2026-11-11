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


def _json(obj, code=200):
    body = json.dumps(obj, ensure_ascii=False, default=str).encode()
    return code, "application/json; charset=utf-8", body


def _strategies():
    out = []
    for s in registry.all():
        out.append({
            "id": s["id"], "name": s["name"], "family": s["family"],
            "symbols": s["symbols"], "state": s["state"],
            "spec": s.get("spec"), "metrics": s.get("metrics") or {},
            "audit": s.get("audit") or {}, "paper": s.get("paper_json") or {},
            "approve_by": s.get("approve_by"),
            "approve_at": s.get("approve_at"), "note": s.get("note"),
            "updated": s.get("updated"),
        })
    return out


def handle_api(method: str, path: str, query: dict, payload: dict | None):
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
                      "checks": (s.get("audit") or {}).get("checks", [])})

    if method == "POST" and path == "/api/action":
        p = payload or {}
        sid, action, by = p.get("sid", ""), p.get("action", ""), (p.get("by") or "").strip()
        if TOKEN and p.get("token") != TOKEN:
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
        rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
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
        code, ct, body = handle_api("POST", u.path, parse_qs(u.query), payload)
        self._send(code, ct, body)

    def log_message(self, *a):
        pass


def main():
    port = int(os.environ.get("QE_PORT", "8091"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"pipeline UI: http://127.0.0.1:{port}  (token {'on' if TOKEN else 'off'})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
