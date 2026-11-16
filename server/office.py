"""Serves cubicle's vendored client plus the BUSY BEE JSON APIs, stdlib only.

Endpoints (the page discovers the feed through /config.json):
  GET  /                     the office page (public/index.html)
  GET  /config.json          which feed source(s) to poll -> /api/feed/0 (polled every 4 s)
  GET  /api/feed, /api/feed/0  the live roster built from /hermes/kanban.db
  GET  /api/office/vitals    host + services + Beszel + Kuma (cached 15 s)
  GET  /api/office/chat      Mattermost presence + latest #off-topic posts (1 req/min)
  GET  /api/office/actions   whether writes are allowed and which actions exist (capability)
  GET  /api/office/confirm   mint a single-use confirm token for one (action, task)  [x-cubicle]
  GET  /api/office/kanban    the board grouped by agent (?agent=<roster id> for one card list)
  GET  /api/office/dispatch  dry-run preview of one dispatcher pass (spawns nothing)
  POST /api/office/action    perform restart/approve/dismiss on a card               [x-cubicle]
  POST /api/office/dispatch  nudge the dispatcher: one pass, over the dashboard API  [x-cubicle]
  GET  /healthz              liveness for the container
  GET  /api/appearance       {} (looks live in the browser's localStorage)
  GET  /api/cubicle/settings {"createTasks": false, ...}
  anything else              a file from public/, else 404

No credential is ever written into a response: the Mattermost token and the Beszel/Kuma
credentials are read server-side from /hermes/creds and stay in this process.
"""
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import actions, dispatch, feed, kanban_view, roster, sources  # noqa: E402

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


def office_theme():
    """The office this deployment draws — the same answer /config.json gives, sanitised for HTML."""
    return re.sub(r"[^a-z0-9-]", "", os.environ.get("OFFICE_THEME", "busybee")) or "pixel"


def stamp_server_theme(body, theme):
    """Carry that decision INSIDE the page, ahead of its first script.

    A browser that has to ASK which office this is can paint the wrong one first: measured at 4 of
    5 loads the theme chain resolved before /config.json answered, and the wall showed the stock
    pixel office instead of the mansion (aspect 1.69 against 3.15 — which is what made the wrong
    render look cropped with its labels and ticker off-frame). One line before </head> ends the
    race: there is nothing left to wait for and nothing to re-paint.
    """
    # Our own marker, not the word: the page references window.__cubicleServerTheme in its own
    # client code, and guarding on the bare word would skip the stamp forever.
    if b"data-office-theme" in body:
        return body
    script = ('<script data-office-theme="1">window.__cubicleServerTheme=%s;</script>'
              % json.dumps(theme)).encode()
    # Insert after <head>, not before </head>: this page is one 520 KB file that embeds HTML in its
    # own template strings, and a literal "</head>" inside a JS string would land our line deep in
    # the body — after the app script had already chosen the office. After <head> it is simply first.
    i = body.find(b"<head>")
    if i >= 0:
        i += len(b"<head>")
        return body[:i] + script + body[i:]
    i = body.find(b"<script")                      # no <head> at all: still precede the app script
    if i >= 0:
        return body[:i] + script + body[i:]
    return script + body


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
        data = feed.build_feed(feed.read_tasks(DB_PATH), chat_by_agent=chat,
                               previous_agents=(_feed["data"] or {}).get("agents"),
                               gateway=sources_api.gateway_alive())
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


def _clean_note(note):
    """Operator note: strip control chars, trim, cap at 500. Never carries a credential through."""
    if not isinstance(note, str):
        return ""
    return "".join(ch for ch in note if ord(ch) >= 32).strip()[:500]


