"""Operator actions on kanban cards: restart / approve / dismiss — direct DB writes.

The hermes CLI is NOT installed in the office container, so these mirror the exact writes from
`/opt/hermes/hermes_cli/kanban_db.py` by hand: `unblock_task` (:3557) for restart/approve,
`add_comment` (:1671) for the approve note, `_end_run`/`_reclaim_dangling_run` (:1857/:3527) for
closing a leaked run, `_resume_status_from_events` (:2018) for where a card resumes, and
`_parents_satisfied` (:2100) for the ready-vs-todo landing. The hermes CLI would do the same thing
on the next dispatch tick; doing it here is what makes the badge flip in the *next poll* instead of
whenever the dispatcher next wakes.

Every write runs in ONE `BEGIN IMMEDIATE` transaction with `busy_timeout=5000`, an `isolation_level`
of None (we own the transaction), and a `WHERE` guard on the status we expect — so a write is
re-run-safe and returns 409 (`not_in_expected_state`) when someone else already moved the card,
rather than blindly overwriting it. A write never happens without a server-issued confirm token
(`server/state.py`) and without passing the rate limit, both checked in `server/office.py` before
`perform()`; the error codes are shared here so the whole write path speaks one vocabulary.
"""
import json
import os
import re
import sqlite3
import time

from . import state

# The three operator actions and how the UI labels/explains each one.
ACTIONS = {
    "restart": {"label": "Restart task",
                "why": "unblock the card so the dispatcher retries it"},
    "approve": {"label": "Approve",
                "why": "leave a note and unblock the card so it runs again"},
    "dismiss": {"label": "Dismiss",
                "why": "close the crashed run and clear the badge"},
}

TASK_ID_RE = re.compile(r"^t_[0-9a-f]{6,}$", re.IGNORECASE)

TOKEN_TTL_S = 120
PER_TASK_S = 10              # at most one write to a given card every 10 s
GLOBAL_WINDOW_S = 60         # ... and at most 20 writes per minute across the whole dashboard
GLOBAL_MAX = 20


class ActionError(Exception):
    """A refused action. `code` is the machine key, `status` the HTTP code the route answers with."""
    def __init__(self, code, status, message):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


def _paths():
    """Resolve paths at CALL time (never import time) so tests can point them at temp files."""
    hermes = os.environ.get("OFFICE_HERMES_DIR", "/hermes")
    return {
        "db": os.environ.get("OFFICE_DB_RW") or os.environ.get("OFFICE_DB") or f"{hermes}/kanban.db",
        "state": state.state_path(),
        "log": os.environ.get("OFFICE_ACTIONS_LOG"),
    }


def _connect():
    """A fresh RW connection per call — never held across an HTTP request (kanban.db is WAL)."""
    con = sqlite3.connect(_paths()["db"], timeout=5, isolation_level=None)
    con.execute("pragma busy_timeout=5000")
    con.row_factory = sqlite3.Row
    return con


def capability():
    """Can this process write? The UI hides the buttons when it can't (reason says why)."""
    allow = os.environ.get("OFFICE_ALLOW_WRITES", "").strip().lower() in ("1", "true", "yes", "on")
    db = _paths()["db"]
    if not allow:
        return {"writable": False, "reason": "writes disabled (OFFICE_ALLOW_WRITES unset)", "actions": []}
    if not db or not os.path.exists(db):
        return {"writable": False, "reason": "kanban.db not found on a writable path", "actions": []}
    if not os.access(db, os.W_OK):
        return {"writable": False, "reason": "kanban.db is read-only to this process", "actions": []}
    return {"writable": True, "reason": None, "actions": sorted(ACTIONS)}


def issue_token(action, task, now=None):
    """Hand the page a one-shot token for exactly (action, task); TTL 120 s."""
    now = now or time.time()
    token = "ct_" + os.urandom(12).hex()
    with state.locked():
        s = state.load()
        s["tokens"][f"{action}|{task}"] = {"token": token, "exp": now + TOKEN_TTL_S, "used": False}
        state.save(s)
    return token


def consume_token(action, task, token, now=None):
    """Burn the token. (True, "") on success; else (False, reason) keyed as the error codes."""
    now = now or time.time()
    with state.locked():
        s = state.load()
        rec = s["tokens"].get(f"{action}|{task}")
        if not rec:
            return False, "no_token"
        if not token or token != rec.get("token"):
            return False, "bad_token"
        if now > float(rec.get("exp") or 0):
            return False, "expired_token"
        if rec.get("used"):
            return False, "bad_token"                      # the same token, a second time
        rec["used"] = True
        state.save(s)
    return True, ""


