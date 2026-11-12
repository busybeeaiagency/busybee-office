# Spec 3 — office feed part 2: live-run truth (Bart's auditor screenshot, 2026-11-12 15:45)

Adds to `docs/spec-actions-backend.md` (same commit series, same files: `server/feed.py`,
`server/sources.py`, `server/office.py`, `test/`). Same rules: stdlib only, the FEED status
vocabulary is unchanged, dense why-comments.

## 5. `working` must mean the CURRENT run, and prove it is alive

Bart saw `working` on auditor / `t_635c0449` with a green pill while the newest `Last steps`
entry was 126 min old — the steps belonged to the **crashed earlier attempt** and the reclaimed,
live run was invisible. A worker that is mid-flight with no comment for 2 h is *working*, not stale.

- `read_tasks()` additionally loads (best effort, tolerate missing tables — the unit-test DB has
  none): `task_runs` rows for the agent's non-archived tasks, newest 3 per task:
  `{"tasks": [...], "blocked_at": {...}, "runs": {task_id: [ {id, started_at, ended_at, status,
  outcome, last_heartbeat_at, worker_pid, claim_expires}, ... newest first ]}}`.
- For the live claim (the `running` task), the **run row** is the truth, not the task row:
  `live = (run.claim_expires > now) or (run.last_heartbeat_at > now - STALE_CLAIM_S)` using the
  newest open run; fall back to the task's own columns when no run row exists.
- New per-agent field (only while a claim is live):
  ```json
  "run": {"id": 281, "startedAt": 1794499000, "since": 1794499000000,
          "heartbeatAt": 1794503000, "heartbeatAgeS": 45, "alive": true,
          "attempt": 2, "attempts": 3}
  ```
  `alive` is the live test above; `heartbeatAgeS` is `now - max(run.last_heartbeat_at, started_at)`
  (integer seconds, never negative); `attempt` is the 1-based index of this run among the task's
  runs ordered by `started_at`. The UI prints `started 12 min ago · alive, 45 s` from it.
- **Steps are per attempt.** Replace the current `recent` (built from `tasks.started_at`) with
  steps derived per run, and keep the FEED field meaning: `recent` = steps of the **current**
  attempt only (newest last, `[ms, text, durMs]`, max 10), so upstream renders today's behaviour
  and never mixes attempts.
  Source per attempt, in this order of preference, degrading quietly:
  1. `task_events` of that run (`run_id` column): one step per event, text =
     `kind` humanised (`claimed` → `claimed by <profile>`, `heartbeat` → `heartbeat`,
     `commented` → `comment (<len> chars)`, `completed` → `completed: <summary first 80>`,
     `blocked` → `blocked: <payload kind>`, `crashed`/`spawn_failed`/`timed_out` →
     `<kind>: <error first 80>`), ts = event `created_at`;
  2. if the attempt has no events (older deployments): `task_comments` whose `created_at` falls in
     the run's window — one step per comment, first line of the body, `[:120]`;
  3. else one step: `run started` / `run <outcome>` as available.
- New per-agent field `previous` (older attempts, newest first, max 3):
  `[{"id": 279, "startedAt": …, "endedAt": …, "outcome": "crashed", "since": …,
    "steps": [[ms, text, dur], …max 5]}]` — the UI folds these under `previous attempts`.
  A task with a single attempt has `previous: []`.

## 6. Archived and duplicate cards must not appear as anyone's work

- A task may become an agent's *attention* card or its *working* card only when its `status`
  matches what the label claims and its `assignee` maps (via `roster.canonical_assignee`) to that
  agent. Concretely: `entry["task"]` (the "Working on" line) is set **only** for the live-claimed
  `running` task. Blocked/queued/attention cards are reported in `attention.task` / `queued` only,
  never in `entry["task"]` — so an `archived` or `done` card can never be shown as "working on".
- `read_tasks()` keeps `where status != 'archived'` (unchanged) **and** the UI must be able to
  detect a card that *did* disappear: emit `dismissedTasks: ["t_9d94a9fa", …]` at the top level of
  the feed for the cards this agent had shown before and that are now archived or gone. Keep it
  cheap: diff the previous feed snapshot held in the office server cache (`current_feed()` already
  keeps one) — implement as an optional `previous_agents` argument to `build_feed` so the pure
  function stays testable, and have `office.py` pass the last good snapshot.
  A card listed in `dismissedTasks` must never be linked from a detail card any more.

## 7. Orchestrator (`default` / `hermes` / `orchestrator`) is chat-driven

The CEO talks to Bart; most of the time it has no kanban task. Its desk must not attach to
whichever `default`-assigned card lingers.

- New helper `server/sources.py`: `gateway_alive(now=None) -> (bool, age_s, how)` —
  live signal for the main hermes session, cheapest first, cached 15 s like `vitals()`:
  1. mtime age of `{HERMES_DIR}/logs/agent.log` → alive when `age < GATEWAY_ALIVE_S`
     (default 300 s, env `OFFICE_GATEWAY_ALIVE_S`);
  2. else the existing `Hermes UI` probe result (`http://<host>:9119/` reachable) → alive with
     `how = "probe"`;
  3. else `False`. Never raise; return `how = "no signal"` when neither is available.
- In `build_feed`, for the agent whose roster id is `orchestrator` **only**
  (pass the flag from `roster.AGENTS` via an `agent.get("ceo")` marker you add to roster.py —
  add `"ceo": True` to that one entry, do not string-compare ids in feed.py):
  - its `entry["task"]` is set **only** when a live-claimed `running` task exists for it
    (rule 6 already guarantees this), i.e. a real `default`-profile worker run is claimed now;
  - when it has no live claim but the gateway is alive →
    `status = "running"`, `task = {"title": "orchestrating — chat with Bart"}` (**no `id`**),
    `entry["run"] = {"alive": true, "heartbeatAgeS": <agent.log age>, "how": "gateway"}`,
    and no `attention` badge is suppressed for it: a blocked `default` card still shows as
    `waiting` *in the banner* (`attention`), it just does not hijack the "Working on" line;
  - when it has no live claim and the gateway is not alive → keep the normal rule
    (waiting / paused / idle).
  `gateway_alive`'s result must be an **argument** to `build_feed`
  (`gateway=None`), so the tests stay hermetic; `office.py` passes `sources_api.gateway_alive()`.
- The counters stay honest: document in one comment that `working` = a live claim **or** the
  orchestrator's live session, and the tooltip wording in spec 2 §4 must say so.

## Tests to add (hermetic, temp DBs)

- run row open + heartbeat 45 s ago → `run.alive is True`, `heartbeatAgeS == 45`,
  `attempt`/`attempts` correct for a task with 3 runs; heartbeat 3 h old + expired claim →
  `alive False` and the agent is **not** `running` (it falls to attention/idle as the rules say).
- `recent` contains only current-attempt steps (`task_events` of the open run) and `previous[0]`
  holds the crashed attempt's steps; when events are missing, comments are used.
- an `archived` card never appears in `task`; a card that was in the previous snapshot and is now
  archived appears in `dismissedTasks` and not in `task`.
- orchestrator with a lingering `blocked` `default` card + `gateway=(True, 12, "log")` →
  `status == "running"`, `task.title` starts with `orchestrating`, `task` has no `id`, and
  `attention.task.id` is the blocked card; with `gateway=(False, …)` → no `orchestrating`.
- every pre-existing test in `test/feed_test.py` and `test/actions_test.py` still passes
  (build_feed's new arguments must be optional).
