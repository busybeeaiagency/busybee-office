"""Give the dispatcher a nudge from the dashboard, over the Hermes dashboard's own API.

The orchestrator's "Nudge dispatcher" button runs one dispatcher pass: reclaim dead claims, promote
ready work, spawn a worker per task that has one. The office wants that same pass pressable from the
TV dashboard — an agent sitting `ready` with no worker is invisible downtime.

Why call the dashboard instead of touching kanban.db: our rule is that the board is read here and
written only by `server/actions.py`, and a dispatcher pass is a dozen coordinated writes. Re-issuing
them by hand would race the orchestrator, so the nudge goes over the same route the button uses.

That route sits behind the dashboard's sign-in gate, so this keeps one in-process session (cookie jar
+ opener), logs in with the server-side creds file, and on a 401 re-logs-in once and retries once.
The password never leaves this process: not in a response body, not in an actions-log line.

One real pass per MIN_GAP_S — the page is polled from several places at once and a double spawn is
worse than a missed nudge. The GET preview (`dry_run`) spawns nothing, so it is not rate limited.
"""
import http.cookiejar
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from . import actions

MIN_GAP_S = 15                     # one real pass per 15 s (the dry_run preview is exempt)
LOGIN_TIMEOUT_S = 10
DISPATCH_TIMEOUT_S = 20
DEFAULT_BASE = "http://10.10.10.202:9119"
DEFAULT_MAX = 8
DRY_RUN_PATH = "/api/office/dispatch"
DISPATCH_PATH = "/api/plugins/kanban/dispatch"
LOGIN_PATH = "/auth/password-login"
USERINFO_RE = re.compile(r"//[^/@]*@")

_lock = threading.Lock()
_last = {"at": 0.0}                # monotonic ts of the last REAL pass; process-wide, not persisted
_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))

# The DispatchResult keys worth carrying to the page and into the actions log. Read with .get(): a
# dashboard that renames a counter must degrade to 0, never to a traceback on a TV screen.
COUNTERS = ("reclaimed", "promoted", "auto_blocked", "crashed", "stale", "skipped_locked")
LOG_KEYS = ("spawned_n",) + COUNTERS


def _paths():
    """Resolved at CALL time (like actions._paths) so tests can point the creds file elsewhere."""
    hermes = os.environ.get("OFFICE_HERMES_DIR", "/hermes")
    return {"creds": os.environ.get("OFFICE_DASHBOARD_CREDS") or f"{hermes}/creds/dashboard.json"}


def _public_base(base):
    """The base url as it may be shown: userinfo stripped, so it can never carry a password."""
    return USERINFO_RE.sub("//", (base or "").rstrip("/"))


def _env_base():
    """OFFICE_DASHBOARD_URL, already credential-free; "" when unset."""
    return _public_base(os.environ.get("OFFICE_DASHBOARD_URL"))


def _blob():
    """The creds file as a dict, or None when it is missing, unreadable or not a JSON object."""
    try:
        with open(_paths()["creds"]) as fh:               # a creds file we hold open would be rude
            blob = json.load(fh)
    except Exception:                                    # noqa: BLE001 — a creds gap is a 503, not a 500
        return None
    return blob if isinstance(blob, dict) else None


def _base_for(blob):
    """Base url precedence: OFFICE_DASHBOARD_URL, then the creds file's "url", then the default."""
    return _env_base() or _public_base((blob or {}).get("url")) or DEFAULT_BASE


def _target():
    """(base, username, password, provider) to call with, or None when the file cannot say."""
    blob = _blob()
    if not blob or not blob.get("username") or not blob.get("password"):
        return None
    return _base_for(blob), blob["username"], blob["password"], blob.get("provider") or "basic"


def capability():
    """Is the nudge available from here? The extra key on GET /api/office/actions.

    Available means the credentials are readable here — the dashboard itself is only found out by
    trying, and a dark button with a reason beats a red error once somebody presses it.
    """
    blob = _blob()
    return {"available": blob is not None,
            "reason": "" if blob is not None else f"dashboard creds not readable at {_paths()['creds']}",
            "base": _base_for(blob), "dry_run_path": DRY_RUN_PATH}


def gate(now=None):
    """Seconds until the next real pass may run; 0 (and this call takes the slot) when it may run now.

    Keyed to the whole queue rather than to one card — one nudge spawns everybody's work — so
    actions.check_rate (per task id) is the wrong tool here. The slot is taken up front, before the
    call, because a double-click that spawned twice would be worse than a nudge that was refused.
    """
    now = now if now is not None else time.monotonic()
    with _lock:
        since = now - _last["at"]
        if _last["at"] and since < MIN_GAP_S:
            return max(1, int(MIN_GAP_S - since) + 1)
        _last["at"] = now
        return 0


def reset():
    """Forget the cached session and the rate-limit slot (tests; or after the credentials change)."""
    _jar.clear()
    with _lock:
        _last["at"] = 0.0


# ---------- the calls themselves ----------