def check_rate(action, task, now=None):
    """One write per card / 10 s, 20 writes / 60 s overall. (True, 0) or (False, retry_after_s).

    Read-only on purpose: a refused request (bad token, wrong card state) must not eat the slot,
    otherwise one mis-click locks the operator out of the very card they are trying to fix.
    The hit is stored by record_hit(), which the route calls only when it is really about to write.
    """
    now = now or time.time()
    with state.locked():
        s = state.load()
        hits = [h for h in (s.get("hits") or []) if isinstance(h, list) and h[0] > now - GLOBAL_WINDOW_S]
        per_task = [h[0] for h in hits if len(h) > 2 and h[2] == task]
        if per_task:
            since = now - max(per_task)
            if since < PER_TASK_S:
                s["hits"] = hits
                state.save(s)
                return False, max(1, int(PER_TASK_S - since) + 1)
        if len(hits) >= GLOBAL_MAX:
            retry = int(GLOBAL_WINDOW_S - (now - min(h[0] for h in hits))) + 1
            s["hits"] = hits
            state.save(s)
            return False, max(1, retry)
    return True, 0


def record_hit(action, task, now=None):
    """Count a write attempt against the limiter (call it right before perform())."""
    now = now or time.time()
    with state.locked():
        s = state.load()
        hits = [h for h in (s.get("hits") or []) if isinstance(h, list) and h[0] > now - GLOBAL_WINDOW_S]
        hits.append([now, action, task])
        s["hits"] = hits
        state.save(s)


def performed_count():
    with state.locked():
        return len(state.load().get("hits") or [])


def _json_or_empty(payload):
    if isinstance(payload, dict):
        return payload
    try:
        out = json.loads(payload) if payload else {}
    except (ValueError, TypeError):
        out = {}
    return out if isinstance(out, dict) else {}


def _add_event(con, task, kind, now, run_id=None, payload=None):
    """Append one `task_events` row. The live schema carries `run_id` (which attempt this
    belongs to) and `payload` (why); an old DB without them still gets the event."""
    try:
        con.execute("insert into task_events (task_id, run_id, kind, payload, created_at)"
                    " values (?, ?, ?, ?, ?)",
                    (task, run_id, kind,
                     json.dumps(payload, ensure_ascii=False) if payload else None, int(now)))
    except sqlite3.OperationalError:
        con.execute("insert into task_events (task_id, kind, created_at) values (?, ?, ?)",
                    (task, kind, int(now)))


def _parents_satisfied(con, task):
    """`_parents_satisfied` (:2100): every parent must be done|archived. No parents -> satisfied.
    `task_links` is (parent_id, child_id) — the card being unblocked is the CHILD."""
    rows = con.execute(
        "select p.status as s from task_links l join tasks p on p.id = l.parent_id"
        " where l.child_id = ?",
        (task,)
    ).fetchall()
    return all(r["s"] in ("done", "archived") for r in rows)


# Upstream counts a card as resuming in `review` only when the newest lifecycle event says so.
_LIFECYCLE_KINDS = ("blocked", "block_loop_detected", "dependency_wait", "gave_up", "unblocked",
                    "changes_requested", "review_reopened", "status", "reclaimed", "stale",
                    "timed_out", "crashed", "spawn_failed", "rate_limited")


