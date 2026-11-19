// Runs inside TH.GL's map page (injected by overlay.py into every frame).
// Takes commands from our overlay UI through postMessage:
//   {aao: 'bars', hidden: true|false}   hide TH.GL's top header
//   {aao: 'autozoom', on: true|false}   smart zoom
//   {aao: 'autodiscover', on: true|false, radius: metres}  mark pins you stand on for 3 s
//   {aao: 'target', keys: [...], label}   point at the nearest unmarked pin of these types
//   {aao: 'setFilters', keys: [...]}      switch TH.GL's filters (activity profiles)
//   {aao: 'getFilters'}                   reply {aao:'filters', keys} with the current filters
//   {aao: 'rotate', on}                   rotate the map with your facing (round minimap)
//   {aao: 'lowpower', on}                 draw the map at ~8 fps while nothing moves
// and reports back: discStatus, target, progress, marked, walked, filters.
//
// Smart zoom: TH.GL's map follows your character, so the map centre is you.
// Every ~0.8 s it measures how far the nearest visible pins are from the centre
// and zooms so those pins fill about a third of the window. Tight clusters
// (gatherables, collectibles in one spot) zoom in hard; open areas zoom out.
// It only acts while your character is moving: standing still (looking around,
// planning a route) never changes the zoom. Scrolling or dragging the map
// yourself pauses it for 10 seconds, and the overlay turns it off while maximized.
(() => {
  if (!/(^|\.)th\.gl$/.test(location.hostname) || window.__aaoTools) return;
  window.__aaoTools = true;

  const NEAREST = 6;          // how many nearby pins define "the cluster"
  const FILL = 0.33;          // cluster radius as a share of the smaller window side
  const MIN_Z = 0.5;          // never zoom out past this
  const MAX_FROM_TOP = 1.5;   // stay this far below the map's max zoom
  const DEADBAND = 0.3;       // ignore smaller zoom changes (no jitter)
  const MAX_STEP = 0.75;      // most zoom change per tick (smooth)
  const PAUSE_MS = 10000;     // pause after you zoom or drag yourself
  const MOVE_CM = 150;        // you count as moving after going 1.5 m...
  const MOVING_MS = 2500;     // ...and stay "moving" this long after the last step

  let autoOn = false, pausedUntil = 0, timer = 0, ctx = null, anchor = null, movedAt = 0, zoomBias = 0, touchedAt = 0;

  // ---- clean look: no header; TH.GL's floating map controls only while hovered ----
  // (and never while the overlay is locked or its bars are hidden with Ctrl+Alt+H)
  const cleanCss = document.createElement('style');
  cleanCss.textContent = 'header.fixed{display:none!important}:root{--header-h:0px!important}'
    + 'div.fixed.z-500{transition:opacity .15s}'
    + 'html.aao-clean div.fixed.z-500{opacity:0!important;pointer-events:none!important}'
    // locked: the map ignores the mouse entirely, so nothing opens or highlights on hover
    + 'html.aao-locked,html.aao-locked *{pointer-events:none!important}'
    + 'html.aao-locked body>div.bg-popover{display:none!important}' // marker hover cards
    // TH.GL's "Update Ready!" tooltip: the overlay shows its own bar at the bottom instead
    + '[data-testid=new-version-label],[data-radix-popper-content-wrapper]:has([data-testid=new-version-label]){display:none!important}';
  let noHover = false, hovered = false, hoverTimer = 0;
  function updateChrome() {
    if (!document.documentElement) return;
    if (!cleanCss.isConnected && document.head) document.head.appendChild(cleanCss); // the page builds <head> after this script starts
    document.documentElement.classList.toggle('aao-clean', noHover || !hovered);
  }
  document.addEventListener('DOMContentLoaded', updateChrome);
  setInterval(() => { if (!cleanCss.isConnected) updateChrome(); }, 1000); // re-add if the page swaps <head>
  document.addEventListener('pointermove', () => { clearTimeout(hoverTimer); if (!hovered) { hovered = true; updateChrome(); } }, {passive: true});
  // the pointer left this page (into the overlay's own bars, or out of the window)
  document.addEventListener('mouseout', e => {
    if (e.relatedTarget) return;
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => { hovered = false; updateChrome(); }, 400);
  });
  function setBars(hidden) {
    noHover = !!hidden;
    updateChrome();
    window.dispatchEvent(new Event('resize')); // let the map canvas refit
  }

  // ---- locked: map only. Close the marker card and hover highlight, ignore the mouse ----
  let lockedNow = false;
  function applyLockState(on) {
    lockedNow = !!on;
    if (document.documentElement) document.documentElement.classList.toggle('aao-locked', lockedNow);
    if (!lockedNow) return;
    const sel = walkFibers(s => s && typeof s === 'object' && typeof s.setSelectedNodeId === 'function' ? s : null);
    if (sel) {
      if (sel.selectedNodeId) sel.setSelectedNodeId(null);
      if (sel.selectedZone && sel.setSelectedZone) sel.setSelectedZone(null);
    }
    const m = findMap();
    if (m && m.markers.hoveredMarkerId != null) m.markers.hoveredMarkerId = null;
    closeHoverCards();
  }
  // TH.GL's marker card (name, type, coords, Codex link) lives in the map component's own
  // state ({x, y, radius, items}), not in the selection store; clear it there.
  function closeHoverCards() {
    for (const el of document.querySelectorAll('body>div.bg-popover')) {
      const k = Object.keys(el).find(k => k.startsWith('__reactFiber'));
      for (let f = k && el[k], i = 0; f && i < 6; f = f.return, i++) {
        for (let h = f.memoizedState, n = 0; h && typeof h === 'object' && 'next' in h && n < 40; h = h.next, n++) {
          const v = h.memoizedState;
          if (v && typeof v === 'object' && 'items' in v && 'radius' in v && h.queue && h.queue.dispatch) { h.queue.dispatch(null); i = 99; break; }
        }
      }
    }
  }
  document.addEventListener('DOMContentLoaded', () => applyLockState(lockedNow));

  // ---- locking closes TH.GL's Filters panel; unlocking reopens it if it was open ----
  let filtersWereOpen = false;
  function setLocked(on) {
    const st = walkFibers(s => s && typeof s === 'object' && typeof s.toggleShowFilters === 'function' ? s : null);
    if (!st) return;
    if (on) { filtersWereOpen = !!st.showFilters; if (st.showFilters) st.toggleShowFilters(); }
    else if (filtersWereOpen && !st.showFilters) { st.toggleShowFilters(); filtersWereOpen = false; }
  }

  // ---- find TH.GL's map engine through React's internals ----------------------
  function findMap() {
    const canvas = document.querySelector('canvas');
    if (ctx && ctx.webmap && ctx.canvas === canvas && canvas && canvas.isConnected) return ctx;
    ctx = null;
    if (!canvas) return null;
    for (let el = canvas.parentElement, i = 0; el && i < 3 && !ctx; el = el.parentElement, i++) {
      const fk = Object.keys(el).find(k => k.startsWith('__reactFiber'));
      for (let f = fk && el[fk], j = 0; f && j < 8 && !ctx; f = f.return, j++) {
        for (let h = f.memoizedState, n = 0; h && n < 40; h = h.next, n++) {
          const cur = h.memoizedState && h.memoizedState.current;
          if (cur && cur.webmap && cur.markerLayer && typeof cur.webmap.getZoom === 'function') {
            ctx = {webmap: cur.webmap, markers: cur.markerLayer, canvas}; break;
          }
        }
      }
    }
    if (ctx) hookUserInput(canvas);
    return ctx;
  }

  function hookUserInput(canvas) {
    if (canvas.__aaoHooked) return;
    canvas.__aaoHooked = true;
    const pause = () => { pausedUntil = Date.now() + PAUSE_MS; touchedAt = Date.now(); };
    canvas.addEventListener('wheel', pause, {passive: true});
    canvas.addEventListener('pointerdown', pause);
    canvas.addEventListener('pointermove', () => { touchedAt = Date.now(); }, {passive: true});
  }

  // ---- smart zoom -------------------------------------------------------------
  function tick() {
    if (!autoOn || Date.now() < pausedUntil || document.hidden) return;
    // only while your character moves (tracked by the companion loop below)
    if (Date.now() - movedAt > MOVING_MS) return;
    const m = findMap();
    if (!m) return;
    const w = m.webmap;
    let c;
    try { c = w.projectAt(w.center, 0); } catch (e) { return; }
    if (!c || !Number.isFinite(c.x)) return;

    // distances (in zoom-0 map pixels) to the nearest few pins still to discover;
    // discovered ones are skipped so the view widens to show what is left around you
    const best = [];
    for (const p of m.markers.getInstances()) {
      if (m.markers.isHidden(p) || !p.latLng || p.isDiscovered) continue;
      // zoom-0 map position; cached per pin, refreshed if the pin moves
      let q = p.__aaoPos;
      if (!q || q.ll !== p.latLng || q.a !== p.latLng[0] || q.b !== p.latLng[1]) {
        let r; try { r = w.projectAt(p.latLng, 0); } catch (e) { continue; }
        if (!r || !Number.isFinite(r.x)) continue;
        q = p.__aaoPos = {ll: p.latLng, a: p.latLng[0], b: p.latLng[1], x: r.x, y: r.y};
      }
      const d = Math.hypot(q.x - c.x, q.y - c.y);
      if (best.length < NEAREST) { best.push(d); best.sort((a, b) => a - b); }
      else if (d < best[NEAREST - 1]) { best[NEAREST - 1] = d; best.sort((a, b) => a - b); }
    }
    if (best.length < 2) return;

    const spread = Math.max(best[best.length - 1], 0.02);
    const side = Math.min(m.canvas.clientWidth, m.canvas.clientHeight) || 400;
    const maxZ = w.getMaxZoom() - MAX_FROM_TOP;
    let z = Math.log2((side * FILL) / spread) + zoomBias;   // screen px = map px * 2^zoom; profiles nudge it
    z = Math.max(MIN_Z, Math.min(maxZ, z));

    const now = w.targetZoom !== undefined ? w.targetZoom : w.getZoom();
    window.__aaoLast = {spread, want: z, now, at: Date.now()}; // for troubleshooting
    if (Math.abs(z - now) < DEADBAND) return;
    const next = now + Math.max(-MAX_STEP, Math.min(MAX_STEP, z - now));
    w.targetZoom = next; // TH.GL's engine animates to targetZoom
  }

  // ---- auto discover: stand on a pin for 2 s and it is marked discovered ----
  // Uses your live position from the TH.GL app (Peer Link) and TH.GL's own
  // "auto discovered" list, so it shows and syncs like the app's own marks.
  const DWELL_MS = 2000;     // how long you must stay on it
  let RADIUS = 800;          // game units (cm): within 8 m counts as "on it"; set from the overlay's top bar
  let discoverOn = false, discTimer = 0, peersRef = null, markFn = null, meId = '', meAt = 0, lastStatus = 0, peersSearchAt = 0;
  const near = new Map();    // pin id -> time you came within RADIUS
  const moved = new Set();   // pins that move (monsters etc.) are never marked

  function walkFibers(test) {
    let start = null;
    for (const el of document.querySelectorAll('body *')) {
      const k = Object.keys(el).find(k => k.startsWith('__reactFiber'));
      if (k) { start = el[k]; break; }
    }
    if (!start) return null;
    let root = start; while (root.return) root = root.return;
    let found = null, n = 0;
    (function walk(f) {
      while (f && !found && n < 80000) {
        n++;
        for (let h = f.memoizedState, i = 0; h && typeof h === 'object' && i < 50 && !found; h = h.next, i++) {
          found = test(h.memoizedState) || null;
        }
        if (!found) walk(f.child);
        f = f.sibling;
      }
    })(root);
    return found;
  }

  function myPosition() {
    if ((!peersRef || !Object.values(peersRef).some(v => v && v.player)) && Date.now() - peersSearchAt > 2000) {
      peersSearchAt = Date.now(); // searching the whole page is slow: at most every 2 s
      peersRef = walkFibers(s => {
        const c = s && s.current;
        return c && typeof c === 'object' && !Array.isArray(c) &&
          Object.values(c).some(v => v && v.player && Number.isFinite(v.player.x)) ? c : null;
      });
    }
    if (!peersRef) return null;
    if (Date.now() - meAt > 5000) { // which app is "Me" (re-read every 5 s)
      meAt = Date.now();
      try {
        const st = JSON.parse(localStorage.getItem('settings-storage')).state;
        const prof = st.profiles.find(p => p.id === st.currentProfileId) || st.profiles[0];
        meId = prof.settings.lastMeSenderId || '';
      } catch (e) { meId = ''; /* fall back to the only app */ }
    }
    const me = meId;
    const entry = (me && peersRef[me]) || Object.values(peersRef).find(v => v && v.player);
    const p = entry && entry.player;
    return p && Number.isFinite(p.x) ? p : null;
  }

  function markDiscovered(ids) {
    if (!markFn) markFn = walkFibers(s => s && typeof s === 'object' && typeof s.markAutoDiscovered === 'function' ? s.markAutoDiscovered : null);
    if (markFn) markFn(ids); // TH.GL expects a list of pin ids
  }

  function discoverTick() {
    if (!discoverOn) return;
    const m = findMap();
    const me = m && myPosition();
    if (!me) { near.clear(); return; }
    const now = Date.now(), seen = new Set(), done = [];
    let best = null, bestD2 = Infinity;
    for (const p of m.markers.getInstances()) {
      if (!p.latLng || p.isDiscovered || moved.has(p.id) || m.markers.isHidden(p)) continue;
      if (/^(mob-|quest-|__center)/.test(p.key || '')) continue; // boss spawns and quest givers aren't things you collect
      // remember where each pin started; anything that moves is a live actor, not a node
      if (p.__aaoHome === undefined) p.__aaoHome = p.latLng[0] + ',' + p.latLng[1];
      else if (p.__aaoHome !== p.latLng[0] + ',' + p.latLng[1]) { moved.add(p.id); near.delete(p.id); continue; }
      const dx = p.latLng[0] - me.x, dy = p.latLng[1] - me.y, d2 = dx * dx + dy * dy;
      if (d2 < bestD2) { bestD2 = d2; best = p; }
      if (d2 > RADIUS * RADIUS) continue;
      seen.add(p.id);
      if (!near.has(p.id)) near.set(p.id, now);
      else if (now - near.get(p.id) >= DWELL_MS) done.push(p.id);
    }
    for (const id of near.keys()) if (!seen.has(id)) near.delete(id); // walked away: start over
    // tell the overlay what we're tracking (shown in its footer)
    if (now - lastStatus > 400 && window.parent !== window) {
      lastStatus = now;
      const held = best && near.has(best.id) ? now - near.get(best.id) : 0;
      window.parent.postMessage({aao: 'discStatus', d: best ? Math.sqrt(bestD2) / 100 : null,
        key: best ? best.key : '', id: best ? best.id : '', held, need: DWELL_MS}, '*');
    }
    if (done.length) {
      try {
        markDiscovered(done);
        const keys = done.map(id => id.split('@')[0]);
        if (window.parent !== window) window.parent.postMessage({aao: 'marked', keys}, '*');
      } catch (e) { /* TH.GL changed; try again next time */ markFn = null; }
      done.forEach(id => near.delete(id));
    }
  }

  window.__aaoDisc = {near, myPosition, radius: () => RADIUS, findMark: () => markDiscovered.length && (markFn || walkFibers(s => s && typeof s === 'object' && typeof s.markAutoDiscovered === 'function' ? s.markAutoDiscovered : null))}; // troubleshooting

  function setDiscover(on) {
    discoverOn = !!on;
    clearInterval(discTimer);
    near.clear();
    if (discoverOn) discTimer = setInterval(discoverTick, 250);
  }

  function setAuto(on) {
    autoOn = !!on;
    clearInterval(timer);
    if (autoOn) { pausedUntil = 0; timer = setInterval(tick, 500); tick(); }
  }


  // ---- companion loop: movement, walked distance, target pointer, progress ---------
  let targetKeys = [], targetLabel = '', walkedCm = 0, lastPos = null, lastProgress = 0, guide = null;
  const EDGE_COUNT = 6;      // edge markers: the nearest few targets beyond the map's edge
  let edgeTargets = [];      // [{to: latLng, d: metres}], nearest first
  let testHoldUntil = 0;
  const targetMatch = key => !!key && targetKeys.some(k => k.endsWith('*') ? key.startsWith(k.slice(0, -1)) : k === key);
  function findStore(test) { return walkFibers(s => s && typeof s === 'object' && test(s) ? s : null); }

  // where a map position appears on screen relative to another (works when the map is rotated)
  function screenOffset(w, fromLL, toLL) {
    const cw = w.canvas.clientWidth || 300, ch = w.canvas.clientHeight || 300;
    const ll = (x, y) => { const r = w.screenToLatLng(x, y); return Array.isArray(r) ? r : [r.lat, r.lng]; };
    const c = ll(cw / 2, ch / 2), u = ll(cw / 2, ch / 2 - 100), r = ll(cw / 2 + 100, ch / 2);
    const ux = u[0] - c[0], uy = u[1] - c[1], rx = r[0] - c[0], ry = r[1] - c[1];
    const tx = toLL[0] - fromLL[0], ty = toLL[1] - fromLL[1], det = rx * uy - ry * ux;
    if (!det) return null;
    const a = (tx * uy - ty * ux) / det, b = (rx * ty - ry * tx) / det; // target = a*right + b*up
    return {x: a * 100, y: -b * 100};
  }
  const COMPASS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];

  function companionTick() {
    const me = myPosition(), now = Date.now();
    if (me) {
      if (!anchor || Math.hypot(me.x - anchor.x, me.y - anchor.y) > MOVE_CM) { if (anchor) movedAt = now; anchor = {x: me.x, y: me.y}; }
      if (lastPos) {
        const step = Math.hypot(me.x - lastPos.x, me.y - lastPos.y);
        if (step > 30 && step < 5000) walkedCm += step; // ignore jitter and teleports
      }
      lastPos = {x: me.x, y: me.y};
      if (walkedCm > 2000 && window.parent !== window) { window.parent.postMessage({aao: 'walked', m: walkedCm / 100}, '*'); walkedCm = 0; }
    }
    const m = findMap();
    if (!m || window.parent === window) return;

    // nearest unmarked pin of the chosen types
    if (now < testHoldUntil) { /* troubleshooting guide is showing */ }
    else if (targetKeys.length) {
      let best = null, bd = Infinity;
      const near = []; // nearest few, for the edge markers
      if (me) for (const p of m.markers.getInstances()) {
        if (!p.latLng || p.isDiscovered || !targetMatch(p.key) || m.markers.isHidden(p)) continue;
        const d = Math.hypot(p.latLng[0] - me.x, p.latLng[1] - me.y);
        if (d < bd) { bd = d; best = p; }
        if (near.length < 24 || d < near[near.length - 1].d) {
          near.push({to: p.latLng, d}); near.sort((a, b) => a.d - b.d); if (near.length > 24) near.pop();
        }
      }
      edgeTargets = near.map(t => ({to: t.to, d: t.d / 100}));
      if (best) {
        let angle = 0, compass = '';
        try {
          const a = m.webmap.projectAt([me.x, me.y], 0), b = m.webmap.projectAt(best.latLng, 0);
          compass = COMPASS[Math.round(((Math.atan2(b.x - a.x, a.y - b.y) * 180 / Math.PI) + 360) % 360 / 45) % 8];
          const off = screenOffset(m.webmap, [me.x, me.y], best.latLng);
          if (off) angle = Math.atan2(off.x, -off.y) * 180 / Math.PI;
        } catch (e) { /* projection not ready */ }
        window.parent.postMessage({aao: 'target', label: targetLabel, key: best.key, d: bd / 100, angle, compass}, '*');
        guide = {me: [me.x, me.y], to: best.latLng, d: bd / 100};
      } else {
        window.parent.postMessage({aao: 'target', label: targetLabel, none: true, live: !!me}, '*');
        guide = null;
      }
    } else {
      guide = null;
    }
    if (!guide && now >= testHoldUntil) edgeTargets = [];

    // collection progress for the pin types on this map (every 3 s)
    if (now - lastProgress > 3000) {
      lastProgress = now;
      const counts = {};
      for (const p of m.markers.getInstances()) {
        if (!p.key || /^(mob-|quest-|__center|station-|housing-)/.test(p.key)) continue;
        const c = counts[p.key] || (counts[p.key] = [0, 0]);
        c[1]++; if (p.isDiscovered) c[0]++;
      }
      window.parent.postMessage({aao: 'progress', counts}, '*');
    }
  }
  setInterval(companionTick, 500);

  // ---- objective guide: dashed line from you to the target, arrow at the edge if off-screen ----
  const NS = 'http://www.w3.org/2000/svg';
  let svg = null;
  function guideSvg() {
    if (svg && svg.isConnected) return svg;
    svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('style', 'position:fixed;inset:0;width:100vw;height:100vh;pointer-events:none;z-index:400');
    svg.innerHTML = '<defs><filter id="aaoglow"><feDropShadow dx="0" dy="0" stdDeviation="1.5" flood-color="#000" flood-opacity=".9"/></filter></defs>'
      + '<g filter="url(#aaoglow)"><line id="aaoline" stroke="#ff8a4c" stroke-width="3" stroke-dasharray="9 7" stroke-linecap="round"/>'
      + '<circle id="aaoring" r="13" fill="none" stroke="#ff8a4c" stroke-width="3"/>'
      + '<path id="aaoedge" fill="#ff8a4c" stroke="#1b1209" stroke-width="1.5"/>'
      + '<text id="aaolabel" fill="#fff" font-size="12" font-weight="600" font-family="Segoe UI,sans-serif" text-anchor="middle"></text></g>'
      + '<g id="aaopips" filter="url(#aaoglow)" font-family="Segoe UI,sans-serif" font-size="10" font-weight="600" text-anchor="middle"></g>';
    document.body.appendChild(svg);
    return svg;
  }
  function drawGuide() {
    const m = ctx && ctx.webmap ? ctx : findMap();
    if (!guide || !m) { if (svg) svg.style.display = 'none'; return; }
    const g = guideSvg(); g.style.display = '';
    const w = m.webmap, rect = m.canvas.getBoundingClientRect();
    const cw = m.canvas.clientWidth, ch = m.canvas.clientHeight;
    let a, b, cLL;
    try {
      const r = w.screenToLatLng(cw / 2, ch / 2); cLL = Array.isArray(r) ? r : [r.lat, r.lng];
      a = screenOffset(w, cLL, guide.me); b = screenOffset(w, cLL, guide.to);
    } catch (e) { g.style.display = 'none'; return; }
    if (!a || !b) { g.style.display = 'none'; return; }
    const ox = rect.left + cw / 2, oy = rect.top + ch / 2;
    const x1 = ox + a.x, y1 = oy + a.y, x2 = ox + b.x, y2 = oy + b.y;
    const pad = 22, minX = rect.left + pad, maxX = rect.right - pad, minY = rect.top + pad, maxY = rect.bottom - pad;
    const inside = x2 >= minX && x2 <= maxX && y2 >= minY && y2 <= maxY;
    let ex = x2, ey = y2;
    if (!inside) { // clip the line where it leaves the map
      const dx = x2 - x1, dy = y2 - y1;
      let t = 1;
      if (dx) t = Math.min(t, dx > 0 ? (maxX - x1) / dx : (minX - x1) / dx);
      if (dy) t = Math.min(t, dy > 0 ? (maxY - y1) / dy : (minY - y1) / dy);
      t = Math.max(0, t); ex = x1 + dx * t; ey = y1 + dy * t;
    }
    const line = g.querySelector('#aaoline'), ring = g.querySelector('#aaoring'), edge = g.querySelector('#aaoedge'), label = g.querySelector('#aaolabel');
    line.setAttribute('x1', x1); line.setAttribute('y1', y1); line.setAttribute('x2', ex); line.setAttribute('y2', ey);
    ring.style.display = inside ? '' : 'none'; ring.setAttribute('cx', x2); ring.setAttribute('cy', y2);
    const ang = Math.atan2(ey - y1, ex - x1), s = 11;
    edge.style.display = inside ? 'none' : '';
    edge.setAttribute('d', `M${ex + Math.cos(ang) * s},${ey + Math.sin(ang) * s} L${ex + Math.cos(ang + 2.5) * s},${ey + Math.sin(ang + 2.5) * s} L${ex + Math.cos(ang - 2.5) * s},${ey + Math.sin(ang - 2.5) * s}Z`);
    const dist = guide.d < 1000 ? `${Math.round(guide.d)} m` : `${(guide.d / 1000).toFixed(1)} km`;
    label.textContent = dist;
    label.setAttribute('x', inside ? x2 : ex - Math.cos(ang) * 22); label.setAttribute('y', (inside ? y2 - 18 : ey - Math.sin(ang) * 22 + 4));
    drawEdgePips(g, w, cLL, ox, oy, x1, y1, {minX, maxX, minY, maxY}, inside ? null : [ex, ey]);
  }

  // ---- edge markers: like a compass around the map's border. Each of the nearest few targets
  // that is off the map gets a small arrow where its direction from you meets the edge ----
  function drawEdgePips(g, w, cLL, ox, oy, x1, y1, box, guideAt) {
    const holder = g.querySelector('#aaopips');
    const placed = guideAt ? [guideAt] : [];
    let html = '', shown = 0;
    // the edge frame sits a little outside the guide arrow's so they do not collide
    const minX = box.minX - 10, maxX = box.maxX + 10, minY = box.minY - 10, maxY = box.maxY + 10;
    for (const t of edgeTargets) {
      if (shown >= EDGE_COUNT) break;
      let b; try { b = screenOffset(w, cLL, t.to); } catch (e) { continue; }
      if (!b) continue;
      const x2 = ox + b.x, y2 = oy + b.y;
      if (x2 >= box.minX && x2 <= box.maxX && y2 >= box.minY && y2 <= box.maxY) continue; // on the map already
      const dx = x2 - x1, dy = y2 - y1;
      let k = Infinity;
      if (dx) k = Math.min(k, dx > 0 ? (maxX - x1) / dx : (minX - x1) / dx);
      if (dy) k = Math.min(k, dy > 0 ? (maxY - y1) / dy : (minY - y1) / dy);
      if (!Number.isFinite(k) || k <= 0) continue;
      const ex = x1 + dx * k, ey = y1 + dy * k;
      if (placed.some(([px, py]) => Math.hypot(px - ex, py - ey) < 26)) continue; // keep them readable
      placed.push([ex, ey]); shown++;
      const ang = Math.atan2(dy, dx), s = 9;
      const op = Math.max(0.55, 1 - shown * 0.09); // nearer = stronger
      const tri = `M${ex + Math.cos(ang) * s},${ey + Math.sin(ang) * s} L${ex + Math.cos(ang + 2.4) * s},${ey + Math.sin(ang + 2.4) * s} L${ex + Math.cos(ang - 2.4) * s},${ey + Math.sin(ang - 2.4) * s}Z`;
      const dist = t.d < 1000 ? `${Math.round(t.d)} m` : `${(t.d / 1000).toFixed(1)} km`;
      const tw = dist.length * 5.6 + 8, lx = ex - Math.cos(ang) * (12 + tw / 2 * Math.abs(Math.cos(ang))), ly = ey - Math.sin(ang) * 17;
      html += `<g opacity="${op.toFixed(2)}"><path d="${tri}" fill="#ffb347" stroke="#1b1209" stroke-width="1.4"/>`
        + `<rect x="${(lx - tw / 2).toFixed(1)}" y="${(ly - 7.5).toFixed(1)}" width="${tw.toFixed(1)}" height="15" rx="7.5" fill="rgba(14,16,20,.82)"/>`
        + `<text x="${lx.toFixed(1)}" y="${(ly + 3.6).toFixed(1)}" fill="#ffe2b8">${dist}</text></g>`;
    }
    holder.innerHTML = html;
  }
  (function guideLoop() { drawGuide(); setTimeout(guideLoop, guide ? 50 : 500); })();
  // troubleshooting: draw a guide (and edge markers) without live position, held for 15 s
  window.__aaoGuide = (g, edges) => { guide = g; edgeTargets = edges || []; testHoldUntil = Date.now() + 15000; };

  // ---- activity profiles: switch TH.GL's filters --------------------------------------
  function filterStore() { return findStore(s => typeof s.setFilters === 'function' && Array.isArray(s.filters)); }
  function withStore(get, fn, tries = 0) {
    const st = get();
    if (st) return fn(st);
    if (tries < 30) setTimeout(() => withStore(get, fn, tries + 1), 1000);
  }

  // ---- round minimap: rotate the map with your facing -----------------------------------
  function setRotate(on) {
    withStore(() => findStore(s => typeof s.setRotateMapWithPlayer === 'function'), st => {
      st.setRotateMapWithPlayer(!!on);
      if (!on) { const m = findMap(); if (m && m.webmap.setBearing) m.webmap.setBearing(0); }
    });
  }

  // ---- low-power: draw ~8 frames a second while you stand still and don't touch the map ----
  let lowPower = false;
  function idle() {
    const now = Date.now(), m = ctx && ctx.webmap;
    const animating = m && (Math.abs((m.targetZoom ?? m.zoom) - m.zoom) > 0.005 || m.dragging || m.zoomAnim || m.panAnim);
    return !animating && now - movedAt > 3000 && now - touchedAt > 3000;
  }
  const slowWindow = new Proxy(window, {
    get(t, prop) {
      if (prop === 'requestAnimationFrame') return cb => (lowPower && idle()) ? setTimeout(() => t.requestAnimationFrame(cb), 120) : t.requestAnimationFrame(cb);
      const v = t[prop];
      return typeof v === 'function' ? v.bind(t) : v;
    }
  });
  setInterval(() => {
    const m = findMap();
    if (m && m.webmap.rafWindow === window) m.webmap.rafWindow = slowWindow;
  }, 2000);

  // ---- Peer Link backup: keep the Peer Code and "Me" in the overlay's own settings ----
  // TH.GL keeps them in this page's browser storage, which a reinstall, update or
  // cleared cache can wipe. We copy them out to the overlay and put them back.
  function readPeer() {
    try {
      const st = JSON.parse(localStorage.getItem('settings-storage')).state;
      const s = (st.profiles.find(p => p.id === st.currentProfileId) || st.profiles[0]).settings;
      return {code: s.peerCode || '', auto: !!s.autoJoinPeer, me: s.lastMeSenderId || ''};
    } catch (e) { return null; }
  }
  let lastSent = '';
  setInterval(() => { // report changes (new code, new "Me") to the overlay
    const p = readPeer();
    if (!p || !p.code || window.parent === window) return;
    const key = p.code + '|' + p.me;
    if (key !== lastSent) { lastSent = key; window.parent.postMessage({aao: 'peer', code: p.code, me: p.me}, '*'); }
  }, 3000);

  function restorePeer(code, me) {
    if (!code) return;
    let tries = 0;
    const attempt = () => {
      const cur = readPeer();
      const store = walkFibers(s => s && typeof s === 'object' && typeof s.setPeerCode === 'function' && typeof s.setAutoJoinPeer === 'function' ? s : null);
      if (!cur || !store) { if (++tries < 30) setTimeout(attempt, 1000); return; }
      if (!cur.me && me) store.setLastMeSenderId && store.setLastMeSenderId(me);
      if (cur.code) return; // already has a code (joined, or left on purpose): leave it alone
      store.setPeerCode(code);
      store.setAutoJoinPeer(true);
      if (me && store.setLastMeSenderId) store.setLastMeSenderId(me);
      // TH.GL joins saved meshes when the page loads, so reload once to join now
      try { if (sessionStorage.getItem('aaoPeerRestored')) return; sessionStorage.setItem('aaoPeerRestored', '1'); } catch (e) { /* no storage: reload anyway */ }
      setTimeout(() => location.reload(), 300);
    };
    attempt();
  }

  // ---- map update: TH.GL flags a newer site version; tell the overlay, reload on request ----
  let mapUpdate = 'current';
  setInterval(() => {
    const b = document.querySelector('[data-testid=new-version-button]');
    const st = (b && b.dataset.status) || 'current';
    if (st !== mapUpdate && window.parent !== window) { mapUpdate = st; window.parent.postMessage({aao: 'mapupdate', status: st}, '*'); }
  }, 3000);

  window.addEventListener('message', e => {
    const d = e.data;
    if (e.source !== window.parent || !d || typeof d !== 'object' || !d.aao) return;
    if (d.aao === 'bars') setBars(!!d.hidden);
    if (d.aao === 'autozoom') { if (Number.isFinite(d.bias)) zoomBias = d.bias; setAuto(d.on); }
    if (d.aao === 'target') { targetKeys = Array.isArray(d.keys) ? d.keys : []; targetLabel = d.label || ''; if (!targetKeys.length) guide = null; }
    if (d.aao === 'setFilters' && Array.isArray(d.keys)) withStore(filterStore, st => st.setFilters(d.keys.slice()));
    if (d.aao === 'getFilters') withStore(filterStore, st => window.parent.postMessage({aao: 'filters', keys: st.filters.slice(), req: d.req}, '*'));
    if (d.aao === 'rotate') setRotate(d.on);
    if (d.aao === 'reloadmap') location.reload();
    if (d.aao === 'lowpower') lowPower = !!d.on;
    if (d.aao === 'locked') { applyLockState(!!d.on); setLocked(!!d.on); }
    if (d.aao === 'lockstate') applyLockState(!!d.on);
    if (d.aao === 'autodiscover') {
      const r = Number(d.radius);
      if (Number.isFinite(r) && r >= 1 && r <= 100) RADIUS = r * 100; // metres -> game units (cm)
      setDiscover(d.on);
    }
    if (d.aao === 'peerRestore' && !restoreAsked) { restoreAsked = true; restorePeer(d.code, d.me); }
  });
  let restoreAsked = false;

  // ---- start with TH.GL's Filters panel closed ------------------------------------
  (function closeFilters(tries) {
    let open = null;
    try {
      const st = JSON.parse(localStorage.getItem('settings-storage')).state;
      open = (st.profiles.find(p => p.id === st.currentProfileId) || st.profiles[0]).settings.showFilters;
    } catch (e) { /* settings not saved yet */ }
    const store = document.querySelector('canvas') && walkFibers(s => s && typeof s === 'object' && typeof s.toggleShowFilters === 'function' ? s : null);
    if (store) { if (store.showFilters ?? open) store.toggleShowFilters(); return; }
    if (tries < 30) setTimeout(() => closeFilters(tries + 1), 1000);
  })(0);
  // ask the overlay for the current settings once this page is up
  if (window.parent !== window) window.parent.postMessage({aao: 'hello'}, '*');
})();
