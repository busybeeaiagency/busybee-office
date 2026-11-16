# BUSY BEE animated office dashboard

A live pixel-art office for the BUSY BEE AI Agency fleet, served at **http://10.10.10.202:9120**
(`?kiosk` for a full-screen TV view; the five-room mansion theme is the default).

Agents appear as pixel characters in five rooms — **Coding, Game Dev, Research, Communications,
Art** — plus a lounge. Idle agents wander the lounge and chat with each other, a working agent
walks to the room its task belongs to and types, a blocked agent shows red, and a finished run
gets a brief "✓ done" celebration. A small vitals panel shows homelab health.

> This repo is a fork of [caglarutkuguler/cubicle](https://github.com/caglarutkuguler/cubicle)
> (MIT). The upstream README, `LICENSE`, `docs/`, `bin/` and `examples/` are kept verbatim; this
> file documents what the fork adds. Upstream commit: `b10185f`.

## What the fork adds

| Path | Purpose |
| --- | --- |
| `server/` | Python 3.14 stdlib HTTP server (no dependencies): static hosting + JSON APIs |
| `server/roster.py` | the fleet: agents, colours, home rooms, room keywords |
| `server/feed.py` | `kanban.db` → cubicle feed (status mapping, room routing) |
| `server/kanban_view.py` | `kanban.db` → the per-agent board view (read-only, capped, cached 5 s) |
| `server/dispatch.py` | "Check queue": signs in to the Hermes dashboard and asks for one dispatcher pass |
| `server/sources.py` | Mattermost, Beszel, Uptime Kuma, host stats, service probes (cached) |
| `server/office.py` | the HTTP endpoints (map below) |
| `public/themes/busybee.js` | the five-room mansion theme |
| `public/index.html` | upstream client + guarded theme hooks (`theme.plan`, `theme.pickDesk`, `theme.obstacles`, `cfg.theme`, …) |
| `deploy/portainer.py` | create/recreate/stop/logs for the container on the docker host |
| `test/feed_test.py` | `python3 -m unittest test.feed_test` |

## Data-endpoint map (what the page reads)

Everything is polled by the browser; the server never pushes.

| Endpoint | Source | Refresh | Auth |
| --- | --- | --- | --- |
| `GET /config.json` | this server (tells the page to poll `/api/feed/0`) | once | none |
| `GET /api/feed/0` (alias `/api/feed`) | `/hermes/kanban.db` (SQLite, read-only) + latest `#off-topic` post | 4 s (page), feed rebuilt ≤1 s | none |
| `GET /api/office/vitals` | host `/proc`, TCP/HTTP probes, Beszel, Uptime Kuma | 15 s server-side, 5 s page-side | server-side only |
| `GET /api/office/chat` | Mattermost REST | ≤1 req/min server-side, 20 s page-side | bot login, server-side only |
| `GET /api/office/kanban` (`?agent=<id>`) | `/hermes/kanban.db` (SQLite, **read-only**, `server/kanban_view.py`) | 12 s page-side, rebuilt ≤5 s server-side | none |
| `GET` / `POST /api/office/dispatch` | the Hermes dashboard at `:9119`, signed in server-side (`server/dispatch.py`) | on click; POST limited to one pass per 15 s | `x-cubicle: 1` header on POST |
| `GET /api/office/actions` | this server: the button contract (caps + `dispatch.available`) | on open, and after each action | none |
| `GET /healthz` | this server | container healthcheck | none |
| `GET /api/appearance`, `GET /api/cubicle/settings` | `{}` stubs (looks live in localStorage) | on open | none |

The office reads **kanban.db** for "status per assignee":

```
running task, live claim (claim_expires > now, or heartbeat < 15 min) -> running  (types at its desk)
blocked task                                                        -> error    (red: "BLOCKED: needs input")
ready/todo                                                          -> paused   (lounge, "zZ")
finished in the last 20 min                                         -> idle + completedAt ("✓ done")
otherwise                                                           -> idle     (wanders the lounge)
```

`default` / `hermes` / `orchestrator` kanban assignees are one person: the 🟡 orchestrator.
A running task can move an agent to a room other than its home room when the title matches a room
keyword (`server/roster.py:ROOM_KEYWORDS`) — that is the "walk to the room" behaviour.

## Running it

Deployed as container **`busybee-office`** on the homelab docker host (Portainer endpoint 3):

```
image:     python:3.14-slim, network agency-net, host port 9120 -> 9120, restart unless-stopped
/app    <- /opt/data/coder-workspaces/busybee-office   (this repo, read-only)
/hermes <- busybee-hermes-data volume                  (read-only: kanban.db + creds)
```

```bash
python3 deploy/portainer.py status      # running? ports? restarts?
python3 deploy/portainer.py recreate    # stop, delete, create, start (picks up new code)
python3 deploy/portainer.py logs 60     # container logs
```

The container survives reboots via its restart policy; the app keeps no state of its own.
Static assets are served `no-store` (only images cache for an hour), so a recreate is picked up
by a plain reload; the theme in use is `OFFICE_THEME` (default `busybee`).

Local development (no container):

```bash
OFFICE_PORT=9120 OFFICE_DB=/opt/data/kanban.db OFFICE_HERMES_DIR=/opt/data python3 -m server.office
python3 -m unittest test.feed_test       # feed + room routing tests
node --check public/themes/busybee.js    # theme syntax
```

## Credentials

Nothing secret is ever sent to the browser. The server reads, at boot, from `/hermes/creds`:

- `mattermost.json` — the `hermes` bot user logs in to poll presence and `#off-topic`
  (rate-limited to one request per minute).
- `beszel.json` (`{"user","password"}`) or `office.json` (`{"beszel":{...}}`) — optional; without
  it the panel falls back to host `/proc` + probes.
- `office.json` (`{"kuma_slug","kuma_token"}`) — optional; without it the panel notes that no
  status page is published and relies on the probes.

## License / provenance

Upstream code and assets: MIT, `LICENSE` unchanged, © 2026 Çağlar Utku Güler. Everything under
`server/`, `public/themes/busybee.js`, `deploy/`, `test/` and the `public/index.html` hooks is
BUSY BEE's own, MIT-licensed as part of this fork. See `NOTICE.md`.
