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

A pass that spawns nothing is the common answer, and "nothing to do" used to be a lie: the dashboard
reports a stack of DEFERRALS with it — memory pressure, the host cap, a profile at its limit, a
respawn guard, a lost dispatch lock — and the result line named none of them. `_deferred` and
`_suppression` below carry that half of the answer to the page, in the same words the dispatcher's
own `describe_suppression` uses (hermes_cli/kanban_db_dispatch.py). The buckets are read `.get()`
and defensively: a dashboard that renames one, or sends a dict where a list belongs, reads as empty.
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

# A deferred row may only name a card: the shape is the guard that keeps a value we misread out of
# the page (kanban ids are t_<hex>; actions.TASK_ID_RE is stricter because it also MOVES cards).
TASK_ID_RE = re.compile(r"^t_[0-9a-z_-]{1,64}$", re.IGNORECASE)

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
# Measured against the live dashboard (2026-11-16): auto_blocked / crashed / stale / timed_out are
# LISTS of task ids and spawned is a list of triples, so `_count` sizes lists rather than int()-ing
# them — int() of a non-empty list raises TypeError, which used to mean every one of these counters
# read 0 in production while cards sat there blocked.
COUNTERS = ("reclaimed", "promoted", "auto_blocked", "crashed", "stale", "skipped_locked")
LOG_KEYS = ("spawned_n", "deferred_n") + COUNTERS

# The DEFERRED buckets: ready work the pass saw and held back. All are lists (skipped_host_capped
# is a (total_running, max_in_progress) pair, skipped_locked a flag), and none of them are failures —
# they are the reason the spawn count was 0, which is the half of the answer the page was missing.
DEFERRED_BUCKETS = ("skipped_unassigned", "skipped_nonspawnable", "skipped_per_profile_capped",
                    "respawn_guarded", "rate_limited", "interrupted")
DEFERRED_CAP = 8                   # rows on the answer; deferred_n still counts every one of them
SUPPRESSION_MAX = 180              # a line has to fit on the TV
MESSAGE_MAX = 240
HOST_CAP = "host cap reached (%d/%d in progress)"   # upstream's wording, sorted last of the parts
MEMORY_KEY = "memory_pressure"     # ... then the pressure, same order as describe_suppression
# Bucket -> the word the dispatcher uses for it upstream. "skipped_unassigned" and friends keep
# theirs verbatim; per-profile saturation is the one place upstream says per_profile_capped.
BUCKET_LABELS = {"skipped_per_profile_capped": "per_profile_capped"}


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
    """A counter from a dict we do not fully control: missing or broken reads as 0.

    A LIST of ids is a counter too (auto_blocked, crashed, stale, timed_out are all lists on the
    live dashboard): its length is the count, and `len` on a dict counts its keys, which is wrong
    but never raises. Anything else falls back to int().
    """
    value = answer.get(key, 0)
    if key == "skipped_locked":
        return bool(value)                        # the contract says bool; the page shows a flag
    if isinstance(value, (list, tuple, dict)):
        return len(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _row(value, reason_at):
    """One deferred card as ["<task_id>", "<reason>"], from rows of shape we only partly know.

    `value` is one entry of a bucket: a bare task id, or a (task_id, ...) tuple whose reason sits
    at `reason_at` (None for buckets that have none). A dict entry is a dashboard being generous.
    A malformed entry yields ["", ""], which the caller drops — a reason we cannot read is a reason
    we must not guess at. A bare task id may be short and plain ("t_a"); it still names a card.
    """
    task = reason = ""
    if isinstance(value, (list, tuple)):
        if len(value) > 0 and value[0] is not None:
            task = str(value[0])[:64]
        if reason_at is not None and len(value) > reason_at and value[reason_at] is not None:
            reason = str(value[reason_at])[:64]
    elif isinstance(value, dict):
        task = str(value.get("task") or value.get("task_id") or value.get("id") or "")[:64]
        reason = str(value.get("reason") or value.get("why") or "")[:64]
    elif value not in (None, ""):
        task = str(value)[:64]                    # a bare task id, which most buckets are
    return task, (reason if task else "")


def _deferred(answer):
    """(rows, n): the cards this pass held back, and how many there really were.

    `rows` is capped at DEFERRED_CAP so the page stays a page; `n` counts every bucket in full,
    including the host cap (that board is saturated as a whole, which is one reason, not a list of
    card ids). Malformed or credential-shaped junk cannot ride along: a row whose task id does not
    look like a task id is dropped, so a dashboard that starts reporting reasons verbatim in a slot
    we read as a task cannot put the dashboard's own sign-in URL — userinfo and maybe a password —
    on the TV.
    """
    rows = []

    def add(value, reason_at):
        task, reason = _row(value, reason_at)
        if TASK_ID_RE.match(task):
            rows.append([task, reason])

    for key in DEFERRED_BUCKETS:
        value = answer.get(key)
        # Where a reason sits, if a reason came with the id at all: respawn_guarded is
        # (task_id, reason) by contract, and a bucket that arrived as a {task_id: reason} dict hands
        # us the same pair through _list_of. Every other shape carries only ids, and a reason we
        # cannot read is left blank rather than invented.
        reason_at = 1 if (key == "respawn_guarded" or isinstance(value, dict)) else None
        for entry in _list_of(value):
            add(entry, reason_at)
    # The two board-wide holds are reasons rather than card ids — the host cap says "this whole
    # board was saturated", memory pressure says "no new workers this tick" — and both count as
    # work held back, because a ready card stayed ready on each of them.
    n = len(rows) + (1 if _host_pair(answer.get("skipped_host_capped")) else 0)
    n += 1 if _pressure(answer) else 0
    return rows[:DEFERRED_CAP], n


def _list_of(value):
    """A bucket as entries, whatever shape it arrived in.

    list/tuple → as-is; dict → its `.items()`, because a task-id-keyed bucket then hands us
    (task_id, reason) pairs, which is exactly the shape `_row` reads; anything else (a string, a
    number, None because the dashboard renamed the key) → nothing at all. That is the whole of the
    defensive read: no bucket can raise, and a shape we do not know reads as "nothing deferred".
    """
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, dict):
        return list(value.items())
    return []


