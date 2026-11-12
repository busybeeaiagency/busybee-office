"""Serves cubicle's vendored client plus the BUSY BEE JSON APIs, stdlib only.

Endpoints (the page discovers the feed through /config.json):
  GET  /                     the office page (public/index.html)
  GET  /config.json          which feed source(s) to poll -> /api/feed/0 (polled every 4 s)
  GET  /api/feed, /api/feed/0  the live roster built from /hermes/kanban.db
  GET  /api/office/vitals    host + services + Beszel + Kuma (cached 15 s)
  GET  /api/office/chat      Mattermost presence + latest #off-topic posts (1 req/min)
  GET  /healthz              liveness for the container
  GET  /api/appearance       {} (looks live in the browser's localStorage)
  GET  /api/cubicle/settings {"createTasks": false, ...}
  anything else              a file from public/, else 404

No credential is ever written into a response: the Mattermost token and the Beszel/Kuma
credentials are read server-side from /hermes/creds and stay in this process.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import feed, roster, sources  # noqa: E402

PUBLIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public")
DB_PATH = os.environ.get("OFFICE_DB", f"{sources.HERMES_DIR}/kanban.db")
PORT = int(os.environ.get("OFFICE_PORT", "9120"))
BUILD = os.environ.get("OFFICE_BUILD", "busybee-office")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml",
    ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon",
    ".map": "application/json", ".txt": "text/plain; charset=utf-8",
}

_lock = threading.Lock()
_feed = {"at": 0.0, "data": None, "error": ""}
sources_api = sources.Sources()


def current_feed():
    """The roster feed, refreshed at most once a second; the last good one survives errors."""
    with _lock:
        if _feed["data"] and time.time() - _feed["at"] < 1.0:
            return _feed["data"]
    try:
        chat = {}
        chatter = sources_api.chatter()
        for post in chatter.get("posts", []):
            user = post.get("user")
            msg = (post.get("message") or "").strip()
            if user and msg:
                chat[user] = msg[:140]
        data = feed.build_feed(feed.read_tasks(DB_PATH), chat_by_agent=chat)
        with _lock:
            _feed.update({"at": time.time(), "data": data, "error": ""})
        return data
    except Exception as e:
        sys.stderr.write(f"[feed] {type(e).__name__}: {e}\n")
        with _lock:
            if _feed["data"]:
                _feed["error"] = f"{type(e).__name__}: {e}"
                return _feed["data"]
        return {"company": feed.COMPANY, "agents": []}


class Handler(BaseHTTPRequestHandler):
    server_version = "busybee-office"

    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache="no-store"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _static(self, path):
        rel = unquote(path).lstrip("/")
        full = os.path.realpath(os.path.join(PUBLIC_DIR, rel))
        if not full.startswith(os.path.realpath(PUBLIC_DIR)) or not os.path.isfile(full):
            return self._send(404, '{"error":"not found"}')
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            body = f.read()
        cache = "no-store" if ext in (".html", ".json") else "public, max-age=3600"
        self._send(200, body, CONTENT_TYPES.get(ext, "application/octet-stream"), cache)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._static("index.html")
        if path == "/config.json":
            return self._send(200, {
                "sources": [{"kind": "feed", "label": "Hermes kanban", "path": "/api/feed/0"}],
                "source": "feed", "label": "Hermes kanban", "redact": False,
                "version": BUILD, "build": BUILD, "kpiLocal": False,
            })
        if path in ("/api/feed", "/api/feed/0"):
            data = current_feed()
            if _feed["error"]:
                data = {**data, "warning": _feed["error"]}
            return self._send(200, data)
        if path == "/api/office/vitals":
            return self._send(200, sources_api.vitals())
        if path == "/api/office/chat":
            return self._send(200, sources_api.chatter())
        if path == "/healthz":
            return self._send(200, {"ok": True, "db": os.path.exists(DB_PATH), "agents": len(current_feed()["agents"])})
        if path == "/api/appearance":
            return self._send(200, {})
        if path == "/api/cubicle/settings":
            return self._send(200, {"createTasks": False, "canEdit": False, "available": False})
        if path.startswith("/api/"):
            return self._send(404, '{"error":"not found"}')
        return self._static(path)

    def do_HEAD(self):
        self.do_GET()

    def do_PUT(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if path in ("/api/appearance", "/api/cubicle/settings"):
            return self._send(200, {"ok": True})
        return self._send(404, '{"error":"not found"}')


def main():
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write(f"[office] BUSY BEE office on :{PORT} (db={DB_PATH})\n")
    srv.serve_forever()


if __name__ == "__main__":
    main()
