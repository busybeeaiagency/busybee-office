# Spec 1 — office backend: live status + operator actions (kanban writes)

Repo: `busybee-office` (fork of cubicle). The dashboard is served in docker (`python:3.14-slim`,
`python3 -m server.office`, port 9120) with `/hermes` mounted **read-only**.
Style: stdlib only (no new pip dependencies, no FastAPI — the container has no pip packages and the
existing server is `http.server`). Keep the file's existing voice: dense, commented, `snake_case`,
module docstrings that explain the *rule*, not the syntax.

## Non-negotiable decisions (already made — do not re-decide)

1. **Status vocabulary stays inside the cubicle feed contract** (`docs/FEED.md`):
   `running | waiting | error | paused | idle`. Never invent a new status string.
2. **`blocked` cards are split by *why* they are blocked** (this is what removes the
   "0 waiting for a decision" vs "BLOCKED: needs input" contradiction):
   - `block_kind == 'needs_input'` (or any non-crash kind) → status **`waiting`**,
     `error` text `BLOCKED: needs input` (or `BLOCKED: <kind with _ → space>`).
   - crash-blocked (`block_kind` is NULL/empty AND (`consecutive_failures > 0` OR
     `last_failure_error` is set)) → status **`error`**, `error` text
     `CRASHED: ` + the first line of `last_failure_error` (strip `pid <n> `, cap 120 chars),
     falling back to `CRASHED: the worker exited and left the card blocked`.
     A crash row must **never** say "needs input".
   - a third and final case, because it is real: blocked with **no** `block_kind` and **no**
     failure evidence at all (an operator/agent blocked the card by hand, e.g. the test cards) →
     treat it as `needs_input` (`kind = "needs_input"`, text `BLOCKED: needs input`).
     One formula: `kind = "crashed" if (not block_kind and (consecutive_failures or
     last_failure_error)) else (block_kind or "needs_input")`.
