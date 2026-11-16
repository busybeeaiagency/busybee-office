// Cubicle theme: BUSY BEE mansion. Five rooms (Coding, Game Dev, Research, Communications, Art)
// plus a lounge, in one wide panorama. Agents are seated at a desk in the room their work belongs
// to (the page's theme.pickDesk hook); idle agents wander the lounge; the overlay draws the
// homelab vitals and Mattermost presence. Everything is drawn in code, as upstream requires.
(() => {
  const K = window.CubicleKit, T = K.T;

  // ---- floor plan (tiles; the page multiplies by T) ----------------------------------------
  // Rooms are 12.5 tiles so a room's two name chips can never touch: the chips are DOM boxes
  // measured at 40.6 css px half-width (11 px cap) / 29.5 px (8 px floor) at scale k = clientWidth/CW.
  // Desk offsets are room-LOCAL (plan() adds the room's own x). At 2.6 tiles the seated sprite sits
  // at 2.6*T + 22 = 63.6, so the worst-case task bubble (80 px cap = 125 px-office units at the
  // smallest fit scale k ~ 0.63) still clears the canvas edge by ~1 unit; at 8.1 the name-chip
  // right (151.6 + 51) stays inside the room's label lane and 10+ units clear of the next room's
  // desk chip. The 88-unit desk gap fits two typical 8 px name chips (2 x ~39 units); two patho-
  // logically long names at once is the known remainder, and overlapping task BUBBLES are
  // suppressed by placeLabels by design.
  const ROOM_W = 12.5;                    // tiles per room
  const DESK_Y = 5.6;
  // The desk pair is written room-LOCAL (identical for all five rooms); plan() shifts it by the
  // room's own x, since the kit paints a desk at its absolute tile x (d[0] * T).
  const ROOMS = [
    { id: 'coding', en: 'Coding', tr: 'Kodlama', accent: '#2f6fed', desks: [[2.6, DESK_Y], [8.1, DESK_Y]] },
    { id: 'gamedev', en: 'Game Dev', tr: 'Oyun', accent: '#8b5cf6', desks: [[2.6, DESK_Y], [8.1, DESK_Y]] },
    { id: 'research', en: 'Research', tr: 'Araştırma', accent: '#22c55e', desks: [[2.6, DESK_Y], [8.1, DESK_Y]] },
    { id: 'comms', en: 'Comms', tr: 'İletişim', accent: '#22d3ee', desks: [[2.6, DESK_Y], [8.1, DESK_Y]] },
    { id: 'art', en: 'Art', tr: 'Sanat', accent: '#eab308', desks: [[2.6, DESK_Y], [8.1, DESK_Y]] },
  ];
  const LOUNGE_X = 5 * ROOM_W;            // tiles; x where the lounge starts (62.5)
  const CW_TILES = 5 * ROOM_W + 9;        // 5 rooms + lounge (71.5)
  const CW = CW_TILES * T, CH = 13 * T;

  // desk tile -> room id, so the seat picker and the desk painter agree
  const DESK_ROOMS = new Map();
  const TAKEN = new Map();                // deskKey -> sprite id
  const dkey = (d) => `${d[0].toFixed(2)},${d[1].toFixed(2)}`;

  const AGENT_COLORS = {
    coder: '#2f6fed', coder2: '#8b5cf6', auditor: '#f59e0b',
    maya: '#22d3ee', researcher: '#22c55e', orchestrator: '#eab308',
  };
  const AGENT_MM = { coder: 'coder', coder2: 'coder2', auditor: 'auditor', maya: 'maya', researcher: 'researcher', orchestrator: 'hermes' };
  const KEYS = Object.keys(AGENT_COLORS).sort((a, b) => b.length - a.length);   // coder2 before coder

  // The page prefixes sprite ids with the feed ("/api/feed/0#coder") and the display name
  // carries the emoji ("🔵 coder"), so look for a known agent name in every field we have.
  function agentKey(s) {
    const fields = [s && s.id, s && s.realName, s && s.name, s && s.role].map((v) => String(v || '').toLowerCase());
    for (const key of KEYS) {
      if (fields.some((f) => f.includes(key))) return key;
    }
    return null;
  }
  const colorOf = (s) => AGENT_COLORS[agentKey(s)] || (s && s.shirt) || '#4f6f9c';

  // walls: one per room boundary, solid from the ceiling down to the corridor, so each room
  // reads as its own booth; agents walk between rooms around the wall ends.
  const WALL_TOP = 2 * T, WALL_BOTTOM = 6.4 * T;
  const boundaries = ROOMS.slice(1).map((r, i) => (i + 1) * ROOM_W * T).concat([LOUNGE_X * T]);

  function plan() {
    const desks = [];
    const lounge = [];
    DESK_ROOMS.clear();
    TAKEN.clear();
    ROOMS.forEach((r, i) => {
      // the pair is written room-local; the canvas needs the room's absolute tile x
      for (const [dx, dy] of r.desks) {
        const d = [i * ROOM_W + dx, dy];
        desks.push(d); DESK_ROOMS.set(dkey(d), r.id);
      }
    });
    // Idle loiter spots sit BELOW the desks' stacked chips and staggered between rooms, so an
    // idle agent's own chips (name + one zZ/☕ line ~36 px tall at k >= 0.6) never share a box
    // with a seated chip or with another loiterer: nearest neighbours are >= 88 px apart and
    // the lower row still clears the overlay panels at y 170.
    const loiter = [[11.1, 9.2], [36.1, 9.2], [60.5, 9.2], [23.6, 10.35], [48.6, 10.35], [66.3, 10.35]];
    for (const [lx, ly] of loiter) lounge.push([lx, ly]);
    const out = {
      id: 'mansion-v1', desks, lounge, CW, CH, wings: 1, rows: 1, OX: 0, loungeX: LOUNGE_X,
    };
    window.__busybee_plan = { CW: out.CW, CH: out.CH, desks: desks.length, lounge: lounge.length };
    return out;
  }

  function pickDesk(s, DESKS, a) {
    if (!s) return null;
    const prev = s._deskKey;
    if (prev && TAKEN.get(prev) === s.id) {
      const d = DESKS.find((x) => dkey(x) === prev);
      if (d) return d;
    }
    const room = s.room || (a && a.room);
    const free = DESKS.filter((d) => !TAKEN.has(dkey(d)));
    let pick = room ? free.find((d) => DESK_ROOMS.get(dkey(d)) === room) : null;
    if (!pick) pick = free[0] || null;
    if (pick) { const k = dkey(pick); TAKEN.set(k, s.id); s._deskKey = k; }
    return pick;
  }

  // ---- walls ------------------------------------------------------------------------------
  function drawWalls(c) {
    const g = c.g;
    for (const bx of boundaries) {
      // the wall itself
      K.rect('#2a2f3a', bx - 4, WALL_TOP, 8, WALL_BOTTOM - WALL_TOP);
      K.rect('rgba(255,255,255,.07)', bx - 4, WALL_TOP, 1.5, WALL_BOTTOM - WALL_TOP);
      // door frame at the corridor end
      K.rect('#4a3b28', bx - 6, WALL_BOTTOM - 1.5, 12, 2.5);
      K.rect('rgba(255,255,255,.05)', bx - 6, WALL_BOTTOM - 1.5, 12, 0.8);
    }
  }

  function drawRoom(c) {
    K.begin(c);
    // floor: slate in the work area, warm planks in the lounge
    K.floor(c, { work: ['#3b414d', '#363c47'], lounge: ['#5d4832', '#55422e'], split: LOUNGE_X * T, grout: 'rgba(0,0,0,.20)' });
    K.wall(c, { top: '#242a35', bottom: '#1a1f29', trim: 'rgba(0,0,0,.45)', band: 'rgba(255,255,255,.05)' });
    const rx = (i) => i * ROOM_W * T;      // room i's left edge, in office px

    // The wall strip is y 0..WALL_TOP (32) and the room plaques own 18..31, so above the plaques
    // there is one band — 0..17 — for everything else hung on the wall. Measured: an agent's stacked
    // name+task chips top out at desk_y*T + 12 - 60 = 38.4, so nothing of ours is drawn below y 17
    // until that band starts, and each row of the stack keeps its own clearance.
    // One window per room wall (the research wall carries the company sign instead), 112 px wide
    // with its 1.2 px frame: x 2.75..9.75 tiles inside a 12.5-tile room, y 2..18 including the frame.
    ROOMS.forEach((r, i) => {
      if (r.id === 'research') return;
      K.window(c, rx(i) + 2.75 * T, 2, 7 * T, 16, (cc, x, y, w, h) => K.skyline(cc, x, y, w, h, 3 + i));
    });

    // company name on the wall, centred on the research room (its middle is 2.5 rooms along, and
    // that wall has no window); y 12 with size-7 glyphs stays above the plaque line at 18
    K.sign(c, 2.5 * ROOM_W * T, 12, { color: '#d8b25a', size: 7 });

    // room plaques on the back wall of each room: the wall strip ends at WALL_TOP (32) and the
    // agents' worst-case stacked chips begin at y 38.4, so 18..31 is the band the room name lives in
    // — readable, and never under a chip.
    for (const r of ROOMS) {
      const x0 = ROOMS.indexOf(r) * ROOM_W * T;
      K.rr('#12151c', x0 + 10, 18, ROOM_W * T - 20, 13, 1.5);
      K.rect(r.accent, x0 + 10, 18, 2, 13);
      K.text(K.say(c, { en: r.en, tr: r.tr }), x0 + ROOM_W * T / 2 + 1, 25.8, { size: 6, align: 'center', color: '#e8ecf4' });
    }
    K.rr('#12151c', LOUNGE_X * T + 4, 18, CW - (LOUNGE_X * T) - 10, 13, 1.5);
    K.rect('#f59e0b', LOUNGE_X * T + 4, 18, 2, 13);
    K.text(K.say(c, { en: 'Lounge', tr: 'Dinlenme' }), LOUNGE_X * T + (CW - LOUNGE_X * T) / 2 + 1, 25.8, { size: 6, align: 'center', color: '#e8ecf4' });

    drawWalls(c);

    // corridor rug in front of the rooms: from the left edge to just before the lounge partition
    K.rr('rgba(216,178,90,.14)', 2 * T, 7.6 * T, (LOUNGE_X - 2.5) * T, 2.6 * T, 4);
    K.rect('rgba(216,178,90,.35)', 2 * T, 7.6 * T, (LOUNGE_X - 2.5) * T, 1);
    K.rect('rgba(216,178,90,.35)', 2 * T, 10.1 * T, (LOUNGE_X - 2.5) * T, 1);

    // per-room furniture (behind the desks, drawn with the room). Every relative x offset is 1.78x
    // the old 7-tile layout's (12.5/7) so the props spread across the wider rooms; each piece stays
    // inside x 0.4..12.1 tiles of its own room, clear of the room walls on both sides.
    K.shelf(rx(0) + 0.6 * T, 2.6 * T, 3.2 * T, 1.6 * T, { wood: '#4a3a28' });
    K.plant(rx(0) + 11.0 * T, 5.4 * T, { size: 1 });
    // Game Dev: arcade cabinet + a beanbag
    K.rr('#1d1230', rx(1) + 1.1 * T, 2.7 * T, 2.2 * T, 2.6 * T, 2);
    K.rect('#8b5cf6', rx(1) + 1.4 * T, 2.9 * T, 1.8 * T, 1.1 * T);
    K.rect('#0b0716', rx(1) + 1.4 * T, 4.0 * T, 1.8 * T, 0.5 * T);
    K.circle(Math.floor(c.t * 2) % 2 ? '#ff5a5a' : '#5fe08a', rx(1) + 1.8 * T, 4.2 * T, 0.8);
    K.ellipse('#3d2f63', rx(1) + 9.6 * T, 5.6 * T, 2.2, 1.2);
    // Research: server rack with blinking lights + a table of papers
    K.rr('#14171d', rx(2) + 0.7 * T, 2.6 * T, 2.4 * T, 2.8 * T, 1);
    for (let i = 0; i < 5; i++) {
      K.rect('#0a0d12', rx(2) + 1.0 * T, (2.75 + i * 0.52) * T, 2.1 * T, 0.4 * T);
      K.circle(Math.floor(c.t * 3 + i) % 3 ? '#5fe08a' : '#2a6a3a', rx(2) + 1.25 * T, (2.95 + i * 0.52) * T, 0.45);
    }
    K.table(rx(2) + 6.05 * T, 3.2 * T, 3 * T, 1.6 * T, { top: '#5b4430', edge: '#4a3727', chairCount: 2 });
    // Comms: whiteboard with post-its + a water cooler
    K.rr('#e8ecf1', rx(3) + 0.9 * T, 2.6 * T, 4 * T, 2 * T, 1);
    const notes = ['#ffd23f', '#ff8fa3', '#7fd1ff', '#9be89b'];
    for (let i = 0; i < 4; i++) K.rect(notes[i], rx(3) + (1.42 + (i % 2) * 2.85) * T, (2.9 + Math.floor(i / 2) * 0.8) * T, 0.9 * T, 0.7 * T);
    K.line('rgba(40,60,90,.6)', 0.4, rx(3) + 5.16 * T, 2.9 * T, rx(3) + 7.48 * T, 3.6 * T);
    K.waterCooler(rx(3) + 9.97 * T, 2.9 * T);
    // Art: easels with canvases
    for (let i = 0; i < 2; i++) {
      const ex = rx(4) + (1.6 + i * 4.63) * T, ey = 2.8 * T;
      K.line('#7a5a34', 0.6, ex - 1.4 * T / 2, ey + 2.6 * T, ex, ey);
      K.line('#7a5a34', 0.6, ex + 1.4 * T / 2, ey + 2.6 * T, ex, ey);
      K.rr('#f4f1ea', ex - 1.1 * T, ey + 0.3 * T, 2.2 * T, 1.9 * T, 0.8);
      K.poly(i ? '#2f6fed' : '#eab308', [[ex - 0.7 * T, ey + 1.8 * T], [ex - 0.1 * T, ey + 0.7 * T], [ex + 0.5 * T, ey + 1.8 * T]]);
      K.circle(i ? '#eab308' : '#22c55e', ex + 0.6 * T, ey + 0.9 * T, 0.35 * T);
    }

    // lounge: rug and plants on the floor; the clock and the status board hang on the lounge
    // stretch of wall (x >= LOUNGE_X * T) in the 0..17 band above the plaque line.
    K.rr('rgba(245,158,11,.16)', (LOUNGE_X + 0.6) * T, 6.6 * T, (CW_TILES - LOUNGE_X - 1.2) * T, 3.8 * T, 5);
    K.plant(CW - 0.9 * T, 10.4 * T, { size: 1.3 });
    K.clock(c, LOUNGE_X * T + 1.2 * T, 3);
    // h 14 + its 1.2 frame: y 1.5..16.7, entirely above the plaque line at y 18 — the old board
    // hung down to y 26, right over the room name under it.
    K.statusBoard(c, LOUNGE_X * T + 3.2 * T, 1.5, 3.4 * T, 14, { frame: '#1b1f24', bg: '#05090c' });
  }

  function props(c, list) {
    K.begin(c);
    // lounge furniture sorts with the characters (z = y it stands on)
    list.push({ z: 9.4 * T, f: () => K.sofa((LOUNGE_X + 1.0) * T, 9.4 * T, 4.4 * T, { color: '#6a3f8f' }) });
    list.push({ z: 9.8 * T, f: () => K.table((LOUNGE_X + 4.4) * T, 9.8 * T, 2.8 * T, 1.2 * T, { top: '#5b4430', edge: '#4a3727', chairCount: 3 }) });
    list.push({ z: 9.8 * T, f: () => K.coffeeMachine(c, (LOUNGE_X + 7.1) * T, 9.8 * T) });
    list.push({ z: 6.9 * T, f: () => K.waterCooler((LOUNGE_X + 0.5) * T, 6.9 * T) });
    list.push({ z: 6.0 * T, f: () => K.plant((LOUNGE_X + 0.4) * T, 6.0 * T, { size: 1.1 }) });
  }

  function drawDesk(c, d, s) {
    K.begin(c);
    const room = DESK_ROOMS.get(dkey(d));
    const accent = (ROOMS.find((r) => r.id === room) || {}).accent || '#8b93a8';
    K.desk(c, d, s, { top: [accent, '#2b303b'], screens: 2, on: '#8fe3ff', mug: room ? '#e8e3d6' : '#e8e3d6' });
  }

  function drawChar(c, s) {
    K.begin(c);
    const color = colorOf(s);
    K.person(c, s, {
      top: color, sleeve: color,
      hair: K.pick(s, 'hair', ['short', 'long', 'bun', 'pony', 'curly']),
      badge: color,
      tag: true,
      coat: agentKey(s) === 'orchestrator' ? '#2f3542' : null,
    });
    if (s.status === 'error' || s.status === 'waiting') {
      // blocked reads red: a ring under the character, whatever the kit's monitor does
      c.g.strokeStyle = s.status === 'error' ? 'rgba(255,80,80,.9)' : 'rgba(255,195,74,.9)';
      c.g.lineWidth = 0.9;
      c.g.beginPath(); c.g.ellipse(s.x, s.y, 6.5, 2.2, 0, 0, Math.PI * 2); c.g.stroke();
    }
  }

  // ---- overlay: homelab vitals + Mattermost presence --------------------------------------
  let vitals = null, chat = null;
  const startTimers = () => {
    if (startTimers.done) return;
    startTimers.done = true;
    const tick = (url, slot, ms) => {
      const load = () => fetch(url, { cache: 'no-store' }).then((r) => r.ok ? r.json() : null)
        .then((j) => { slot === 'vitals' ? (vitals = j) : (chat = j); })
        .catch(() => {});
      load();
      setInterval(load, ms);
    };
    tick('/api/office/vitals', 'vitals', 5000);
    tick('/api/office/chat', 'chat', 20000);
  };

  function overlay(c) {
    startTimers();
    if (!vitals) return;
    const host = vitals.host || {}, svcs = vitals.services || [];
    const up = svcs.filter((s) => s.up).length;
    const row = (y, text, color) => K.text(text, 8, y, { size: 8, color, font: 'ui-monospace, Menlo, Consolas, monospace' });
    // The panels live in the strip along the bottom of the canvas (y 170..204), under both rugs and
    // clear of every room, desk and label. Four rows at 8.5-unit pitch inside a 34-unit panel.
    K.rr('rgba(6,8,12,.88)', 8, 170, 480, 34, 3);
    row(177, `HOMELAB  cpu ${host.cpu == null ? '--' : host.cpu + '%'}  mem ${host.mem == null ? '--' : host.mem + '%'}  disk ${host.disk == null ? '--' : host.disk + '%'}`, '#eef2fa');
    row(185.5, `services ${up}/${svcs.length} up   ${svcs.filter((s) => !s.up).map((s) => s.name).join(' ') || 'all green'}`, up === svcs.length ? '#7be3a0' : '#ffd23f');
    row(194, `beszel ${(vitals.beszel && vitals.beszel.systems || []).length} host(s)   kuma ${(vitals.kuma && vitals.kuma.monitors || []).length} monitor(s)`, '#aeb9cf');
    const post = chat && chat.posts && chat.posts[chat.posts.length - 1];
    if (post) row(202.5, `#off-topic ${post.user}: ${String(post.message).slice(0, 42)}`, '#8fd3ff');

    // Mattermost presence: one dot per agent, left to right, green when online, plus the tally
    const presence = (chat && chat.presence) || {};
    const online = KEYS.filter((id) => (presence[AGENT_MM[id]] || 'offline') === 'online').length;
    K.rr('rgba(6,8,12,.88)', CW - 143, 170, 135, 34, 3);
    K.text('MM presence', CW - 137, 177, { size: 8, color: '#aeb9cf', font: 'ui-monospace, Menlo, Consolas, monospace' });
    KEYS.forEach((id, i) => {
      const st = presence[AGENT_MM[id]] || 'offline';
      const col = st === 'online' ? '#5fe08a' : st === 'away' ? '#ffd23f' : st === 'dnd' ? '#ff5a5a' : '#5a6272';
      K.circle(col, CW - 137 + i * 13, 186, 3.4);
    });
    K.text(`${online} of ${KEYS.length} online`, CW - 137, 197, { size: 8, color: '#aeb9cf', font: 'ui-monospace, Menlo, Consolas, monospace' });
  }

  window.CubicleThemes.register({
    id: 'busybee',
    name: { en: 'BUSY BEE mansion', tr: 'BUSY BEE konağı' },
    scale: 4, smooth: true, allDesks: true, loungeX: LOUNGE_X,
    setup() {}, drawRoom, props, drawDesk, drawChar, overlay, plan, pickDesk,
    obstacles: () => boundaries.map((b) => [b - 4, WALL_TOP, b + 4, WALL_BOTTOM]),
    // the user's logo hangs right of the status board (whose frame ends at CW - 2.34 * T)
    logoSpot: [CW - 2.2 * T, 2, 1.8 * T, 15],
  });
})();
