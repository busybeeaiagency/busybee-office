"""Test fixture: a stand-in for the canonical approval_queue module (SPEC-1 API).

The real funnel lives on the Hermes side (`/opt/data/scripts/approval_queue.py`) and is NOT part of
this repo, so the dashboard's tests load this file through `server/approval_queue.py` instead:
same function names and signatures, same files on disk (`pending.json`, `review_gate_state.json`,
`decisions_overlay.jsonl`, `prune_log.jsonl`), same call-time path resolution from
`APPROVAL_STATE_DIR` / `APPROVAL_GATE_STATE` / `APPROVAL_KANBAN_DB`. It keeps the two rules the
dashboard actually depends on — flock-then-tmp+`os.replace` so a racing writer cannot lose an entry,
and append+fsync for the decision rows — so a change to the real module's contract shows up here as a
failing test rather than as drift in production.
"""
import fcntl
import json
import os
import sqlite3
import tempfile
import time

DECISIONS = ("approve", "reject", "restart", "dismiss", "decision")


# ---------- paths: resolved at CALL time, never at import time ----------
def state_dir():
    return os.environ.get("APPROVAL_STATE_DIR") or "/opt/data/runtime/approvals"


def pending_path():
    return os.path.join(state_dir(), "pending.json")


def prune_log_path():
    return os.path.join(state_dir(), "prune_log.jsonl")


def overlay_path():
    return os.path.join(state_dir(), "decisions_overlay.jsonl")


def decisions_path():
    return os.path.join(state_dir(), "decisions.jsonl")


def gate_state_path():
    return os.environ.get("APPROVAL_GATE_STATE") or os.path.join(state_dir(),
                                                                 "review_gate_state.json")


def kanban_db():
    return os.environ.get("APPROVAL_KANBAN_DB") or "/opt/data/kanban.db"