def _resume_status(con, task):
    """`_resume_status_from_events` (:2018): 'review' only when the newest lifecycle event's payload
    says `resume_status`/`retry_status`/`source_status` == review; otherwise None (ready/todo)."""
    try:
        row = con.execute(
            "select payload from task_events where task_id = ? and kind in ("
            + ",".join("?" for _ in _LIFECYCLE_KINDS) + ") order by id desc limit 1",
            (task, *_LIFECYCLE_KINDS)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    payload = _json_or_empty(row["payload"] if row else None)
    for key in ("resume_status", "retry_status", "source_status"):
        if payload.get(key) == "review":
            return "review"
    return None


def _open_run(con, task):
    """The newest still-open run for the card (the one a crash left dangling), or None."""
    row = con.execute(
        "select id from task_runs where task_id = ? and ended_at is null order by id desc limit 1",
        (task,)
    ).fetchone()
    return row["id"] if row else None


def _reclaim_run(con, run_id, now, summary):
    con.execute(
        "update task_runs set status='reclaimed', outcome='reclaimed', summary=?, ended_at=?,"
        " claim_lock=NULL, claim_expires=NULL, worker_pid=NULL where id = ?",
        (summary, int(now), run_id),
    )


def _block_ts(con, task, now):
    """Rule 3 (see feed.py): created_at of the newest 'blocked' event, else the card's own timeline.
    Computed here with the SAME formula the feed uses, so a dismiss and the feed agree."""
    try:
        row = con.execute(
            "select created_at from task_events where task_id = ? and kind = 'blocked'"
            " order by created_at desc limit 1",
            (task,)
        ).fetchone()
        if row and row["created_at"]:
            return int(row["created_at"])
    except sqlite3.OperationalError:
        pass
    r = con.execute("select last_heartbeat_at, started_at, completed_at from tasks where id = ?",
                    (task,)).fetchone()
    return max(int((r["last_heartbeat_at"] or 0) if r else 0),
               int((r["started_at"] or 0) if r else 0),
               int((r["completed_at"] or 0) if r else 0), 0)


def _do_restart(con, task, row, now):
    before = row["status"]
    if before not in ("blocked", "scheduled"):
        raise ActionError("not_in_expected_state", 409, f"{task} is {before}, not blocked")
    run_id = _open_run(con, task)
    if run_id is not None:
        _reclaim_run(con, run_id, now, "invariant recovery on unblock")
    resume = _resume_status(con, task)
    parents_ok = _parents_satisfied(con, task)
    if resume == "review":
        new_status = "review"
    else:
        new_status = "ready" if parents_ok else "todo"
    cur = con.execute(
        "update tasks set status = ?, current_run_id = NULL, consecutive_failures = 0,"
        " last_failure_error = NULL where id = ? and status in ('blocked', 'scheduled')",
        (new_status, task),
    )
    if cur.rowcount == 0:
        raise ActionError("not_in_expected_state", 409, f"{task} changed state mid-write")
    # `unblocked` is task-scoped (no run_id) upstream; its payload names the landing status.
    _add_event(con, task, "unblocked", now, payload={"status": new_status})
    # promote todo -> ready for THIS card when its parents are satisfied: the dispatcher does the
    # same each tick, but doing it here is what flips the badge on the very next feed poll.
    if new_status == "todo" and parents_ok:
        con.execute("update tasks set status = 'ready' where id = ? and status = 'todo'", (task,))
        new_status = "ready"
    return {"before": before, "after": new_status, "run_id": run_id,
            "detail": f"unblocked -> {new_status}" + (f" (recovered run {run_id})" if run_id else "")}


def _do_approve(con, task, row, note, now):
    """approve == add_comment (:1671) then restart, in that order, one transaction."""
    if row["status"] not in ("blocked", "scheduled"):
        raise ActionError("not_in_expected_state", 409, f"{task} is {row['status']}, not blocked")
    body = (note or "").strip() or "Approved from the BUSY BEE office dashboard."
    author = "bart (office dashboard)"
    # `task_comments.id` is INTEGER PRIMARY KEY AUTOINCREMENT in the live schema: let SQLite fill
    # it in, never hand it a text id (that is a hard datatype mismatch on the real DB).
    con.execute("insert into task_comments (task_id, author, body, created_at) values (?, ?, ?, ?)",
                (task, author, body[:500], int(now)))
    _add_event(con, task, "commented", now, payload={"author": author, "len": len(body)})
    res = _do_restart(con, task, row, now)
    res["detail"] = "approved + " + res["detail"]
    return res


def _do_dismiss(con, task, row, now):
    """Acknowledge + close the crashed run WITHOUT throwing the card's work away (it stays blocked;
    archiving the card would). A newer 'blocked' event makes the badge come back (see _block_ts)."""
    if row["status"] != "blocked":
        raise ActionError("not_in_expected_state", 409, f"{task} is {row['status']}, not blocked")
    run_id = _open_run(con, task)
    if run_id is not None:
        _reclaim_run(con, run_id, now, "dismissed from the office dashboard")
        if row["current_run_id"] is not None and row["current_run_id"] == run_id:
            con.execute("update tasks set current_run_id = NULL where id = ? and current_run_id = ?",
                        (task, run_id))
    _add_event(con, task, "office_dismissed", now, run_id=run_id,
               payload={"by": "office-dashboard", "at": int(now), "run_id": run_id,
                        "block_kind": (row["block_kind"] or "")})
    bts = _block_ts(con, task, now)
    return {"before": "blocked", "after": "blocked", "run_id": run_id, "block_ts": bts,
            "detail": f"dismissed run {run_id}" if run_id is not None else "dismissed (no open run)"}


def perform(action, task, *, agent=None, note=None, actor="dashboard"):
    """Do one action against a card. Returns {ok, action, task, before, after, run_id, detail}."""
    if action not in ACTIONS:
        raise ActionError("unknown_action", 400, f"unknown action: {action}")
    if not TASK_ID_RE.match(task or ""):
        raise ActionError("bad_task_id", 400, "task id must match ^t_[0-9a-f]{6,}$")
    cap = capability()
    if not cap["writable"]:
        raise ActionError("not_writable", 503, cap["reason"] or "writes are not enabled")
    con = _connect()
    try:
        con.execute("begin immediate")
        try:
            row = con.execute("select id, status, current_run_id, block_kind from tasks where id = ?",
                              (task,)).fetchone()
            if not row:
                raise ActionError("task_missing", 404, f"no such task: {task}")
            now = time.time()
            if action == "dismiss":
                result = _do_dismiss(con, task, row, now)
            elif action == "approve":
                result = _do_approve(con, task, row, note, now)
            else:
                result = _do_restart(con, task, row, now)
            con.execute("commit")
        except Exception:
            con.execute("rollback")
            raise
    finally:
        con.close()
    if action == "dismiss":
        state.record_dismissed(task, {"at": int(time.time()), "block_ts": result["block_ts"],
                                      "run_id": result["run_id"]})
    return {"ok": True, "action": action, "task": task, "agent": agent, "actor": actor,
            "before": result["before"], "after": result["after"], "run_id": result["run_id"],
            "detail": result["detail"]}


def log_action(record):
    """One JSON line per ATTEMPT (success or denial) to OFFICE_ACTIONS_LOG. Never raises."""
    path = _paths()["log"]
    if not path:
        return
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        pass
