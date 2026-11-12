"""Turn /opt/data/kanban.db into the cubicle feed.

One agent entry per roster id (server/roster.py). The status rule, per assignee:

  running task with a live claim      -> running  (walks to its room, types)
  blocked, waiting on a human         -> waiting  (amber: "BLOCKED: needs input")
  blocked because it crashed          -> error    (red: "CRASHED: <first line>")
  queued work (ready/todo)            -> paused   (lounge, "zZ")
  finished within the last 20 min     -> idle + completedAt ("✓ done" celebration)
  anything else                       -> idle     (wanders the lounge)

A blocked card is split by *why* it is blocked (see _block_kind): needing input and having crashed
are different states and must not share a badge — that is what removes the "0 waiting for a decision"
vs "BLOCKED: needs input" contradiction. A card only raises a badge while it is the agent's *current*
state (rule 3): once the agent started/finished something newer, the badge is history and the card
drops into `stale`, never hidden. `working` always means the CURRENT run and proves it is alive.

The status vocabulary stays inside the cubicle feed contract (docs/FEED.md):
running | waiting | error | paused | idle. Timestamps in kanban.db are epoch seconds; the feed
speaks ISO-8601 / epoch milliseconds.
"""
import json
import re
import sqlite3
import time
from datetime import datetime, timezone

from . import actions, roster, state

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


def _json_or_empty(payload):
    if isinstance(payload, dict):
        return payload
    try:
        out = json.loads(payload) if payload else {}
    except (ValueError, TypeError):
        out = {}
    return out if isinstance(out, dict) else {}


def read_tasks(db_path, now=None):
    """Read the tasks table read-only, plus the side tables best-effort.

    Returns a dict (not just the rows) so `blocked_at` and per-run data travel with the tasks.
    The side tables (task_events / task_runs / task_comments) may be absent on old deployments or
    the unit-test DB — tolerate that (OperationalError -> empty). Raises only if the tasks read
    fails; the caller caches the last good feed.
    """
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
        tasks = [dict(r) for r in rows]
        ids = {t["id"] for t in tasks}

        blocked_ev = {}
        try:
            for e in con.execute("select task_id, created_at from task_events where kind = 'blocked'"):
                if e["task_id"] in ids and e["created_at"]:
                    if int(e["created_at"]) > blocked_ev.get(e["task_id"], 0):
                        blocked_ev[e["task_id"]] = int(e["created_at"])
        except sqlite3.OperationalError:
            pass

        events_by_run = {}
        try:
            for e in con.execute("select task_id, run_id, kind, payload, created_at from task_events"):
                if e["task_id"] in ids and e["run_id"] is not None:
                    events_by_run.setdefault(int(e["run_id"]), []).append(dict(e))
        except sqlite3.OperationalError:
            pass

        runs = {}
        try:
            for r in con.execute(
                "select id, task_id, started_at, ended_at, status, outcome, last_heartbeat_at,"
                " worker_pid, claim_expires from task_runs"
                " where task_id in (select id from tasks where status != 'archived')"
            ):
                runs.setdefault(r["task_id"], []).append(dict(r))
        except sqlite3.OperationalError:
            pass
        for lst in runs.values():
            lst.sort(key=lambda r: (r["started_at"] or 0, r["id"] or 0), reverse=True)   # newest first

        comments = {}
        try:
            for c in con.execute("select task_id, body, created_at from task_comments"):
                if c["task_id"] in ids:
                    comments.setdefault(c["task_id"], []).append(dict(c))
        except sqlite3.OperationalError:
            pass
        for lst in comments.values():
            lst.sort(key=lambda c: c["created_at"] or 0)
    finally:
        con.close()

    blocked_at = {}
    for t in tasks:
        if t["status"] == "blocked":
            blocked_at[t["id"]] = blocked_ev.get(t["id"]) or max(
                int(t["last_heartbeat_at"] or 0), int(t["started_at"] or 0),
                int(t["completed_at"] or 0), 0)
    return {"tasks": tasks, "blocked_at": blocked_at, "runs": runs,
            "events_by_run": events_by_run, "comments_by_task": comments}


# ---------- blocked-card truth (rule 2) ----------

def _block_kind(t):
    """One formula: crashed only when there is NO block_kind AND failure evidence; else the kind,
    defaulting to needs_input (an operator blocked the card by hand)."""
    bk = (t.get("block_kind") or "").strip()
    if not bk and (t.get("consecutive_failures") or t.get("last_failure_error")):
        return "crashed"
    return bk or "needs_input"


