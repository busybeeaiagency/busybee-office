# Spec 2 — office UI: action buttons on the banner, honest counters, phone layout

File: `public/index.html` (one vendored single-file app, ~4900 lines, plain ES2019 JS + inline CSS,
no build step). The BUSY BEE theme is `public/themes/busybee.js`. Keep the codebase's voice:
dense comments that explain *why*, i18n strings come from the `L` tables (7 languages) — for NEW
user-visible strings add **English + Turkish** entries to both `L` tables and fall back to English
in code (`L.oAct || EN.oAct`) so a missing key never prints `undefined`.

The backend contract (already implemented in `server/feed.py` / `server/actions.py`; see
`docs/spec-actions-backend.md`) adds to each agent in `/api/feed/0`:

```json
"attention": {"task": {"id","title","kind","at","since"},
              "actions": [{"kind":"restart|approve|dismiss","label","why"}]},
"stale":     [{"id","title","kind","at"}]
```
and `status` is `waiting` for `needs_input` cards, `error` for crashed ones, `running` only for a
live claim. Routes: `GET /api/office/actions` (capability), `GET /api/office/confirm?action&task`
(one-shot confirm token), `POST /api/office/action` `{action, task, agent, note?, token}` with
header `x-cubicle: 1`.

Anchors below are from commit `d51aca0`; match on the quoted text, not the line number.

## 1. Plumb the new feed fields (do not rename existing ones)

- In `feedProvider`'s mapper, next to `errorReason: a.error || null, task, completedAt: a.completedAt || null,`
  add `attention: a.attention || null, stale: Array.isArray(a.stale) ? a.stale : [],`.
- In the sync loop, next to `s.error = a.errorReason || '';` add
  `s.attention = a.attention || null; s.stale = a.stale || [];` and make the waiting card's
  reason visible: when `a.status === 'waiting' && a.error` set `s.waitReason = a.error`
  (the line `s.waitReason = '';` already resets it every poll — set it *after* that line).

## 2. Operator buttons inside the error/waiting banner (`errorBox`, `function errorBox(s)`)

Today:
`if (!(s.status === 'error' && s.error) && !f) return '';` and it renders only text.

- Render when `s.status === 'error' || s.status === 'waiting'` and (`s.error` or `s.attention` or
  `f`). Give the box a class modifier so CSS can style waiting (amber) separately from error (red):
  `<div class="errbox ${s.status === 'waiting' ? 'warn' : ''}">`.
- Head line must name the **card id** so the banner and the task row can never disagree:
  `⚠ <what> · t_xxxxxxxx` where the id is `s.attention.task.id` (fall back to `s.task.identifier`).
- Body: the `error` text, then — when `s.attention.task.title` exists — the title in a second
  muted line with `title` attribute holding the full text (mobile wraps it, see §5).
- `f && f.stderr` (the Paperclip-only branch) stays as it is; it never appears for the feed source.
- Below the text, when `s.attention && s.attention.actions && s.attention.actions.length` and the
  server is writable (`window.__officeCaps && window.__officeCaps.writable`), render:
  `<div class="erracts">` + one `<button type="button" class="oact" data-oact="restart"
  data-task="t_x" data-agent="<s.id>" title="<why>">` per action, in the order the server sent
  them (`Restart task`, `Approve`, `Dismiss`). Labels come from the server (`a.label`) — that is
  where they are translated in one place. Buttons must be real `<button type="button">`, keyboard
  reachable, and never render when `data-task` is missing.
- Stale cards, if any: after the actions add one muted line
  `· 2 older crashed cards ignored (they were blocked before this agent's last finished work)`
  built from `s.stale.length` (+ i18n), each id shown as text only.

## 3. Click → confirm → token → POST (no write without confirm)

Add a delegated listener (attach it once next to the existing `.act` handling, search for
`else if (kind === 'run') openPaperclip(` to find the dispatcher):