3. **Stale badges (Bart's fix #1).** A blocked card raises a badge only while it is the *current*
   state of that agent. Compute `block_ts(card)` = created_at of the newest `blocked`
   `task_events` row for the card; fall back to
   `max(last_heartbeat_at, started_at, completed_at, 0)` of the card when no such event exists
   (and the `task_events` table may be absent entirely — tolerate that, the unit-test DB has none).
   Compute `active_ts(agent)` = max over the agent's **other** non-archived tasks of
   `completed_at` and `started_at`. Drop a blocked card from the badge when
   `active_ts > block_ts` — i.e. the agent started or finished something *after* that card was
   blocked, so the badge is history, not the present. The dropped cards are still reported as
   `stale` (see the feed shape) so nothing is hidden from the modal.
4. **Precedence per agent:** live-claimed `running` → attention (`waiting` beats `error` when both
   exist, newest `block_ts` first inside a kind) → `queued`(paused) → `idle` (+`completedAt`).
   A claim is live when `claim_expires > now` or `last_heartbeat_at > now - STALE_CLAIM_S`
   (unchanged rule).
5. **The card that causes the badge is named everywhere.** `entry["task"]` and
   `entry["attention"]["task"]` must be the *same* id, and `entry["error"]` must never name a
   different card.
6. **Writes are direct kanban-DB writes**, mirroring `/opt/hermes/hermes_cli/kanban_db.py`
   (`unblock_task` :3557, `add_comment` :1671, `_end_run`/`_reclaim_dangling_run` :1857/:3527,
   `_resume_status_from_events` :2018, `_parents_satisfied` :2100). The hermes CLI is *not*
   installed in the container — say so in the module docstring and name the functions you mirror.
   Every write happens in one `BEGIN IMMEDIATE` transaction with `PRAGMA busy_timeout=5000`,
   `isolation_level=None`, and must be re-run-safe (a `WHERE` guard on the current status, and
   return 409 when the guard stops matching instead of writing blindly).
7. **No write without a confirm token** (server-side, not just a JS dialog) and a rate limit.
   Both are pure functions in `server/actions.py` so they are unit-testable without HTTP.

## New file `server/actions.py`

Public surface (keep it small and functional; no classes needed):

```python
ACTIONS = {"restart": ..., "dismiss": ..., "approve": ...}   # labels/tooltips for the UI
def capability() -> dict            # {"writable": bool, "reason": str|None, "actions": [...]}
def issue_token(action, task, now=None) -> str        # 120 s TTL, single use, stored in state
def consume_token(action, task, token, now=None) -> (bool, reason)
def check_rate(action, task, now=None) -> (bool, retry_after_s)   # 1 write per 10 s per task,
                                                                  # 20 writes per 60 s global
def perform(action, task, *, agent=None, note=None, actor="dashboard") -> dict
def log_action(record: dict) -> None                  # one JSON line per ATTEMPT, incl. denials
```

`perform()` returns `{"ok": True, "action", "task", "before", "after", "run_id", "detail"}` and
raises `ActionError(code, http_status, message)` for: `unknown_action`(400), `bad_task_id`(400 —
ids must match `^t_[0-9a-f]{6,}$` case-insensitively), `not_writable`(503 — DB is read-only /
`OFFICE_ALLOW_WRITES` unset), `no_token`/`bad_token`/`expired_token`(409), `rate_limited`(429),
`task_missing`(404), `not_in_expected_state`(409 — the status guard did not match, e.g. the card
was already unblocked by someone else).

Semantics:

- `restart` — unblock (retry). Exactly `unblock_task`: resume status from the newest lifecycle
  event (`review` only when that event's payload says review), reclaim a leaked open run
  (`status/outcome='reclaimed'`, `summary='invariant recovery on unblock'`, `ended_at=now`,
  clear claim/pid), land on `ready` when every parent is `done|archived` else `todo`,
  `UPDATE tasks SET status=?, current_run_id=NULL, consecutive_failures=0, last_failure_error=NULL
  WHERE id=? AND status IN ('blocked','scheduled')`, insert `task_events(kind='unblocked')`.
  Then promote `todo → ready` for that one card when its parents are satisfied (the dispatcher
  does the same each tick; doing it here is what makes the badge change in the *next* poll).
  Keep `block_kind`/`block_recurrences` — that is deliberate in upstream; only
  `consecutive_failures`/`last_failure_error` reset.
- `approve` — `add_comment` (`author='bart (office dashboard)'`,
  body = `note` or `Approved from the BUSY BEE office dashboard.`) **then** `restart`, in that
  order, in one transaction. This is "unblock with a human note".
- `dismiss` — acknowledge the error and close the crashed **run**, without touching the card's
  work: `UPDATE task_runs SET status='reclaimed', outcome='reclaimed',
  summary='dismissed from the office dashboard', ended_at=?, claim_lock=NULL, claim_expires=NULL,
  worker_pid=NULL WHERE task_id=? AND ended_at IS NULL`, clear `tasks.current_run_id` when it
  pointed at that run, insert `task_events(kind='office_dismissed')`, and record in the dashboard
  state: `dismissed[task] = {"at": now, "block_ts": <block_ts>, "run_id": run_id}`.
  The card stays `blocked` (archiving the whole card would throw the work away — say that in a
  comment). A **newer** `blocked` event on the same card makes the badge come back: the feed only
  ignores a dismissal whose recorded `block_ts` is >= the card's current `block_ts`.

State store `server/state.py` (or inside actions.py if it stays under ~40 lines): JSON file at
`OFFICE_STATE` (default `os.path.join(os.path.dirname(OFFICE_DB), "state", "busybee-office.json")`,
the office falls back to a `tmp/` path when nothing is writable). Atomic write
(`tmp` + `os.replace`), guarded by the module's existing threading lock discipline. It holds
`{"dismissed": {...}, "tokens": {"action|task": {"token":..,"exp":..,"used":..}}, "hits": [...]}`.
Tokens are keyed by `action|task` so a page can only confirm the exact thing it showed.
State file permission: create it `0o664`.

DB connection for writes: `OFFICE_DB_RW` env, falling back to `OFFICE_DB`. Open it
lazily per call (`sqlite3.connect(path, timeout=5, isolation_level=None)`), never hold a
connection across HTTP requests. Reads for `block_ts`/status guards use the same connection
(the RW one) so a write and its guard see one snapshot.

## `server/feed.py`

- `read_tasks()` also loads, best-effort (`try/except sqlite3.OperationalError` — the test DB and
  old deployments may not have them): `task_events` newest `blocked` per task, and returns
  `{"tasks": [...], "blocked_at": {task_id: epoch_s}}`. Keep the signature backward compatible
  for the existing tests (`build_feed(feed.read_tasks(...))` must keep working: accept either a
  list of task rows or the new dict).
- Implement rules 2–5 above.
- Feed entry additions for the agent (all optional, additive; existing fields unchanged):
  ```json
  "attention": {"task": {"id": "t_x", "title": "...", "kind": "needs_input|crashed",
                          "at": 1794494145, "since": 1794494145000},
                "actions": [{"kind": "restart", "label": "Restart task",
                             "why": "unblock the card so the dispatcher retries it"},
                            {"kind": "approve", ...},   // only when kind == needs_input
                            {"kind": "dismiss", "label": "Dismiss",
                             "why": "close the crashed run and clear the badge"}]},
  "stale": [{"id": "t_y", "title": "...", "kind": "crashed", "at": 1794490000}]
  ```
  When there is no attention card: `"attention": null` (and `actions: []`).
- `error` text rules (rule 2) apply to both `waiting` and `error` rows, and
  `entry["error"]` must start with the badge text and never mention a foreign task id.
- Keep `STALE_CLAIM_S`/`RECENT_DONE_S`; add module constants for anything new
  (`ATTENTION_MAX_AGE_S` is NOT wanted — staleness is rule 3, not a time window).

## `server/office.py`

- `POST /api/office/action` — the only new write route.
  - Requires header `x-cubicle: 1` (the fork's existing convention for every write route;
    without it → 405/403 with `{"error": "x-cubicle required"}`).
  - Body: JSON, `Content-Length <= 4096` else 413; fields `action`, `task`, `agent?`, `note?`,
    `token`. `note` is trimmed and capped at 500 chars; strip control chars.
  - Order of checks: header → parse → action known → task id shape → writable → token consumed →
    rate limit → `perform()`. Every exit (including denials) calls `log_action` and answers JSON:
    `{"ok": true, ...}` or `{"ok": false, "error": code, "message": str}` (+ `Retry-After` header
    for 429).
  - Never echo a credential; the response may echo task/before/after.
- `GET /api/office/confirm?action=restart&task=t_x` → `{"token": ..., "expires_in": 120,
  "action", "task", "label", "why"}`; this is what the page calls **from inside the confirm
  dialog flow** so the token cannot be guessed. Unknown action/id → 400. 403 without `x-cubicle`.
- `GET /api/office/actions` → `capability()` (the page hides the buttons when `writable` is false).
- `GET /healthz` — add `"writable": bool, "actions": <count performed>`; keep the existing keys.
- `do_POST` must exist (the class only has GET/HEAD/PUT today). Keep `_send` for responses.

## `deploy/portainer.py`

Keep the existing "spec is rebuilt from the live container" behaviour and make the new parts
idempotent:
- add the bind `/var/lib/docker/volumes/busybee-hermes-data/_data:/hermes-rw:rw` (so kanban.db and
  the log get a writable path while `/hermes` stays read-only),
- force the container to run as the hermes user: `HostConfig.User = "10000:10000"` — **required**,
  otherwise SQLite's `-wal`/`-shm` sidecar files get created root-owned and the gateway (uid 10000)
  can no longer open the DB. Comment that in the code.
- env: `OFFICE_DB_RW=/hermes-rw/kanban.db`, `OFFICE_ALLOW_WRITES=1`,
  `OFFICE_STATE=/hermes-rw/state/busybee-office.json`,
  `OFFICE_ACTIONS_LOG=/hermes-rw/logs/office_actions.log`.
  (`/hermes-rw/state` must be created by the app with `os.makedirs(..., exist_ok=True)`.)
- `--filter` the env/mount rebuild so the new `OFFICE_*` vars and the `/hermes-rw` mount survive a
  `recreate`, and `User` is carried through `spec()`.

## Tests (must pass with `python3 -m unittest discover -s test -v` from the repo root)

- Extend `test/feed_test.py` — do not rewrite the existing cases:
  crash-blocked → `status == "error"` and `error` starts with `CRASHED:`;
  `needs_input` → `status == "waiting"` and `error` starts with `BLOCKED:`;
  a crash-blocked card + a later completed task for the same agent → `status == "idle"` and the
  card shows up in `stale`, not in `error`;
  a blocked card + a *live* running claim → `status == "running"`;
  dismissed card (inject the state) → badge gone, and a newer `blocked` event brings it back.
- New `test/actions_test.py`: build a throwaway DB with `tasks`, `task_events`, `task_runs`,
  `task_links`, `task_comments`; point `OFFICE_DB_RW`/`OFFICE_STATE`/`OFFICE_ACTIONS_LOG` at temp
  paths (module-level path resolution must be overridable — read env at call time or expose
  `_paths()`), then assert:
  `restart` on a blocked card whose parents are done → `ready`, `consecutive_failures == 0`,
  `last_failure_error is None`, `current_run_id is None`, a leaked `task_runs` row is closed
  `reclaimed`, one `unblocked` event appended; with an open parent → `todo`;
  `approve` → a `task_comments` row with the note + `commented` event + the card unblocked;
  `dismiss` → the open run closed `reclaimed`, an `office_dismissed` event, `tasks.status`
  unchanged, and the state file records `dismissed[task]["block_ts"]`;
  a second `restart` on the same (now `ready`) card → `ActionError` 409 and **no** new event;
  token: missing token → no DB change; wrong token → no DB change; the same token twice → the
  second is refused; expired token → refused;
  rate limit: 2nd write to the same task inside 10 s → refused, and `log_action` wrote one JSON
  line per attempt to the log path (parse each line with `json.loads`).
- Keep every test independent of the live `/opt/data/kanban.db` (temp DBs only) and of the network.

## Definition of done for this spec

`python3 -m unittest discover -s test -v` green, `python3 -m compileall -q server` clean,
`git diff` touches only `server/`, `test/`, `deploy/portainer.py`, and no file gets a new pip
dependency. Commit with a clear message on the current branch; do not push.