def _block_error(t, kind):
    if kind == "crashed":
        err = (t.get("last_failure_error") or "").strip()
        if err:
            line = re.sub(r"\bpid\s+\d+\s*", "", err.splitlines()[0]).strip()
            return ("CRASHED: " + line[:120]) if line else "CRASHED: the worker exited and left the card blocked"
        return "CRASHED: the worker exited and left the card blocked"
    label = (kind or "needs_input").replace("_", " ")
    return f"BLOCKED: {label}"


# ---------- run/steps truth (spec 3) ----------

def _open_run(runs_list):
    for r in runs_list:                      # newest first
        if not r["ended_at"]:
            return r
    return None


def _live(t, run, now):
    """The RUN row is the truth for liveness; fall back to the task's own columns when there is none."""
    if run is not None:
        return (run["claim_expires"] or 0) > now or (run["last_heartbeat_at"] or 0) > now - STALE_CLAIM_S
    return (t["claim_expires"] or 0) > now or (t["last_heartbeat_at"] or 0) > now - STALE_CLAIM_S


def _humanise(ev):
    kind = (ev.get("kind") or "").lower()
    p = _json_or_empty(ev.get("payload"))
    if kind == "claimed":
        return "claimed by " + str(p.get("profile") or "worker")
    if kind == "heartbeat":
        return "heartbeat"
    if kind == "commented":
        body = p.get("body") or p.get("text") or ""
        n = p.get("len") if p.get("len") is not None else len(str(body))
        return f"comment ({n} chars)"
    if kind == "completed":
        return "completed: " + str(p.get("summary") or "")[:80]
    if kind == "blocked":
        return f"blocked: {p.get('kind') or p.get('reason') or 'blocked'}"
    if kind in ("crashed", "spawn_failed", "timed_out"):
        err = p.get("error") or p.get("last_failure_error") or kind
        return f"{kind}: {str(err)[:80]}"
    return kind or "update"


def _mkstep(ts, text, prev_ts):
    step = [int(ts or 0) * 1000, text]
    if prev_ts:
        step.append(max(0, int((ts - prev_ts) * 1000)))
    return step


def _steps_for_run(run, events_by_run, comments, now):
    """Steps of ONE attempt, oldest last -> newest last. Preference: this run's events, else
    comments in its window, else a bare 'run started' / 'run <outcome>'. Degrade quietly."""
    evs = sorted(events_by_run.get(run["id"], []), key=lambda e: e["created_at"] or 0)
    if evs:
        steps, prev = [], None
        for e in evs:
            steps.append(_mkstep(e["created_at"], _humanise(e), prev))
            prev = e["created_at"]
        return steps[-10:]
    lo, hi = run["started_at"] or 0, run["ended_at"] or now
    inwin = sorted([c for c in comments if lo <= (c["created_at"] or 0) <= hi],
                   key=lambda c: c["created_at"] or 0)
    if inwin:
        steps, prev = [], None
        for c in inwin:
            first = ((c["body"] or "").strip().splitlines() or [""])[0][:120]
            steps.append(_mkstep(c["created_at"], first, prev))
            prev = c["created_at"]
        return steps[-10:]
    steps = []
    if run["started_at"]:
        steps.append([int(run["started_at"]) * 1000, "run started"])
    if run["outcome"]:
        st = [int(run["ended_at"] or run["started_at"] or 0) * 1000, f"run {run['outcome']}"]
        if run["started_at"]:
            st.append(max(0, int(((run["ended_at"] or now) - run["started_at"]) * 1000)))
        steps.append(st)
    return steps


def _fallback_recent(mine):
    recent = []
    for t in sorted([x for x in mine if x["started_at"]], key=lambda x: x["started_at"])[-5:]:
        item = [int(t["started_at"]) * 1000, f"kanban: {(t['title'] or '')[:80]}"]
        if t["completed_at"] and t["started_at"]:
            item.append(max(0, int((t["completed_at"] - t["started_at"]) * 1000)))
        recent.append(item)
    return recent