```
click on button.oact:
  1. read kind = dataset.oact, task = dataset.task, agent = dataset.agent;
  2. if the button is already pending (dataset.busy) → ignore;
  3. GET /api/office/confirm?action=<kind>&task=<task>   (headers: {'x-cubicle':'1'})
       → {token, label, why}; on failure: show the reason in the status line and stop;
  4. confirm(`<label> <task>?\n\n<why>\n\nCard: <task> <title>`)   ← native confirm(),
       cancelled → NO request is sent at all (this is the acceptance rule);
  5. POST /api/office/action  {action, task, agent, token}  with
       {'content-type':'application/json','x-cubicle':'1'};
  6. while in flight: button disabled + text `…`; on 200 → set the status line to
       `✓ <label> <task>` , then force a feed refresh (call the same loader the Refresh
       button uses, or bump `lastFetch = 0` and let the next tick pull) so the badge changes
       within one poll; on !ok → put `message` (or `error`) in the status line AND in the
       button's `title`, and re-enable.
```
Show outcomes in `statusEl` (the header line) rather than `alert()`; keep any previous status text
by restoring it after 6 s. Never `eval` or innerHTML a server string — use `textContent`.

Fetch the capability once at startup into `window.__officeCaps`
(`GET /api/office/actions`, ignore failures → `{writable:false}`); when `writable` is false the
buttons render as one disabled note line `ⓘ actions disabled on this server`.

## 4. Counters must agree with the rows

Two places show counts and they currently contradict each other:

- header (`statusEl.textContent = [...]` with `L.summary(agents.length, running)`): extend to
  `N agents · M working · K waiting · E in error` (keep `L.summary` for the first two and append,
  do not break the other sources/Paperclip mode), and put the arithmetic in the element's `title`:
  `N = working + waiting + error + idle + paused · working = a task claimed right now,
  waiting = a card that needs your decision, error = a crashed run`.
- the Agents dialog summary (`W.sum` in the bottom inline script, and `sum.textContent`):
  count from the row statuses (`counts` already does) but *name the same words the rows use* and
  add the same `title` tooltip on `#ag-sum`, plus an explicit reconciliation line under the
  headline in small text: `= 2 working + 3 waiting + 0 in error + 1 idle` (build it from the same
  `counts`; when the numbers do not add up to `top.length` say `1 subagent counted separately`
  instead of silently dropping it).
- The per-row status cell keeps the pill + the `a.error` text. Because the backend now writes
  `CRASHED: …` for errors and `BLOCKED: …` for waiting, the pill word and the text can no longer
  disagree; add `title` on the pill with the same definition sentence as above.

## 5. Phone layout (acceptance: 390 × 844 viewport, Chrome mobile emulation)