def _host_pair(value):
    """skipped_host_capped as (total_running, max_in_progress), or None when absent or unusable."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        return int(value[0]), int(value[1])
    except (TypeError, ValueError):
        return None


def _pressure(answer):
    """The pressure word the dashboard used, if it named one. Anything else means no pressure."""
    value = answer.get(MEMORY_KEY)
    text = value.strip() if isinstance(value, str) else ""
    return re.sub(r"[^a-z0-9_-]", "", text.lower())[:32]


def _suppression(answer):
    """One honest line naming why the pass held work back, in the dispatcher's own vocabulary.

    `describe_suppression` (hermes_cli/kanban_db_dispatch.py) says: respawn-guard reasons counted
    per task, then the bucket counts, then "host cap reached (8/5 in progress)", then
    "memory_pressure=critical". Same words, same order, ", " between — so a line on the TV can be
    grepped against the dispatcher's own log. "" when the pass deferred nothing.
    """
    counts = {}
    for key in DEFERRED_BUCKETS:
        values = _list_of(answer.get(key))
        if key == "respawn_guarded":
            for value in values:                  # counted by reason, as upstream does it
                reason = re.sub(r"[^a-z0-9_-]", "", str(_row(value, 1)[1]).lower())[:32]
                reason = reason or "guarded"
                counts[reason] = counts.get(reason, 0) + 1
        elif values:
            counts[BUCKET_LABELS.get(key, key)] = len(values)
    if answer.get("skipped_locked"):              # a flag: the lock holder is the progress
        counts["skipped_locked"] = 1
    parts = [f"{key}={counts[key]}" for key in sorted(counts)]
    capped = _host_pair(answer.get("skipped_host_capped"))
    if capped:
        parts.append(HOST_CAP % capped)
    pressure = _pressure(answer)
    if pressure:
        parts.append(f"{MEMORY_KEY}={pressure}")
    return _clip(", ".join(parts), SUPPRESSION_MAX)


def _mask(text, secret):
    """Scrub the dashboard password out of anything the dashboard WROTE BACK.

    A reason is our own vocabulary, so this is belt-and-braces: the credential canary is a rule
    about response bodies and log lines (AGENTS.md rule 1), and the only writer of a free-text
    reason is the dashboard, whose own config knows that password. "" secret means nothing to do.
    """
    if secret and len(secret) >= 4 and secret in text:
        return text.replace(secret, "***")
    return text


def _clip(text, limit):
    """Shorten for a status line, on a separator when one is in reach, so a clause survives whole."""
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    for sep in (", ", " · "):
        if sep in cut:
            return cut[:cut.rfind(sep)]
    return cut + "…"


def _summary(answer, dry_run, secret=""):
    """DispatchResult -> the small honest shape the page and the actions log both read.

    `secret` is the dashboard password, passed in only so a reason string that happened to echo it
    (a dashboard pasting its own sign-in url into a note field) can be masked before it reaches a
    TV screen or a log line. Nothing else about it leaves this function.
    """
    spawned = [{"task": str(pair[0]), "assignee": str(pair[1] or "")}
               for pair in (answer.get("spawned") or [])
               if isinstance(pair, (list, tuple)) and len(pair) >= 2]
    out = {"ok": True, "dry_run": bool(dry_run), "at": int(time.time()),
           "spawned": spawned, "spawned_n": len(spawned)}
    for key in COUNTERS:
        out[key] = _count(answer, key)
    deferred, deferred_n = _deferred(answer)
    out["deferred"] = [[task, _mask(reason, secret)] for task, reason in deferred]
    out["deferred_n"] = deferred_n
    out["suppression"] = _mask(_suppression(answer), secret)
    if out["spawned_n"] or any(out[key] for key in COUNTERS):
        head = (f"spawned {out['spawned_n']} · reclaimed {out['reclaimed']} · "
                f"promoted {out['promoted']}")
    else:
        head = "nothing to do"                    # the honest answer most of the time
    if out["suppression"]:                        # ... but HERE is why nothing was spawned
        head += " · deferred: " + out["suppression"]
    out["message"] = _clip(head, MESSAGE_MAX)
    return out


def _err(code, key, message):
    return code, {"ok": False, "error": key, "message": message}


def _attempt(target, dry_run):
    """Login-if-needed plus one pass; a 401 buys exactly one re-login and one retry. (code, body)."""
    base, secret = target[0], target[2]
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
    return 200, _summary(answer, dry_run, secret=secret)


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
        rec["deferred"] = [row[0] for row in body.get("deferred") or []]   # ids, as with spawned
        rec["suppression"] = body.get("suppression")   # our own words; auditable after the fact
    else:
        rec["error"] = body.get("error")
    actions.log_action(rec)