def _run_view(t, run, runs_list, events_by_run, comments_by_task, now):
    """Build the per-agent `run`, current-attempt `recent`, and `previous` (older attempts)."""
    asc = sorted(runs_list, key=lambda r: (r["started_at"] or 0, r["id"] or 0))
    attempts = len(asc)
    task_comments = comments_by_task.get(t["id"], [])
    if run is not None:
        attempt = next((i + 1 for i, r in enumerate(asc) if r["id"] == run["id"]), 1)
        age = int(max(0, now - max(run["last_heartbeat_at"] or 0, run["started_at"] or 0)))
        run_field = {"id": run["id"], "startedAt": run["started_at"],
                     "since": int(run["started_at"] or 0) * 1000,
                     "heartbeatAt": run["last_heartbeat_at"] or run["started_at"] or 0,
                     "heartbeatAgeS": age, "alive": True, "attempt": attempt, "attempts": attempts}
        recent = _steps_for_run(run, events_by_run, task_comments, now)
        previous = []
        for r in [x for x in runs_list if x["id"] != run["id"]][:3]:      # newest first, max 3
            previous.append({"id": r["id"], "startedAt": r["started_at"], "endedAt": r["ended_at"],
                             "outcome": r["outcome"], "since": int(r["started_at"] or 0) * 1000,
                             "steps": _steps_for_run(r, events_by_run, task_comments, now)[:5]})
        return run_field, recent, previous
    age = int(max(0, now - max(t["last_heartbeat_at"] or 0, t["started_at"] or 0)))
    run_field = {"id": t.get("current_run_id"), "startedAt": t["started_at"],
                 "since": int(t["started_at"] or 0) * 1000,
                 "heartbeatAt": t["last_heartbeat_at"] or t["started_at"] or 0,
                 "heartbeatAgeS": age, "alive": True, "attempt": 1, "attempts": attempts or 1}
    return run_field, _fallback_recent([t]), []


# ---------- attention (rules 3, 4, 5) ----------

def _active_ts_excluding(t, mine):
    """Rule 3: the newest completed_at/started_at among the agent's OTHER non-archived tasks."""
    vals = []
    for o in mine:
        if o["id"] != t["id"]:
            vals += [o["completed_at"] or 0, o["started_at"] or 0]
    return max(vals) if vals else 0


def _attention_actions(kind):
    A = actions.ACTIONS
    acts = [{"kind": "restart", "label": A["restart"]["label"], "why": A["restart"]["why"]}]
    if kind != "crashed":                         # approve == "unblock with a human note", input only
        acts.append({"kind": "approve", "label": A["approve"]["label"], "why": A["approve"]["why"]})
    acts.append({"kind": "dismiss", "label": A["dismiss"]["label"], "why": A["dismiss"]["why"]})
    return acts


def _attention_task(t, kind, block_ts):
    ts = int(block_ts or 0)
    return {"id": t["id"], "title": (t["title"] or "")[:160], "kind": kind, "at": ts, "since": ts * 1000}


def _dismissed_map():
    try:
        return state.dismissed()
    except Exception:
        return {}


def _dismissed_tasks(previous_agents, current_ids):
    """Cards that were linked last snapshot but are now archived/gone (rule 6). Cheap diff."""
    if not previous_agents:
        return []
    out = []
    for prev in previous_agents:
        for src in (prev.get("task"), (prev.get("attention") or {}).get("task")):
            tid = (src or {}).get("id")
            if tid and tid not in current_ids and tid not in out:
                out.append(tid)
    return out


