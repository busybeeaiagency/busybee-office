"""Turn /opt/data/kanban.db into the cubicle feed.

One agent entry per roster id (server/roster.py). The status rule, per assignee:

  running task with a live claim      -> running  (walks to its room, types)
  blocked task                        -> error    (red: Bart's "blocked = red status")
  queued work (ready/todo)            -> paused   (lounge, "zZ")
  finished within the last 20 min     -> idle + completedAt ("✓ done" celebration)
  anything else                       -> idle     (wanders the lounge)

Timestamps in kanban.db are epoch seconds; the feed speaks ISO-8601 / epoch milliseconds.
"""
import json
import sqlite3
import time
from datetime import datetime, timezone

from . import roster

COMPANY = "BUSY BEE AI Agency"
STALE_CLAIM_S = 15 * 60      # a claim older than this with no heartbeat is dead
RECENT_DONE_S = 20 * 60      # how long a finish keeps the "✓ done" celebration


def _iso(epoch_s):
    if not epoch_s:
        return None
    try:
        return datetime.fromtimestamp(int(epoch_s), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _task_obj(task_id, title, url=None):
    out = {"id": task_id, "title": (title or "")[:160]}
    if url:
        out["url"] = url
    return out


def read_tasks(db_path, now=None):
    """Read the tasks table read-only. Raises on failure; the caller caches the last good feed."""
    now = now or time.time()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        con.execute("pragma busy_timeout=5000")
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "select id, title, assignee, status, priority, created_at, started_at, completed_at,"
            " block_kind, claim_expires, last_heartbeat_at, consecutive_failures, last_failure_error"
            " from tasks where status != 'archived'"
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def build_feed(tasks, now=None, chat_by_agent=None, dashboard_url=None):
    now = now or time.time()
    chat_by_agent = chat_by_agent or {}
    by_assignee = {}
    for t in tasks:
        canon = roster.canonical_assignee(t["assignee"])
        by_assignee.setdefault(canon, []).append(t)

    agents_out = []
    for agent in roster.AGENTS:
        mine = by_assignee.get(agent["id"], [])
        running, blocked, queued, done = [], [], [], []
        for t in mine:
            st = t["status"]
            if st == "running":
                expires = t["claim_expires"] or 0
                hb = t["last_heartbeat_at"] or 0
                live = expires > now or hb > now - STALE_CLAIM_S
                if live:
                    running.append(t)
            elif st == "blocked":
                blocked.append(t)
            elif st in ("ready", "todo"):
                queued.append(t)
            elif st == "done":
                done.append(t)
        running.sort(key=lambda t: t["started_at"] or 0, reverse=True)
        blocked.sort(key=lambda t: t["started_at"] or 0, reverse=True)

        status, task, error, completed_at = "idle", None, None, None
        if running:
            status = "running"
            t = running[0]
            task = _task_obj(t["id"], t["title"], dashboard_url)
        elif blocked:
            status = "error"                                    # blocked reads red in the office
            t = blocked[0]
            task = _task_obj(t["id"], t["title"], dashboard_url)
            kind = (t["block_kind"] or "needs input").replace("_", " ")
            error = f"BLOCKED: {kind}"
        elif queued:
            status = "paused"
            t = queued[0]
            task = _task_obj(t["id"], t["title"], dashboard_url)

        done.sort(key=lambda t: t["completed_at"] or 0, reverse=True)
        if done:
            completed_at = _iso(done[0]["completed_at"])
            if status == "idle" and (done[0]["completed_at"] or 0) > now - RECENT_DONE_S:
                pass                                                # cubicle celebrates on completedAt change
        recent = []
        for t in sorted([x for x in mine if x["started_at"]], key=lambda x: x["started_at"])[-5:]:
            item = [int(t["started_at"]) * 1000, f"kanban: {(t['title'] or '')[:80]}"]
            if t["completed_at"] and t["started_at"]:
                item.append(max(0, int((t["completed_at"] - t["started_at"]) * 1000)))
            recent.append(item)

        room = agent["home_room"]
        if status == "running" and task:
            room = roster.route_room(task.get("title") or "", agent["home_room"])

        entry = {
            "id": agent["id"],
            "name": agent["name"],
            "role": f"{agent['role']} — {roster.room_name(room)}",
            "status": status,
            "room": room,
            "completedAt": completed_at,
            "since": int(now * 1000),
            "recent": recent,
        }
        if task:
            entry["task"] = task
        if error:
            entry["error"] = error
        if chat_by_agent.get(agent["mm"]):
            entry["chat"] = chat_by_agent[agent["mm"]]
        agents_out.append(entry)

    return {"company": COMPANY, "agents": agents_out}
