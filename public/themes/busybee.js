// Cubicle theme: BUSY BEE mansion. Five rooms (Coding, Game Dev, Research, Communications, Art)
// plus a lounge, in one wide panorama. Agents are seated at a desk in the room their work belongs
// to (the page's theme.pickDesk hook); idle agents wander the lounge; the overlay draws the
// homelab vitals and Mattermost presence. Everything is drawn in code, as upstream requires.
(() => {
  const K = window.CubicleKit, T = K.T;

  // ---- floor plan (tiles; the page multiplies by T) ----------------------------------------
  const ROOM_W = 7;                       // tiles per room
  // The desk row sits at 5.0 tiles, not the 3.5 it used to be: an agent's task+name chips hang
  // ~1.2 tiles over its head, and at 3.5 those chips landed on the wall plaque line (office y
  // 18..31) and grazed the room name on every load — the "labels clipped / misaligned" complaint.
  // One tile down puts the chips at y ~47..60, clear of the wall, and leaves the room furniture
  // (shelf, arcade, rack: y 41..86) behind the desks, which is what it is drawn to look like.
  const DESK_Y = 5.0;
  const ROOMS = [
    { id: 'coding', en: 'Coding', tr: 'Kodlama', accent: '#2f6fed', desks: [[0.5, DESK_Y], [4.4, DESK_Y]] },
    { id: 'gamedev', en: 'Game Dev', tr: 'Oyun', accent: '#8b5cf6', desks: [[7.5, DESK_Y], [11.4, DESK_Y]] },
    { id: 'research', en: 'Research', tr: 'Araştırma', accent: '#22c55e', desks: [[14.5, DESK_Y], [18.4, DESK_Y]] },
    { id: 'comms', en: 'Comms', tr: 'İletişim', accent: '#22d3ee', desks: [[21.5, DESK_Y], [25.4, DESK_Y]] },
    { id: 'art', en: 'Art', tr: 'Sanat', accent: '#eab308', desks: [[28.5, DESK_Y], [32.4, DESK_Y]] },
  ];
  const LOUNGE_X = 35;                    // tiles; x where the lounge starts
  const CW_TILES = 41;                    // 5 rooms + lounge
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
    for (const r of ROOMS) {
      for (const d of r.desks) { desks.push(d); DESK_ROOMS.set(dkey(d), r.id); }
    }
    for (let i = 0; i < 10; i++) {
      lounge.push([35.4 + (i % 5) * 1.05, 3.4 + Math.floor(i / 5) * 4.6 + (i % 2) * 0.8]);
    }
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

    // windows on the wall (two, over the work area)
    // The wall strip is y 0..WALL_TOP (32); everything an agent or its name chip occupies starts at
    // y 34 (measured: the two stacked chips over a desk at 3.5 tiles land at office y 34..48, exactly
    // where the plaques used to sit, so the room names were half covered on every load). So the wall
    // is stacked top-down with a unit of clearance each: windows 2..18, the company sign at 10
    // (centred between the windows, never over a plaque), room plaques 18..31, and nothing of ours
    // below that until the desks. The chips scale with the canvas, so clearing them at fit clears
    // them at every zoom.
    K.window(c, 1.5 * T, 2, 2.6 * T, 16, (cc, x, y, w, h) => K.skyline(cc, x, y, w, h, 3));
    K.window(c, 36.4 * T, 2, 2.6 * T, 16, (cc, x, y, w, h) => K.skyline(cc, x, y, w, h, 11));

    // company name on the wall, centred over the whole floor — between the two windows, above the
    // plaque line (y 10 with ~6-unit glyphs stays inside 2..17; the plaques start at 18)
    K.sign(c, CW / 2, 10, { color: '#d8b25a', size: 6 });

    // room plaques on the back wall of each room: the wall strip ends at WALL_TOP (32) and the
    // agents' name/task chips begin at y 34, so 18..31 is the only band the room name can live in
    // and still be readable on every load — measured, the old y 35..48 sat exactly under the chips.
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

    // corridor rug in front of the rooms
    K.rr('rgba(216,178,90,.14)', 6, 7.6 * T, 34 * T, 2.6 * T, 4);
    K.rect('rgba(216,178,90,.35)', 6, 7.6 * T, 34 * T, 1);
    K.rect('rgba(216,178,90,.35)', 6, 10.1 * T, 34 * T, 1);

    // per-room furniture (behind the desks, drawn with the room)
    const rx = (i) => i * ROOM_W * T;
    K.shelf(rx(0) + 2, 2.6 * T, 3.2 * T, 1.6 * T, { wood: '#4a3a28' });
    K.plant(rx(0) + 6.2 * T, 5.4 * T, { size: 1 });
    // Game Dev: arcade cabinet + a beanbag
    K.rr('#1d1230', rx(1) + 0.6 * T, 2.7 * T, 2.2 * T, 2.6 * T, 2);
    K.rect('#8b5cf6', rx(1) + 0.8 * T, 2.9 * T, 1.8 * T, 1.1 * T);
    K.rect('#0b0716', rx(1) + 0.8 * T, 4.0 * T, 1.8 * T, 0.5 * T);
    K.circle(Math.floor(c.t * 2) % 2 ? '#ff5a5a' : '#5fe08a', rx(1) + 1.0 * T, 4.2 * T, 0.8);
    K.ellipse('#3d2f63', rx(1) + 5.4 * T, 5.6 * T, 2.2, 1.2);
    // Research: server rack with blinking lights + a table of papers
    K.rr('#14171d', rx(2) + 0.4 * T, 2.6 * T, 2.4 * T, 2.8 * T, 1);
    for (let i = 0; i < 5; i++) {
      K.rect('#0a0d12', rx(2) + 0.55 * T, (2.75 + i * 0.52) * T, 2.1 * T, 0.4 * T);
      K.circle(Math.floor(c.t * 3 + i) % 3 ? '#5fe08a' : '#2a6a3a', rx(2) + 0.7 * T, (2.95 + i * 0.52) * T, 0.45);
    }
    K.table(rx(2) + 3.4 * T, 3.2 * T, 3 * T, 1.6 * T, { top: '#5b4430', edge: '#4a3727', chairCount: 2 });
    // Comms: whiteboard with post-its + a water cooler
    K.rr('#e8ecf1', rx(3) + 0.5 * T, 2.6 * T, 4 * T, 2 * T, 1);
    const notes = ['#ffd23f', '#ff8fa3', '#7fd1ff', '#9be89b'];
    for (let i = 0; i < 4; i++) K.rect(notes[i], rx(3) + (0.8 + (i % 2) * 1.6) * T, (2.9 + Math.floor(i / 2) * 0.8) * T, 0.9 * T, 0.7 * T);
    K.line('rgba(40,60,90,.6)', 0.4, rx(3) + 2.9 * T, 2.9 * T, rx(3) + 4.2 * T, 3.6 * T);
    K.waterCooler(rx(3) + 5.6 * T, 2.9 * T);
    // Art: easels with canvases
    for (let i = 0; i < 2; i++) {
      const ex = rx(4) + (0.9 + i * 2.6) * T, ey = 2.8 * T;
      K.line('#7a5a34', 0.6, ex - 1.4 * T / 2, ey + 2.6 * T, ex, ey);
      K.line('#7a5a34', 0.6, ex + 1.4 * T / 2, ey + 2.6 * T, ex, ey);
      K.rr('#f4f1ea', ex - 1.1 * T, ey + 0.3 * T, 2.2 * T, 1.9 * T, 0.8);
      K.poly(i ? '#2f6fed' : '#eab308', [[ex - 0.7 * T, ey + 1.8 * T], [ex - 0.1 * T, ey + 0.7 * T], [ex + 0.5 * T, ey + 1.8 * T]]);
      K.circle(i ? '#eab308' : '#22c55e', ex + 0.6 * T, ey + 0.9 * T, 0.35 * T);
    }

    // lounge: rug, plants, wall clock and the status board
    K.rr('rgba(245,158,11,.16)', (LOUNGE_X + 0.5) * T, 6.4 * T, (CW_TILES - LOUNGE_X - 1) * T, 4.4 * T, 5);
    K.plant((LOUNGE_X + 0.4) * T, 9.8 * T, { size: 1.3 });
    K.plant(CW - 0.6 * T, 9.8 * T, { size: 1.3 });
    K.clock(c, CW - 22, 4, { color: '#ff6b4a' });
    // The wall status board used to hang at y 4 with h 22, i.e. down to y 26 — right on top of the
    // room plaque under it, which is why "Game Dev" read as "…e Dev". Shrunk to stay above the
    // plaque line (plaques start at y 18); the four status bars still fit in the shorter frame.
    K.statusBoard(c, 150, 1.5, 16, 14, { frame: '#1b1f24', bg: '#05090c' });
  }

  function props(c, list) {
    K.begin(c);
    // lounge furniture sorts with the characters (z = y it stands on)
    list.push({ z: 8.4 * T, f: () => K.sofa((LOUNGE_X + 0.7) * T, 8.4 * T, 4.4 * T, { color: '#6a3f8f' }) });
    list.push({ z: 9.2 * T, f: () => K.table((LOUNGE_X + 2.6) * T, 9.2 * T, 2.6 * T, 1.2 * T, { top: '#5b4430', edge: '#4a3727', chairCount: 3 }) });
    list.push({ z: 9.6 * T, f: () => K.coffeeMachine(c, (LOUNGE_X + 4.9) * T, 9.4 * T) });
    list.push({ z: 9.8 * T, f: () => K.waterCooler((LOUNGE_X + 0.3) * T, 9.0 * T) });
    list.push({ z: 6.2 * T, f: () => K.plant((LOUNGE_X + 0.3) * T, 5.4 * T, { size: 1.1 }) });
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
    // The panels live in the empty corridor along the bottom, clear of the rooms and their labels.
    K.rr('rgba(6,8,12,.88)', 5, 156, 218, 46, 3);
    row(166, `HOMELAB  cpu ${host.cpu == null ? '--' : host.cpu + '%'}  mem ${host.mem == null ? '--' : host.mem + '%'}  disk ${host.disk == null ? '--' : host.disk + '%'}`, '#eef2fa');
    row(175.5, `services ${up}/${svcs.length} up   ${svcs.filter((s) => !s.up).map((s) => s.name).join(' ') || 'all green'}`, up === svcs.length ? '#7be3a0' : '#ffd23f');
    row(185, `beszel ${(vitals.beszel && vitals.beszel.systems || []).length} host(s)   kuma ${(vitals.kuma && vitals.kuma.monitors || []).length} monitor(s)`, '#aeb9cf');
    const post = chat && chat.posts && chat.posts[chat.posts.length - 1];
    if (post) row(194.5, `#off-topic ${post.user}: ${String(post.message).slice(0, 42)}`, '#8fd3ff');

    // Mattermost presence: one dot per agent, green when online
    const presence = (chat && chat.presence) || {};
    K.rr('rgba(6,8,12,.88)', CW - 140, 156, 135, 46, 3);
    K.text('MM', CW - 134, 166, { size: 8, color: '#aeb9cf', font: 'ui-monospace, Menlo, Consolas, monospace' });
    KEYS.forEach((id, i) => {
      const st = presence[AGENT_MM[id]] || 'offline';
      const col = st === 'online' ? '#5fe08a' : st === 'away' ? '#ffd23f' : st === 'dnd' ? '#ff5a5a' : '#5a6272';
      K.circle(col, CW - 106 - i * 13, 166, 3.4);
    });
    K.text('presence', CW - 134, 185, { size: 8, color: '#aeb9cf', font: 'ui-monospace, Menlo, Consolas, monospace' });
  }

  window.CubicleThemes.register({
    id: 'busybee',
    name: { en: 'BUSY BEE mansion', tr: 'BUSY BEE konağı' },
    scale: 4, smooth: true, allDesks: true, loungeX: LOUNGE_X,
    setup() {}, drawRoom, props, drawDesk, drawChar, overlay, plan, pickDesk,
    obstacles: () => boundaries.map((b) => [b - 4, WALL_TOP, b + 4, WALL_BOTTOM]),
    logoSpot: [CW / 2 - 70, 2, 140, 22],
  });
})();
