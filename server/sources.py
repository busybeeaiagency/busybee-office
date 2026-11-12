"""Live data sources for the BUSY BEE office: Mattermost, Beszel, Uptime Kuma, host vitals.

Everything is read-only and optional: a source that has no credentials or is unreachable degrades
to a note instead of breaking the page. Secrets are read from /hermes/creds at runtime and are
never sent to the browser (the page only ever gets the aggregated JSON).
"""
import json
import os
import socket
import ssl
import threading
import time
import urllib.request
from urllib.error import HTTPError, URLError

HOST = os.environ.get("OFFICE_HOST", "10.10.10.202")
MM_BASE = os.environ.get("OFFICE_MM_BASE", f"http://{HOST}:8065")
KUMA_BASE = os.environ.get("OFFICE_KUMA_BASE", f"http://{HOST}:3006")
BESZEL_BASE = os.environ.get("OFFICE_BESZEL_BASE", f"http://{HOST}:8090")
HERMES_DIR = os.environ.get("OFFICE_HERMES_DIR", "/hermes")
CREDS_FILE = os.environ.get("OFFICE_CREDS", f"{HERMES_DIR}/creds/mattermost.json")
BESZEL_CREDS = os.environ.get("OFFICE_BESZEL_CREDS", f"{HERMES_DIR}/creds/beszel.json")
MM_BOT = os.environ.get("OFFICE_MM_BOT", "hermes")

PROBES = [
    ("Mattermost", f"{MM_BASE}/api/v4/system/ping", 4),
    ("Beszel", f"{BESZEL_BASE}/api/health", 4),
    ("Uptime Kuma", f"{KUMA_BASE}/api/entry-page", 4),
    ("Gitea", f"http://{HOST}:3001/api/healthz", 4),
    ("LiteLLM", f"http://{HOST}:4000/health/readiness", 4),
    ("Hindsight", f"http://{HOST}:28888/health", 4),
    ("Hermes UI", f"http://{HOST}:9119/", 4),
    ("Portainer", f"https://{HOST}:9443/", 4),
    ("Postgres", None, 5432),
    ("RabbitMQ", f"http://{HOST}:15672/", 4),
    ("Office", "http://127.0.0.1:9120/healthz", 4),
]


def _get_json(url, timeout=6, token=None, data=None, headers=None):
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else None