def build_feed(tasks, now=None, chat_by_agent=None, dashboard_url=None, previous_agents=None,
               gateway=None):
    """Build the roster feed. `tasks` may be a raw list of task rows (old callers/tests) or the dict
    from read_tasks(); `gateway` (bool, age_s, how) is the orchestrator's live-session signal, passed
    in by office.py so this function stays pure/testable."""
    now = now or time.time()
    chat_by_agent = chat_by_agent or {}
    blocked_at, runs_map, events_by_run, comments_by_task = {}, {}, {}, {}
    if isinstance(tasks, dict):
        blocked_at = tasks.get("blocked_at") or {}
        runs_map = tasks.get("runs") or {}
        events_by_run = tasks.get("events_by_run") or {}
        comments_by_task = tasks.get("comments_by_task") or {}
        tasks = tasks.get("tasks") or []
    current_ids = {t["id"] for t in tasks}

    by_assignee = {}
    for t in tasks:
        by_assignee.setdefault(roster.canonical_assignee(t["assignee"]), []).append(t)

    dismissed_map = _dismissed_map()
    agents_out = []
    for agent in roster.AGENTS:
        mine = by_assignee.get(agent["id"], [])
        running, blocked, queued, done = [], [], [], []
        for t in mine:
            st = t["status"]
            if st == "running":
                if _live(t, _open_run(runs_map.get(t["id"], [])), now):
                    running.append(t)
            elif st == "blocked":
                blocked.append(t)
            elif st in ("ready", "todo"):
                queued.append(t)
            elif st == "done":
                done.append(t)
        running.sort(key=lambda t: t["started_at"] or 0, reverse=True)

        # Attention vs stale: split blocked cards, dropping the ones that are only history (rule 3)
        # and the ones an operator dismissed and nothing newer has re-blocked.
        attention_cards, stale_cards = [], []
        for t in blocked:
            kind = _block_kind(t)
            bts = int(blocked_at.get(t["id"], 0))
            rec = dismissed_map.get(t["id"])
            if rec and int(rec.get("block_ts") or 0) >= bts:
                continue                                     # dismissed and nothing newer happened
            if kind == "crashed" and _active_ts_excluding(t, mine) > bts:
                stale_cards.append((t, kind, bts))           # newer work: that crash is history
            else:
                # A question stays a question: needs_input needs the operator until it is unblocked,
                # no matter how much other work the agent has done since. Only crashes age out.
                attention_cards.append((t, kind, bts))
        # needs_input first, then crashed; newest block_ts first inside a kind (rule 4)
        attention_cards.sort(key=lambda c: (0 if c[1] != "crashed" else 1, -c[2]))

        status, task, error, completed_at = "idle", None, None, None
        attention, run_field, recent, previous = None, None, _fallback_recent(mine), []

        done.sort(key=lambda t: t["completed_at"] or 0, reverse=True)
        if done:
            completed_at = _iso(done[0]["completed_at"])

        if running:
            status = "running"
            t = running[0]
            runs_list = runs_map.get(t["id"], [])
            task = _task_obj(t["id"], t["title"], dashboard_url)     # entry["task"]: live run ONLY (rule 6)
            run_field, recent, previous = _run_view(t, _open_run(runs_list), runs_list,
                                                    events_by_run, comments_by_task, now)
            # Live on one card, but another card may still be waiting on the operator: the badge says
            # "working" (truth: a claimed, heartbeating run), the banner still names the blocked card
            # so its action buttons stay reachable. Only the extras fall through to history.
            if attention_cards:
                ta, kind_a, bts_a = attention_cards[0]
                attention = {"task": _attention_task(ta, kind_a, bts_a), "actions": _attention_actions(kind_a)}
                for c in attention_cards[1:]:
                    stale_cards.append(c)
        elif attention_cards:
            t, kind, bts = attention_cards[0]
            status = "error" if kind == "crashed" else "waiting"
            error = _block_error(t, kind)
            attention = {"task": _attention_task(t, kind, bts), "actions": _attention_actions(kind)}
        elif queued:
            status = "paused"

        # Orchestrator is chat-driven (spec 3): a live gateway session is "working" even with no card,
        # and it must not hijack the line for a lingering `default` card. Banner (attention) stays.
        if agent.get("ceo") and status != "running" and gateway and gateway[0]:
            age = gateway[1] if len(gateway) > 1 else 0
            how = gateway[2] if len(gateway) > 2 else "gateway"
            status = "running"
            task = {"title": "orchestrating — chat with Bart"}       # no id: not a claimed card
            run_field = {"alive": True, "heartbeatAgeS": max(0, int(age)), "how": "gateway"}

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
            "attention": attention,
            # Older attempts of the live card, newest first — the UI folds these under
            # "previous attempts" so a crashed attempt's steps never look like today's work.
            "previous": previous,
            "stale": [{"id": t["id"], "title": (t["title"] or "")[:160], "kind": k, "at": ts}
                      for (t, k, ts) in stale_cards],
        }
        if task:
            entry["task"] = task
        if error:
            entry["error"] = error
        if run_field:
            entry["run"] = run_field
        if chat_by_agent.get(agent["mm"]):
            entry["chat"] = chat_by_agent[agent["mm"]]
        agents_out.append(entry)

    return {"company": COMPANY, "agents": agents_out,
            "dismissedTasks": _dismissed_tasks(previous_agents, current_ids)}
