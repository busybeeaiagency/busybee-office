# TASK — finish the BUSY BEE office UI (one file: public/index.html)

Repo: /opt/data/coder2-workspaces/office-ui (branch feat/office-action-ui).
Read docs/spec-office-ui.md first — it is the contract. The backend is already live and serves
GET /api/feed/0 with per-agent `attention`, `stale`, `run`, `previous`, and these routes:
  GET  /api/office/actions   -> {"writable":bool,"reason":str|null,"actions":[...]}
  GET  /api/office/confirm?action=&task= -> {"token":"ct_...","preview":"Restart ..."}
  POST /api/office/action    body {"action","task","token","note"?}  header x-cubicle: 1
       -> 200 {"ok":true,"before","after","detail"} | 429 {"error":"rate_limited"} +Retry-After
       | 403/409 {"ok":false,"error":"no_token|bad_token|not_writable|not_in_expected_state","message"}
Read server/../docs/spec-actions-backend.md only if you need the exact response fields.

ALREADY DONE (uncommitted diff — do NOT redo or duplicate it): CSS for `.errbox.warn`, `.erracts`,
`.oact`, `.runline`, `details.prev`, `#agents-panel`/`#ag-body` mobile rules and the whole
`@media (max-width: 640px)` block; EN + TR strings (summaryMore, countTitle, actionsDisabled,
staleIgnored, runAlive, runDead, runAttempt, prevAttempts, prevSteps, runSecond/Minute/Hour);
`const EN = STR.en;`; and the feed parser that now copies `attention`, `stale`, `run`, `previous`
onto each agent object. DO NOT add new CSS unless a piece below truly needs it, and do not touch
server/, test/, docs/.

IMPLEMENT (JS, in the existing code style — plain ES2019, no build step):

1. `officeAct(kind, agent)` + the buttons. In the agent detail card's error/waiting banner build
   `<div class="erracts">` with one `<button class="oact">` per entry of `agent.attention.actions`
   (labels: L/EN `actRestart`/`actApprove`/`actDismiss` if present, else the backend's `label`; add
   those three keys to BOTH language tables with sensible text: Restart / Approve / Dismiss and
   Yeniden başlat / Onayla / Yoksay). Same buttons in that agent's row in the Agents dialog
   (`#ag-body`, inside `.agacts`).
2. `doOfficeAction(kind, agent, btn)`:
   a. GET /api/office/confirm?action=..&task=.. (x-cubicle: 1) and take `token` + `preview`.
   b. Show the preview text to the user and require an explicit confirmation before any POST
      (a small in-page overlay/confirm with OK + Cancel; native confirm() is acceptable ONLY if
      it shows the preview text and names the task id). Cancel => NO POST at all.
   c. Approve additionally asks for an optional one-line note (input) and sends it as `note`.
   d. POST /api/office/action with the token; disable the buttons while pending (`disabled`,
      aria-busy). On 200: brief success line, then trigger the existing feed refresh. On 429:
      show "slow down — retry in Ns" using Retry-After. On 403/409: show the server's `message`.
      Never POST without a token, never auto-retry a POST.
3. Capability: fetch /api/office/actions with the feed poll; when `writable` is false, render
   `L.actionsDisabled` text instead of buttons (and no POST possible). Cache the result per poll.
4. Detail-card banner content: title from `L` (needsYouTitle or existing error title) + the
   attention task id/title (`<code>`), the block kind, and how long it has been blocked
   (`attention.task.since`). Use `.errbox.warn` when `attention.task.kind !== 'crashed'`, plain
   red `.errbox` when it is crashed. A crashed-and-dismissed card must show nothing.
5. Live-run line: when `agent.run` exists, render `.runline` with `L.runAlive(since, hb)` /
   `L.runDead(...)` (humanise the ages with the existing relative-time helper, `L.runSecond` etc.)
   and `L.runAttempt(run.attempt, run.attempts)`. When `agent.previous` is non-empty, render
   `details.prev` with `L.prevAttempts(n)` as summary and each attempt's steps in an `<ol>`.
6. Agents dialog rows: the blocked/attention card goes in `.blk` (amber line, names the task id),
   the live run line under it, and set `data-more` on `#ag-body` to the number of rows below the
   fold so the CSS `::after` affordance appears (0/empty removes the attribute). Give the table
   cells the `data-l` labels the mobile CSS expects.
7. Header counters: make them add up — working + waiting + error + idle + paused = total agents —
   using `L.summaryMore(w, e)` for the summary and `L.countTitle` as the `title` tooltip. Counting
   comes from the feed statuses only; one agent counts once.

VERIFY before committing:
  node /opt/data/kanban/workspaces/t_2bb03acb/check_html_js.js public/index.html   # must be 0
  python3 -m http.server not needed; do not start servers.
Then commit everything as author coder2 <coder2@busybee.local> with message
"Office UI: operator action buttons on the banner, live-run line, folded attempts, 390px layout".
Write code first, verify second. Reply with the commit hash and one line per numbered item above
saying where it landed (file:line).