# ---------- primitives ----------
class _locked:
    """`with _locked(path):` — exclusive flock on a sibling `<path>.lock` around a read-modify-write."""

    def __init__(self, path):
        self.path = path + ".lock"
        self.handle = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.handle = open(self.path, "a+")
        fcntl.flock(self.handle, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
        finally:
            self.handle.close()


def _read_json(path, default):
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return default
    return doc if isinstance(doc, dict) else default


def _write_json(path, doc):
    """tmp + os.replace in the same dir: a reader never sees a half-written queue."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".aq-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _append(path, record):
    """One JSON object per line, flushed and fsynced — these files are the audit trail."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return path


def _statuses(db):
    """task_id -> status from the kanban DB, READ-ONLY; {} when the DB is missing or unreadable."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            return {r[0]: r[1] for r in con.execute("select id, status from tasks")}
        finally:
            con.close()
    except sqlite3.Error:
        return {}


# ---------- the queue ----------
def load_pending():
    with _locked(pending_path()):
        return _read_json(pending_path(), {})


def register(task_id, label, channel="coding", action="review_decision", surface="cron"):
    """EXACTLY ONCE: re-registering the same (task, action) changes nothing."""
    try:
        with _locked(pending_path()):
            doc = _read_json(pending_path(), {})
            old = doc.get(task_id)
            if isinstance(old, dict) and old.get("action") == action:
                return {"ok": True, "changed": False, "entry": old}
            entry = {"label": str(label)[:200], "channel": channel, "action": action,
                     "surface": surface, "since": int(time.time())}
            doc[task_id] = entry
            _write_json(pending_path(), doc)
        return {"ok": True, "changed": True, "entry": entry}
    except OSError as e:
        return {"ok": False, "changed": False,
                "error": "%s: %s" % (type(e).__name__, e)}


def prune(task_id, reason, surface="cron"):
    try:
        with _locked(pending_path()):
            doc = _read_json(pending_path(), {})
            removed = doc.pop(task_id, None)
            if removed is not None:
                _write_json(pending_path(), doc)
        if removed is not None:
            _append(prune_log_path(), {"ts": int(time.time()), "task_id": task_id,
                                       "reason": reason, "surface": surface, "entry": removed})
        return {"ok": True, "removed": removed, "task_id": task_id}
    except OSError as e:
        return {"ok": False, "removed": None, "task_id": task_id,
                "error": "%s: %s" % (type(e).__name__, e)}


def append_overlay(record):
    """Normalised overlay row (legacy `action`/`target` keys kept for the rows already on disk)."""
    task_id = str(record.get("task_id") or record.get("target") or "")
    decision = str(record.get("decision") or record.get("action") or "")
    surface = str(record.get("surface") or "cron")
    row = {"ts": int(record.get("ts") or time.time()), "actor": str(record.get("actor") or "bart"),
           "action": decision, "decision": decision, "target": task_id, "task_id": task_id,
           "channel": str(record.get("channel") or "coding"), "surface": surface,
           "source": str(record.get("source") or surface), "detail": str(record.get("detail") or "")}
    try:
        return {"ok": True, "path": _append(overlay_path(), row), "record": row}
    except OSError as e:
        return {"ok": False, "path": overlay_path(), "record": row,
                "error": "%s: %s" % (type(e).__name__, e)}


def decide(task_id, decision, actor="bart", surface="cron", detail="", channel="coding"):
    """THE FUNNEL: record the answer, drop the request, clear the gate's announcement marker."""
    overlay = append_overlay({"task_id": task_id, "decision": decision, "actor": actor,
                              "surface": surface, "detail": detail, "channel": channel})
    pruned = prune(task_id, "%s:%s" % (surface, decision), surface)
    marker = clear_gate_marker(task_id)
    return {"ok": bool(overlay.get("ok")) and bool(pruned.get("ok")), "overlay": overlay,
            "pruned": pruned, "marker": marker, "known": decision in DECISIONS}


def clear_gate_marker(task_id):
    with _locked(gate_state_path()):
        st = _read_json(gate_state_path(), {})
        posted = st.get("posted")
        if not isinstance(posted, dict) or posted.pop(task_id, None) is None:
            return False
        st["posted"] = posted
        _write_json(gate_state_path(), st)
    return True


def gate_state():
    st = dict(_read_json(gate_state_path(), {}))
    st.setdefault("posted", {})
    st.setdefault("decisions_offset", 0)
    st.setdefault("overlay_offset", 0)
    return st


def save_gate_state(st):
    try:
        with _locked(gate_state_path()):
            _write_json(gate_state_path(), st if isinstance(st, dict) else {})
        return {"ok": True, "path": gate_state_path()}
    except OSError as e:
        return {"ok": False, "path": gate_state_path(), "error": "%s: %s" % (type(e).__name__, e)}


def pending_rows(db=None):
    db = db or kanban_db()
    pending = load_pending()
    full = {}
    if pending:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            try:
                full = {r[0]: r for r in con.execute("select id, status, assignee, title from tasks")}
            finally:
                con.close()
        except sqlite3.Error:
            full = {}
    now = int(time.time())
    rows = []
    for tid, entry in pending.items():
        entry = entry if isinstance(entry, dict) else {}
        card = full.get(tid)
        since = int(entry.get("since") or 0)
        rows.append({"task_id": tid, "status": (card[1] if card else None),
                     "assignee": (card[2] if card else None), "title": (card[3] if card else None),
                     "label": entry.get("label"), "action": entry.get("action"), "since": since,
                     "age_s": max(0, now - since) if since else 0})
    rows.sort(key=lambda r: (r["since"], r["task_id"]))           # the longest-waiting first
    return rows


def sweep_stale(db=None, terminal=("done", "archived")):
    db = db or kanban_db()
    if not os.path.isfile(db):
        return []                                                  # missing DB: touch nothing
    statuses = _statuses(db)
    touched = []
    for tid in list(load_pending().keys()):
        status = statuses.get(tid)
        if status is None or status in terminal:
            removed = prune(tid, "stale:%s" % (status or "missing"), "review-gate")["removed"]
            marker = clear_gate_marker(tid)
            touched.append({"task_id": tid, "status": status, "pending_removed": removed is not None,
                            "marker_removed": marker})
    for tid in list(gate_state()["posted"]):
        if statuses.get(tid) != "review":                          # re-announced when it comes back
            touched.append({"task_id": tid, "status": statuses.get(tid), "pending_removed": False,
                            "marker_removed": clear_gate_marker(tid)})
    return touched
