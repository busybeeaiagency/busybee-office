# AGENTS.md — working on the BUSY BEE office dashboard

Read this before changing anything. The dashboard is a **fork of cubicle** (MIT) plus a small
Python server; both halves are live at http://10.10.10.202:9120.

## Layout

- `public/` — the vendored cubicle client (index.html, index.js, themes/kit.js, themes/*.js).
  Treat `kit.js`, `plaza.js`, `military.js`, etc. as **upstream**: change them only if you must,
  and say so in the commit message.
- `public/themes/busybee.js` — **our** theme (the six rooms). This is where the visuals live.
- `public/index.html` — upstream + a handful of *guarded* hooks our theme uses. Never remove the
  `theme.plan` / `theme.pickDesk` / `ROOM_*` blocks; stock themes depend on the fallbacks staying.
- `server/` — our stdlib HTTP server: `office.py` (routes), `feed.py` (kanban → cubicle),
  `roster.py` (fleet + room routing), `sources.py` (Mattermost/Beszel/Kuma/probes).
- `deploy/portainer.py` — container lifecycle on the docker host.
- `test/feed_test.py` — unit tests for the feed mapping and room routing.

## Rules

1. **Never put a credential in this repo.** Secrets are read at runtime from `/hermes/creds`
   (`mattermost.json`, `beszel.json`, `office.json`, `dashboard.json`). A page response must never
   contain a token, password or API key — and neither may an `logs/office_actions.log` line
   (`test/dispatch_test.py` pins that for the dispatcher nudge, whose password stays server-side).
2. **The page polls; the server never pushes.** Browser cadence: feed 4 s, vitals 5 s,
   chat 20 s, kanban board 12 s. Server-side caches: feed ≤1 s, vitals 15 s, board ≤5 s,
   Mattermost ≤1 req/min.
3. **Everything drawn is code.** No image files, fonts, or new dependencies in `public/`.
4. **Looks come from the agent id** (`K.rand`, `K.pick`), never `Math.random()` — agents must not
   change clothes between frames.
5. **Keep the status mapping honest**: running → `running`, blocked → `error` (red), queued →
   `paused`, finished recently → `idle` + `completedAt`, else `idle`. Tests in `test/feed_test.py`
   pin this behaviour — update them together with any change.
6. **Upstream licence stays intact**: `LICENSE` and the upstream headers are not ours to rewrite.

## Workflow

```bash
# 1. run locally against the real kanban.db (no container needed)
OFFICE_PORT=9120 OFFICE_DB=/opt/data/kanban.db OFFICE_HERMES_DIR=/opt/data python3 -m server.office

# 2. tests + theme syntax
python3 -m unittest test.feed_test
node --check public/themes/busybee.js

# 3. look at it
#    open http://127.0.0.1:9120/  (add ?kiosk for the TV view, &theme=busybee-mansion to force ours)

# 4. deploy (container picks up the read-only /app mount; server code needs a restart)
python3 deploy/portainer.py recreate
python3 deploy/portainer.py status
python3 deploy/portainer.py logs 40
```

Verify before you claim anything: open the URL, watch the agents change state, then cross-check
against `hermes kanban list --json` (assignee → status). The acceptance bar is ≥3 agents visibly
in the state kanban.db says they are.

## Adding an agent or a room

1. `server/roster.py`: add to `AGENTS` (id, name with its coloured dot, role, home room, colour,
   Mattermost user). The Mattermost user must exist in `/opt/data/creds/mattermost.json`.
2. `public/themes/busybee.js`: add the room to the floor plan (room table + `plan()` desks +
   `obstacles()` walls + `drawRoom()` plaque) and the colour map in `drawChar`.
3. `test/feed_test.py`: cover the new room routing if you added keywords.

## Gotchas

- **kanban.db is WAL mode.** Always open it `mode=ro` with a busy timeout, and never hold a
  connection across requests — open, read, close per refresh (see `feed.read_tasks`).
- **Reading the board and moving a card are different doors.** `GET /api/office/kanban` reads
  kanban.db `mode=ro` (`server/kanban_view.py`); the "Check queue" button does **not** touch the db
  at all — `server/dispatch.py` signs in to the Hermes dashboard (`:9119`) and asks it for one
  dispatcher pass, because the dispatcher is the only thing allowed to claim a card. The office
  writes kanban.db only through `server/actions.py`, one audited line per attempt.
- **The office never runs the dispatcher itself.** No `claim`, no `status` flip from this repo: a
  second implementation of claim rules is how a card ends up claimed twice.
- **The claim is the liveness signal.** `claim_expires` (15 min) or `last_heartbeat_at`; a running
  task with neither is stale and must not render as "working".
- **Mattermost rate limits:** presence + posts are bulk-cached; do not add an un-cached call.
- **`?kiosk` reads the window aspect** to pick the floor plan; the theme's `plan()` receives it
  as `target`.
- **The container mounts the repo read-only.** Local edits are live for static files after a
  browser reload; `server/*.py` changes need `deploy/portainer.py recreate`.
