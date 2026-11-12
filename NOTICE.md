# NOTICE

BUSY BEE animated office dashboard is based on the open-source project
**[cubicle](https://github.com/caglarutkuguler/cubicle)** by Çağlar Utku Güler, used under the
MIT License (see `LICENSE`). Upstream version: commit `b10185f` (2026-10-20).

Upstream components kept in this fork: `public/index.html`, `public/index.js`,
`public/themes/{kit,plaza,holding,warehouse,factory,space,military}.js`, `bin/`, `docs/`,
`examples/`, `paperclip/`, `plugin/`, and the upstream `README.md` (with a fork banner added).

BUSY BEE modifications (MIT, as part of this fork):

- `public/themes/busybee.js` — the five-room "BUSY BEE mansion" theme (new file).
- `public/index.html` — guarded theme hooks for custom floor plans (`theme.plan`,
  `theme.pickDesk`, `theme.allDesks`, `theme.loungeX`, `ROOM_*` state), room-aware desk seating,
  Mattermost chatter bubbles, and a wider room canvas. Every hook falls back to upstream
  behaviour for themes that do not implement it.
- `server/` — the Python stdlib server: kanban feed, Mattermost/Beszel/Uptime Kuma sources,
  host vitals, static hosting (new code).
- `deploy/portainer.py`, `test/feed_test.py`, `README-DASHBOARD.md`, `AGENTS.md` (new files).

No third-party art assets are used anywhere in the office: every sprite is drawn in code, as in
upstream cubicle.
