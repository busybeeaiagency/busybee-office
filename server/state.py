"""The dashboard's own ephemeral side-store (NOT the source of truth for tasks).

kanban.db owns the cards; this JSON file only holds what the office needs to make an operator
action safe: single-use confirm tokens, write-rate-limit timestamps, and which crashed cards an
operator has dismissed. It lives next to the DB (a writable path) so the container survives a
restart; when nothing is writable it falls back to a tmp path so writes still succeed rather than
crash. Every write is atomic (`tmp` + `os.replace`) so a kill mid-write can never leave half a
file. Guarded by one module lock so concurrent HTTP threads see one consistent snapshot.
"""
import json
import os
import tempfile
import threading

LOCK = threading.Lock()
DEFAULT_NAME = "busybee-office.json"
EMPTY = {"dismissed": {}, "tokens": {}, "hits": []}


def state_path():
    """Where the side-store lives: OFFICE_STATE, else <db dir>/state/, else tmp."""
    p = os.environ.get("OFFICE_STATE")
    if p:
        return p
    db = os.environ.get("OFFICE_DB_RW") or os.environ.get("OFFICE_DB") or ""
    base = os.path.dirname(db) if db else ""
    if base:
        return os.path.join(base, "state", DEFAULT_NAME)
    return os.path.join(tempfile.gettempdir(), DEFAULT_NAME)


def locked():
    """`with state.locked():` around a load()/save() pair so read-modify-write is atomic."""
    return LOCK


def load():
    try:
        with open(state_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = {}
    for key, empty in EMPTY.items():
        val = data.get(key)
        out[key] = val if isinstance(val, type(empty)) else (type(empty)())
    return out


def save(state):
    """Atomic write, `0o664` so the hermes gateway (uid 10000) can still rewrite it later."""
    targets = [state_path()]
    fb = os.path.join(tempfile.gettempdir(), DEFAULT_NAME)
    if fb not in targets:
        targets.append(fb)
    for target in targets:
        td = os.path.dirname(target) or "."
        tmp = None
        try:
            if td:
                os.makedirs(td, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=td, prefix=".st-", suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, separators=(",", ":"))
            os.chmod(tmp, 0o664)
            os.replace(tmp, target)
            return target
        except OSError:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return None


def record_dismissed(task, info):
    with LOCK:
        s = load()
        s["dismissed"][task] = info
        save(s)


def dismissed():
    """task_id -> {"at", "block_ts", "run_id"} for cards the operator has dismissed."""
    return load()["dismissed"]