All under `@media (max-width: 640px)` (match `narrow()`'s 640 px rule) unless stated:

a. **Status bar / header**: `body { padding-top: env(safe-area-inset-top); }` and
   `.wrap { padding: calc(10px + env(safe-area-inset-top)) 10px 24px; }` so the `h1`/`#status`
   line is fully below the phone status bar; `header { gap:6px; }` and
   `#status { flex: 1 1 100%; order: 3; font-size: 11px; }` so the long count line wraps under
   the title instead of being clipped by it.
b. **Agents dialog** (`#agents-panel`, `#ag-body`, the `<table>` built in `load()`):
   - dialog: `width: calc(100vw - 12px); max-height: 92vh; display: flex; flex-direction: column;`
     and `#ag-body { flex: 1 1 auto; min-height: 0; max-height: none; overflow: auto;
     overscroll-behavior: contain; -webkit-overflow-scrolling: touch; scrollbar-width: thin; }`
     (the current inline `max-height:70vh` is what makes row 6 disappear with no affordance).
   - the header row inside the dialog: `display:flex; flex-wrap:wrap; gap:8px; align-items:center;`
     and `#ag-sum { flex: 1 1 100%; font-size: 13px; }` so Refresh/Close never sit on top of the
     headline;
   - Refresh/Close: `min-height: 34px; padding: 6px 12px; border-radius: 8px;` with
     `gap: 10px` between them and `#agents-panel > div { padding-right: 4px; overflow: visible; }`
     plus `button:focus-visible { outline: 2px solid #9ab7ff; outline-offset: 2px; }` — the ring
     must be fully visible (that is an acceptance check).
   - the table becomes a stacked list: `#ag-body table { display:block; width:100%; }
     #ag-body tr { display:block; padding:8px 2px; border-top:1px solid #2a2a38; }
     #ag-body th { display:none; }` and each `td { display:block; }` — emit
     `data-l="<Column name>"` on every `td` in `load()` and show it with
     `#ag-body td[data-l]::before { content: attr(data-l); color:#9ab; margin-inline-end:6px; }`.
   - the `Working on` cell must stop truncating mid-word: give it
     `class="task"` and `display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
     overflow:hidden; word-break:break-word;` and set `title` to the full text (keep the id first,
     e.g. `t_4ff70d4c · Dec 12: Uninstall-or-keep Robin GitHub App (time-box G7)` — two lines with
     a real ellipsis, never `Uninstall-or-l…`).
   - affordance for hidden rows: after rendering, measure and set
     `#ag-body.dataset.more = scrollHeight > clientHeight + 8 ? 'N' : ''` and style
     `#ag-body[data-more]::after { content: '▼ ' attr(data-more) ' more below — scroll'; display:block;
     text-align:center; color:#9ab; padding:8px; position:sticky; bottom:0; }`
     (also refresh it on `resize` and on `scroll`, and clear it when scrolled to the end).
   - every row gets a visible action strip when the agent has `attention.actions`: the same three
     buttons (`Restart task` / `Approve` / `Dismiss`) so Bart can act from the list too. Reuse the
     §3 handler by emitting the same `button.oact` markup with the same data attributes (the
     dialog rows are built by the *inline bottom script* — it has its own `fetch`, keep it
     self-contained there and reuse `window.__officeAct` if you expose the §3 helper on `window`,
     which is the preferred shape: define it once in the main script, call it from both).
   - keep the table layout untouched above 640 px (the desktop modal must not regress).
c. **`Agents` floating button must not sit on the detail sheet** (`#agents-btn` is
   `position:fixed;right:12px;bottom:12px`, `#detail.sheet` is a bottom sheet up to `62vh`):
   under 640 px dock it to the top instead: `#agents-btn { top: calc(6px + env(safe-area-inset-top));
   right: 52px; bottom: auto; }` (52 px keeps it clear of the ⚙ button) and add
   `z-index: 60`. Also `#detail.sheet { max-height: 58vh; }` and `#detail.sheet .erracts {
   position: sticky; bottom: 0; }` so the buttons stay reachable while the card scrolls.
d. **Badges must say whose they are and sit on the right desk** (`placeLabels`, `labelFor`):
   - an agent whose `status` is `error` or `waiting` is pinned to its **desk**, not to wherever the
     slumped sprite ended up: in `placeLabels`, when `s.status === 'error' || s.status === 'waiting'`
     and `s.desk` exists, use `x = s.desk[0] * T (or the desk's centre the theme already uses)`,
     `y = s.desk[1] * T` for that label (check how `#detail`/`desks` positions are computed —
     `s.desk` comes from `theme.pickDesk` and the classic layout uses `DESKS[(idx+shift)%…]`; reuse
     whatever `deskRect`/`desks` maths the file already has rather than inventing a new mapping).
   - the pair (bubble + name) must never be split: give error/waiting rank `0` in the collision
     pass (currently `wait` is 1 and `err` is 2, so a name can survive while its badge is dropped,
     which is exactly the "floating ⚠ error, ambiguous whose it is" complaint), and never hide the
     `.name` of an attention agent: if the bubble cannot fit, shorten the bubble text (the task id
     only) instead of hiding the name.
   - the `⚠ error` bubble text for the feed source becomes `⚠ <task id>` (see `labelFor`:
     `if (s.status === 'error' && s.seated) return [L.errBubble, 'err'];` → include
     `s.attention?.task?.id || s.task?.identifier`), and the waiting bubble gets the id too, so an
     unlabeled floating `t_29cd784d`-style pill can always be read as "this agent, this card".
     Keep the hover `tip` behaviour (`b.title`) and set it to
     `<agent name> · <task id> · <first 120 chars of the task title>`.
e. Nothing may overlap at 390 px: verify with a screenshot that (i) the header count line is fully
   visible, (ii) the dialog's 6th row is reachable and the `▼ N more below` hint shows,
   (iii) Refresh/Close have a visible focus ring and are not touching, (iv) the `Agents` button does
   not cover the bottom card, (v) every error/waiting badge on the map is directly above a named
   desk.

## Constraints

- ES2019 syntax only in `index.html` (no optional chaining beyond what is already used, no
  modules); the bottom dialog script is a separate IIFE and must stay self-contained.
- Do not touch the 6 language tables you are not extending; adding a key means adding it to the
  English (`L`) and Turkish tables at minimum, and every read must tolerate `undefined`.
- No new network calls except the three `/api/office/*` routes. No polling changes.
- `node --check` is not applicable (it is HTML); instead run the repo's `test/smoke.js`
  (`node test/smoke.js`) if it exists and passes today, and re-run
  `python3 -m unittest discover -s test -v` at the end.
- Commit on the current branch with a clear message; do not push.