class Handler(BaseHTTPRequestHandler):
    server_version = "busybee-office"

    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache="no-store",
              retry_after=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
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
        if ext == ".html":
            body = stamp_server_theme(body, office_theme())
        # A live dashboard: never let a browser keep an old theme or page for an hour.
        cache = "public, max-age=3600" if ext in (".png", ".jpg", ".jpeg", ".svg", ".ico", ".woff2") else "no-store"
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
                "theme": office_theme(),
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
            cap = actions.capability()
            return self._send(200, {"ok": True, "db": os.path.exists(DB_PATH),
                                    "agents": len(current_feed()["agents"]),
                                    "writable": cap["writable"],
                                    "actions": actions.performed_count(),
                                    # open approval requests, or null when the queue is not wired here
                                    "queue": actions.queue_status()})
        if path == "/api/office/actions":
            return self._send(200, dict(actions.capability(),
                                        queue=actions.queue_status(),
                                        dispatch=dispatch.capability()))
        if path == "/api/office/confirm":
            if (self.headers.get("x-cubicle") or "").strip() != "1":
                return self._send(403, {"error": "x-cubicle required"})
            q = parse_qs(urlparse(self.path).query)
            action = (q.get("action") or [""])[0]
            task = (q.get("task") or [""])[0]
            if action not in actions.ACTIONS or not actions.TASK_ID_RE.match(task):
                return self._send(400, {"ok": False, "error": "bad_request",
                                        "message": "unknown action or bad task id"})
            meta = actions.ACTIONS[action]
            return self._send(200, {"token": actions.issue_token(action, task),
                                    "expires_in": actions.TOKEN_TTL_S, "action": action,
                                    "task": task, "label": meta["label"], "why": meta["why"],
                                    "preview": actions.preview(action, task)})
        if path == "/api/office/kanban":
            agent = (parse_qs(urlparse(self.path).query).get("agent") or [""])[0].strip()
            data = kanban_view.board(agent or None)
            if data is None:                                   # not a fleet id: never echo the query
                return self._send(404, {"ok": False, "error": "unknown_agent",
                                        "message": f"no such agent in the roster: {agent}"})
            if not data.get("ok"):
                return self._send(503, data)
            return self._send(200, data)
        if path == "/api/office/dispatch":
            # the preview: same handler, dry_run on, nothing spawned, no rate limit
            code, body, retry = dispatch.nudge({"ts": int(time.time()), "actor": "office-dashboard"},
                                               dry_run=True)
            return self._send(code, body, retry_after=retry)
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

    def do_POST(self):
        """The write routes. Every exit (including denials) logs one line to the actions log."""
        path = urlparse(self.path).path
        if path == "/api/office/dispatch":
            return self._dispatch_pass()
        if path != "/api/office/action":
            return self._send(404, '{"error":"not found"}')
        rec = {"ts": int(time.time()), "actor": "dashboard"}

        def answer(code, payload, retry=None):
            actions.log_action(rec)
            return self._send(code, payload, retry_after=retry)

        if (self.headers.get("x-cubicle") or "").strip() != "1":
            rec.update({"ok": False, "error": "x_cubicle_required"})
            return answer(403, {"ok": False, "error": "x_cubicle_required",
                                "message": "x-cubicle required"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4096:
            rec.update({"ok": False, "error": "too_large"})
            return answer(413, {"ok": False, "error": "too_large", "message": "body exceeds 4096 bytes"})
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            rec.update({"ok": False, "error": "bad_json"})
            return answer(400, {"ok": False, "error": "bad_json", "message": "body is not valid JSON"})
        if not isinstance(body, dict):
            body = {}
        action, task, token = body.get("action"), body.get("task"), body.get("token")
        agent = body.get("agent")
        note = _clean_note(body.get("note"))
        rec.update({"action": action, "task": task, "agent": agent})
        # Order of checks is deliberate: cheap/local first, the DB write last.
        if action not in actions.ACTIONS:
            rec.update({"ok": False, "error": "unknown_action"})
            return answer(400, {"ok": False, "error": "unknown_action",
                                "message": f"unknown action: {action}"})
        if not actions.TASK_ID_RE.match(task or ""):
            rec.update({"ok": False, "error": "bad_task_id"})
            return answer(400, {"ok": False, "error": "bad_task_id", "message": "bad task id"})
        cap = actions.capability()
        if not cap["writable"]:
            rec.update({"ok": False, "error": "not_writable"})
            return answer(503, {"ok": False, "error": "not_writable",
                                "message": cap["reason"] or "writes disabled"})
        allowed, retry = actions.check_rate(action, task)
        if not allowed:
            rec.update({"ok": False, "error": "rate_limited"})
            return answer(429, {"ok": False, "error": "rate_limited", "message": "slow down"}, retry)
        # Checked before the token is burned: a throttled retry can re-use the same confirm token,
        # so the user just presses the button again instead of re-confirming.
        ok, reason = actions.consume_token(action, task, token)
        if not ok:
            rec.update({"ok": False, "error": reason})
            return answer(409, {"ok": False, "error": reason, "message": f"confirm token: {reason}"})
        actions.record_hit(action, task)          # only a request that really writes is counted
        try:
            result = actions.perform(action, task, agent=agent, note=note)
        except actions.ActionError as e:
            rec.update({"ok": False, "error": e.code})
            return answer(e.status, {"ok": False, "error": e.code, "message": e.message})
        except Exception as e:                                   # never leak a traceback to the page
            rec.update({"ok": False, "error": type(e).__name__})
            return answer(500, {"ok": False, "error": "internal", "message": type(e).__name__})
        rec.update({"ok": True, "before": result["before"], "after": result["after"],
                    "run_id": result["run_id"]})
        return answer(200, result)

    def _dispatch_pass(self):
        """POST /api/office/dispatch: one dispatcher pass over the dashboard API, never a db write.

        Same guard and same "every exit logs one line" answer as the card actions; the log line
        carries the pass counters, so "the nudge caused a dispatcher pass" is provable afterwards.
        """
        if (self.headers.get("x-cubicle") or "").strip() != "1":
            return self._send(403, {"ok": False, "error": "x_cubicle_required",
                                    "message": "x-cubicle required"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4096:
            return self._send(413, {"ok": False, "error": "too_large",
                                    "message": "body exceeds 4096 bytes"})
        if length:                                       # nobody sends a body today; keep-alive safe
            self.rfile.read(length)
        code, body, retry = dispatch.nudge({"ts": int(time.time()), "actor": "office-dashboard"})
        return self._send(code, body, retry_after=retry)


def main():
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write(f"[office] BUSY BEE office on :{PORT} (db={DB_PATH})\n")
    srv.serve_forever()


if __name__ == "__main__":
    main()