def _mm_login(user, password):
    """Mattermost hands the session token back in the Token response header, not the body."""
    req = urllib.request.Request(f"{MM_BASE}/api/v4/users/login",
                                 data=json.dumps({"login_id": user, "password": password}).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
        return r.headers.get("Token")


def _read_creds():
    """Mattermost bot credentials from the homelab credential store (never printed)."""
    user, password = os.environ.get("OFFICE_MM_USER"), os.environ.get("OFFICE_MM_PASSWORD")
    if user and password:
        return user, password
    try:
        blob = json.load(open(CREDS_FILE))
        entry = (blob.get("users") or {}).get(MM_BOT) or {}
        # the store holds the login as "email"; older files used "username"
        return entry.get("username") or entry.get("email"), entry.get("password")
    except Exception:
        return None, None


def _read_beszel_creds():
    user, password = os.environ.get("OFFICE_BESZEL_USER"), os.environ.get("OFFICE_BESZEL_PASSWORD")
    if user and password:
        return user, password
    for path in (BESZEL_CREDS, f"{HERMES_DIR}/creds/office.json"):
        try:
            blob = json.load(open(path))
            entry = blob.get("beszel") if isinstance(blob.get("beszel"), dict) else blob
            if entry.get("user") and entry.get("password"):
                return entry["user"], entry["password"]
        except Exception:
            continue
    return None, None


class Sources:
    """Cached, thread-safe access to every live source."""

    def __init__(self):
        self._lock = threading.Lock()
        self._vitals = {"at": 0, "data": None}
        self._chat = {"at": 0, "presence": {}, "posts": [], "note": ""}
        self._mm_token = None
        self._mm_token_at = 0.0
        self._beszel_token = None
        self._beszel_token_at = 0.0
        self._prev_cpu = None
        self._offtopic_id = None
        self._user_names = {}

    # ---------- mattermost ----------
    def mm_token(self):
        if self._mm_token and time.time() - self._mm_token_at < 12 * 3600:
            return self._mm_token
        user, password = _read_creds()
        if not user:
            return None
        try:
            token = _mm_login(user, password)
            self._mm_token, self._mm_token_at = token, time.time()
        except Exception:
            self._mm_token = None
        return self._mm_token

    def presence(self):
        token = self.mm_token()
        if not token:
            return {}, "no Mattermost credentials"
        try:
            users = json.load(open(CREDS_FILE)).get("users") or {}
            ids = [u.get("user_id") for u in users.values() if u.get("user_id")]
            out = _get_json(f"{MM_BASE}/api/v4/users/status/ids",
                            data=json.dumps(ids).encode(), token=token, timeout=8)
            # the credentials file already maps username -> user_id, so no second lookup is needed
            by_id = {str(u.get("user_id")): u.get("status", "offline") for u in (out or [])}
            return {name: by_id.get(str(u.get("user_id")), "offline")
                    for name, u in users.items() if u.get("user_id")}, ""
        except Exception as e:
            return {}, f"presence failed: {type(e).__name__}"

    def _resolve_users(self, ids):
        token = self.mm_token()
        todo = [i for i in ids if i and i not in self._user_names]
        if not todo:
            return
        try:
            out = _get_json(f"{MM_BASE}/api/v4/users/ids", data=json.dumps(todo).encode(),
                            token=token, timeout=8)
            for u in out or []:
                self._user_names[u.get("id")] = u.get("username") or u.get("id")
        except Exception:
            pass

    def chatter(self, since_ms=None):
        """Latest #off-topic posts, at most one upstream request per minute."""
        with self._lock:
            if time.time() - self._chat["at"] < 60 and self._chat["posts"]:
                return self._chat
        token = self.mm_token()
        if not token:
            return {"presence": {}, "posts": [], "note": "no Mattermost credentials"}
        try:
            if not self._offtopic_id:
                blob = json.load(open(CREDS_FILE))
                team, chan = blob.get("team", {}).get("name"), (blob.get("channels") or {}).get("off-topic")
                if chan:
                    self._offtopic_id = chan
                else:
                    out = _get_json(f"{MM_BASE}/api/v4/teams/name/{blob['team']['name']}/channels/name/off-topic",
                                    token=token, timeout=8)
                    self._offtopic_id = (out or {}).get("id")
            if not self._offtopic_id:
                return {"presence": {}, "posts": [], "note": "off-topic channel not found"}
            # The latest chatter, not "the last hour": an idle office has quiet hours.
            since = since_ms or 0
            out = _get_json(f"{MM_BASE}/api/v4/channels/{self._offtopic_id}/posts?since={since}&per_page=30",
                            token=token, timeout=8)
            posts = []
            for pid in reversed(out.get("order") or []):
                p = (out.get("posts") or {}).get(pid) or {}
                if p.get("delete_at") or p.get("type"):      # skip joins/adds and other system posts
                    continue
                msg = (p.get("message") or "").strip()
                if not msg or "joined the channel" in msg or "added to the channel" in msg:
                    continue
                posts.append({"user_id": p.get("user_id"), "message": msg[:200],
                              "at": p.get("create_at", 0)})
            ids = sorted({p["user_id"] for p in posts if p["user_id"]})
            self._resolve_users(ids)
            for p in posts:
                p["user"] = self._user_names.get(p["user_id"], "someone")
            presence, note = self.presence()
            out = {"presence": presence, "posts": posts[-8:], "note": note}
            with self._lock:
                self._chat = {**out, "at": time.time()}
            return out
        except Exception as e:
            return {"presence": {}, "posts": [], "note": f"chatter failed: {type(e).__name__}"}

    # ---------- beszel ----------
    def beszel(self):
        token = None
        if self._beszel_token and time.time() - self._beszel_token_at < 20 * 3600:
            token = self._beszel_token
        user, password = _read_beszel_creds()
        if not user:
            return {"systems": [], "note": "no Beszel credentials (set /hermes/creds/beszel.json)"}
        if not token:
            try:
                out = _get_json(f"{BESZEL_BASE}/api/collections/users/auth-with-password",
                                data=json.dumps({"identity": user, "password": password}).encode(), timeout=8)
                token = out["token"]
                self._beszel_token, self._beszel_token_at = token, time.time()
            except Exception as e:
                return {"systems": [], "note": f"Beszel auth failed: {type(e).__name__}"}
        try:
            out = _get_json(f"{BESZEL_BASE}/api/collections/systems/records?per_page=50&sort=-created",
                            token=token, timeout=8)
            systems = []
            for it in (out or {}).get("items") or []:
                info = it.get("info")
                try:
                    info = json.loads(info) if isinstance(info, str) and info else (info or {})
                except Exception:
                    info = {}
                systems.append({
                    "name": it.get("name") or it.get("host"),
                    "status": it.get("status"),
                    "cpu": info.get("cpu"), "mem": info.get("mem"), "disk": info.get("disk"),
                    "uptime": info.get("uptime"),
                })
            return {"systems": systems, "note": ""}
        except Exception as e:
            self._beszel_token = None
            return {"systems": [], "note": f"Beszel read failed: {type(e).__name__}"}

    # ---------- uptime kuma ----------
    def kuma(self):
        slug, token = os.environ.get("OFFICE_KUMA_SLUG"), os.environ.get("OFFICE_KUMA_TOKEN")
        if not slug:
            return {"monitors": [], "note": "no status page slug (Kuma publishes /api/status-page/<slug>)"}
        try:
            out = _get_json(f"{KUMA_BASE}/api/status-page/{slug}", token=token, timeout=8)
            monitors = [{"name": m.get("name"), "status": m.get("active")}
                        for m in (out.get("monitorList") or [])]
            return {"monitors": monitors, "note": ""}
        except HTTPError as e:
            if e.code == 404:
                return {"monitors": [], "note": "status page not published on this Kuma instance"}
            return {"monitors": [], "note": f"Kuma {e.code}"}
        except Exception as e:
            return {"monitors": [], "note": f"Kuma unreachable: {type(e).__name__}"}

    # ---------- host + probes ----------
    def host_stats(self):
        cpu = mem = disk = None
        try:
            with open("/proc/stat") as f:
                parts = f.readline().split()[1:]
            total = sum(int(x) for x in parts)
            idle = int(parts[3])
            now = (total, idle)
            if self._prev_cpu:
                dt, di = total - self._prev_cpu[0], idle - self._prev_cpu[1]
                if dt > 0:
                    cpu = round(max(0, min(100, (1 - di / dt) * 100)))
            self._prev_cpu = now
        except Exception:
            pass
        try:
            info = {}
            for line in open("/proc/meminfo"):
                k, _, v = line.partition(":")
                info[k] = int(v.strip().split()[0])
            mem = round(100 * (1 - info.get("MemAvailable", 0) / max(1, info.get("MemTotal", 1))))
        except Exception:
            pass
        try:
            st = os.statvfs(HERMES_DIR)
            disk = round(100 * (st.f_blocks - st.f_bfree) / max(1, st.f_blocks))
        except Exception:
            pass
        return {"cpu": cpu, "mem": mem, "disk": disk, "load": os.getloadavg()[0] if hasattr(os, "getloadavg") else None}

    def probes(self):
        out = []
        for name, url, port in PROBES:
            t0 = time.time()
            ok, note = False, ""
            if url:
                try:
                    req = urllib.request.Request(url, method="GET")
                    with urllib.request.urlopen(req, timeout=4, context=ssl._create_unverified_context()) as r:
                        ok = 200 <= r.status < 400
                except HTTPError as e:
                    ok, note = False, str(e.code)
                except Exception as e:
                    ok, note = False, type(e).__name__
            else:
                try:
                    with socket.create_connection((HOST, port), timeout=3):
                        ok = True
                except Exception as e:
                    ok, note = False, type(e).__name__
            out.append({"name": name, "up": ok, "ms": int((time.time() - t0) * 1000), "note": note})
        return out

    def vitals(self):
        with self._lock:
            if self._vitals["data"] and time.time() - self._vitals["at"] < 15:
                return self._vitals["data"]
        data = {
            "at": int(time.time()),
            "host": self.host_stats(),
            "services": self.probes(),
            "beszel": self.beszel(),
            "kuma": self.kuma(),
        }
        with self._lock:
            self._vitals = {"at": time.time(), "data": data}
        return data