def _request(url, payload, timeout):
    """POST JSON with the shared cookie jar; returns (status, parsed body or raw text)."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    with _opener.open(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return r.status, (json.loads(raw) if raw.strip() else None)
    except ValueError:
        return r.status, raw


def _have_session():
    return any(c.name.startswith("hermes_session") for c in _jar)


def _login(target):
    """Password login: the session cookies land in the jar. Nothing about the answer is echoed."""
    base, user, password, provider = target
    try:
        status, answer = _request(f"{base}{LOGIN_PATH}",
                                  {"provider": provider, "username": user, "password": password},
                                  LOGIN_TIMEOUT_S)
    except urllib.error.HTTPError as e:            # a refused login is a credential problem, not a 500
        raise actions.ActionError("dashboard_unreachable", 502,
                                  f"dashboard refused these credentials (HTTP {e.code})") from None
    if status != 200 or (isinstance(answer, dict) and answer.get("ok") is False):
        raise actions.ActionError("dashboard_unreachable", 502,
                                  f"dashboard refused these credentials (HTTP {status})")


def _max_n():
    try:
        return max(1, int(os.environ.get("OFFICE_DISPATCH_MAX") or DEFAULT_MAX))
    except ValueError:
        return DEFAULT_MAX


def _dispatch(base, dry_run):
    """One pass. Returns the answer dict, or None when the dashboard says our session went stale."""
    flag = "true" if dry_run else "false"
    try:
        status, answer = _request(f"{base}{DISPATCH_PATH}?dry_run={flag}&max={_max_n()}",
                                  {}, DISPATCH_TIMEOUT_S)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        return None
    if status != 200:
        raise actions.ActionError("bad_dashboard_answer", 502, f"dashboard answered HTTP {status}")
    if not isinstance(answer, dict):
        raise actions.ActionError("bad_dashboard_answer", 502, "dashboard answer is not an object")
    return answer


def _count(answer, key):
    """A counter from a dict we do not fully control: missing or broken reads as 0."""
    value = answer.get(key, 0)
    if key == "skipped_locked":
        return bool(value)                        # the contract says bool; the page shows a flag
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _summary(answer, dry_run):
    """DispatchResult -> the small honest shape the page and the actions log both read."""
    spawned = [{"task": str(pair[0]), "assignee": str(pair[1] or "")}
               for pair in (answer.get("spawned") or [])
               if isinstance(pair, (list, tuple)) and len(pair) >= 2]
    out = {"ok": True, "dry_run": bool(dry_run), "at": int(time.time()),
           "spawned": spawned, "spawned_n": len(spawned)}
    for key in COUNTERS:
        out[key] = _count(answer, key)
    if out["spawned_n"] or any(out[key] for key in COUNTERS):
        out["message"] = (f"spawned {out['spawned_n']} · reclaimed {out['reclaimed']} · "
                          f"promoted {out['promoted']}")
    else:
        out["message"] = "nothing to do"          # the honest answer most of the time
    return out


def _err(code, key, message):
    return code, {"ok": False, "error": key, "message": message}


def _attempt(target, dry_run):
    """Login-if-needed plus one pass; a 401 buys exactly one re-login and one retry. (code, body)."""
    base = target[0]
    try:
        if not _have_session():
            _login(target)
        answer = _dispatch(base, dry_run)
        if answer is None:                                   # the session aged out mid-flight
            _login(target)                                   # exactly one re-login
            answer = _dispatch(base, dry_run)                # ... and exactly one retry
        if answer is None:
            return _err(502, "bad_dashboard_answer", "dashboard kept refusing the session")
    except actions.ActionError as e:                         # refused creds / unusable answer
        return e.status, {"ok": False, "error": e.code, "message": e.message}
    except urllib.error.HTTPError as e:                      # the pass itself answered badly
        return _err(502, "bad_dashboard_answer", f"dashboard answered HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return _err(502, "dashboard_unreachable", f"{type(e).__name__}: {getattr(e, 'reason', e)}")
    except ValueError:
        return _err(502, "bad_dashboard_answer", "dashboard answer is not JSON")
    return 200, _summary(answer, dry_run)


def nudge(rec, dry_run=False):
    """One dispatcher pass, plus the one actions-log line that proves it happened.

    Fills the caller's `rec` — the counters on a success, the error key otherwise — appends exactly
    one line to OFFICE_ACTIONS_LOG through actions.log_action on every exit including a denial, and
    returns the (status, body, retry_after) the route answers with. Without usable credentials we
    never so much as open a socket. The dry_run preview shares the path and skips the limiter.
    """
    rec.update({"action": "dispatch", "task": "queue", "agent": None, "dry_run": bool(dry_run)})
    target = _target()
    retry = 0
    if not target:
        code, body = _err(503, "creds_missing",
                          f"no usable dashboard credentials at {_paths()['creds']}")
    elif dry_run:
        code, body = _attempt(target, True)                  # a preview spawns nothing: no limiter
    else:
        retry = gate()
        if retry:
            code, body = _err(429, "rate_limited",
                              f"one dispatch pass per {MIN_GAP_S}s; retry in {retry}s")
        else:
            code, body = _attempt(target, False)
    _log(rec, body)
    return code, body, (retry or None)


def _log(rec, body):
    """The single exit: the log line carries the same truth the page got, and never the password."""
    rec["ok"] = bool(body.get("ok"))
    if rec["ok"]:
        rec.update({key: body.get(key) for key in LOG_KEYS})
        rec["spawned"] = [card["task"] for card in body.get("spawned") or []]
    else:
        rec["error"] = body.get("error")
    actions.log_action(rec)
