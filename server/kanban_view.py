"""The kanban board as the office wants to show it: every card, grouped by roster agent.

`/api/feed` answers "what is each agent doing right now"; this answers "what is on their plate".
Same db, same assignee mapping (`roster.canonical_assignee`), so a card in the office and a card in
this list are the same card — the two views can never disagree about who owns what.

Read-only and strict about it: `file:…?mode=ro`, one connection per rebuild, closed immediately
(kanban.db is WAL and the orchestrator is writing to it constantly). Archived cards are none of our
business, `scheduled` is just `todo` with a date, and anything the board does not recognise lands in
`todo` rather than disappearing — an unknown status is still unfinished work.

`counts` are the TRUE totals while the lists are capped (25 live / 8 done), so one agent with 300
finished cards cannot make the page unreadable and the count still tells the truth. A transient db
failure answers the last good snapshot plus a `warning`: the board never 500s out from under a
dashboard that is bolted to a TV.
"""
import os
import re
import sqlite3
import threading
import time

from . import roster

TTL_S = 5                      # process-wide: the page polls, the db is read at most once per 5 s
STATUSES = ["todo", "ready", "running", "blocked", "review", "done"]
LIVE_CAP = 25                  # task-list cap for every live column
DONE_CAP = 8                   # ... and a shorter one for the history column
TITLE_MAX = 120
UNKNOWN_STATUS = "todo"        # an unrecognised status is unfinished work, not a mystery
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_lock = threading.Lock()
_cache = {"at": 0.0, "owners": None, "error": ""}     # owners: None until the first good read


def db_path():
    """Resolve the db at CALL time (same env as office.DB_PATH) so tests can use a temp db."""
    hermes = os.environ.get("OFFICE_HERMES_DIR", "/hermes")
    return os.environ.get("OFFICE_DB") or f"{hermes}/kanban.db"


def _group(status):
    """Board status -> our column. 'scheduled' folds into todo, and so does anything unknown."""
    key = (status or "").strip().lower()
    if key == "scheduled":
        return "todo"
    return key if key in STATUSES else UNKNOWN_STATUS


def _title(text):
    """Control chars out (they wreck a <pre>), trimmed, capped."""
    return _CONTROL.sub(" ", text or "").strip()[:TITLE_MAX]


def _stamp(row):
    """When this card last mattered: finished, else started, else created. 0 = unknown."""
    for value in (row["completed_at"], row["started_at"], row["created_at"]):
        try:
            if value:
                return int(value)
        except (TypeError, ValueError):
            continue           # a legacy row may hold text where a number belongs
    return 0


def _card(row):
    out = {"id": row["id"], "title": _title(row["title"]), "status": _group(row["status"]),
           "updated_at": _stamp(row)}
    if out["status"] == "todo" and (row["status"] or "").strip().lower() == "scheduled":
        out["scheduled"] = _stamp(row)               # when the card wakes up
    return out


def read_board(path):
    """Every non-archived card, bucketed owner -> status -> cards. Raises if the read fails."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        con.execute("pragma busy_timeout=5000")
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "select id, title, assignee, status, created_at, started_at, completed_at"
            " from tasks where status is null or status != 'archived'"
        ).fetchall()
    finally:
        con.close()                                   # never held across an HTTP request

    owners = {}
    for row in rows:
        who = roster.canonical_assignee(row["assignee"])
        # an assignee outside the fleet is still work; it just has no desk, so it lands on ""
        owners.setdefault(who if roster.agent_by_id(who) else "", {}).setdefault(
            _group(row["status"]), []).append(_card(row))
    for columns in owners.values():
        for cards in columns.values():
            cards.sort(key=lambda card: -card["updated_at"])       # newest first, everywhere
    return owners


def _shape(columns):
    """One owner's {counts, tasks}: true totals, capped lists."""
    counts, tasks, total = {s: 0 for s in STATUSES}, {}, 0
    for status in STATUSES:
        cards = columns.get(status) or []
        counts[status] = len(cards)
        total += len(cards)
        tasks[status] = cards[:DONE_CAP if status == "done" else LIVE_CAP]
    counts["total"] = total
    return {"counts": counts, "tasks": tasks}


def build(owners):
    """The payload body: board totals, one entry per roster agent — zero counts included, so the UI
    can honestly say "no tasks" — then the cards nobody in the fleet owns."""
    totals = {s: 0 for s in STATUSES}
    for columns in owners.values():
        for status in STATUSES:
            totals[status] += len(columns.get(status) or [])
    totals["total"] = sum(totals[s] for s in STATUSES)   # same shape as one agent's counts
    return {
        "generated_at": int(time.time()),
        "ttl": TTL_S,
        "statuses": list(STATUSES),
        "counts": totals,
        "agents": {a["id"]: _shape(owners.get(a["id"]) or {}) for a in roster.AGENTS},
        "unassigned": _shape(owners.get("") or {}),
    }


def board(agent=None):
    """The board, or one agent's slice of it, rebuilt at most once per TTL_S.

    `agent` is accepted as written on the kanban (default/hermes/orchestrator all mean the 🟡
    orchestrator) and answered as the canonical roster id. Returns None for anyone not in the fleet
    — the route turns that into a 404 without echoing the query back. A db failure answers the last
    good snapshot plus `warning`; with no snapshot yet, `db_unavailable` (the route makes that a 503).
    """
    who = roster.canonical_assignee(agent) if agent else None
    if agent and not roster.agent_by_id(who):
        return None
    with _lock:
        if not (_cache["owners"] is not None and time.time() - _cache["at"] < TTL_S):
            try:
                _cache.update({"at": time.time(), "owners": read_board(db_path()), "error": ""})
            except Exception as e:                    # never a 500 for a transient db problem
                _cache["error"] = f"{type(e).__name__}: {e}"
        owners, error = _cache["owners"], _cache["error"]
    if owners is None:
        return {"ok": False, "error": "db_unavailable",
                "message": error or "kanban.db could not be read"}
    data = build(owners)
    if who:
        data = {**data, "agent": who, "agents": {who: data["agents"][who]}}
    if error:
        data["warning"] = error                       # stale-but-usable beats an error page
    return {**data, "ok": True}
