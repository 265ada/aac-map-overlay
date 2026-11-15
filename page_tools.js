// Runs inside TH.GL's map page (injected by overlay.py into every frame).
// Takes commands from our overlay UI through postMessage:
//   {aao: 'bars', hidden: true|false}   hide TH.GL's top header
//   {aao: 'autozoom', on: true|false}   smart zoom
//   {aao: 'autodiscover', on: true|false}  mark pins you stand on for 3 s
//
// Smart zoom: TH.GL's map follows your character, so the map centre is you.
// Every ~0.8 s it measures how far the nearest visible pins are from the centre
// and zooms so those pins fill about a third of the window. Tight clusters
// (gatherables, collectibles in one spot) zoom in hard; open areas zoom out.
// Scrolling or dragging the map yourself pauses it for 10 seconds.
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

  let autoOn = false, pausedUntil = 0, timer = 0, ctx = null;

  // ---- top header -----------------------------------------------------------
  const barsCss = document.createElement('style');
  barsCss.textContent = 'header.fixed{display:none!important}:root{--header-h:0px!important}';
  function setBars(hidden) {
    if (hidden) (document.head || document.documentElement).appendChild(barsCss);
    else barsCss.remove();
    window.dispatchEvent(new Event('resize')); // let the map canvas refit
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
    const pause = () => { pausedUntil = Date.now() + PAUSE_MS; };
    canvas.addEventListener('wheel', pause, {passive: true});
    canvas.addEventListener('pointerdown', pause);
  }

  // ---- smart zoom -------------------------------------------------------------
  function tick() {
    if (!autoOn || Date.now() < pausedUntil || document.hidden) return;
    const m = findMap();
    if (!m) return;
    const w = m.webmap;
    let c;
    try { c = w.projectAt(w.center, 0); } catch (e) { return; }
    if (!c || !Number.isFinite(c.x)) return;

    // distances (in zoom-0 map pixels) to every visible pin; keep the nearest few
    const best = [];
    for (const p of m.markers.getInstances()) {
      if (m.markers.isHidden(p) || !p.latLng) continue;
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
    let z = Math.log2((side * FILL) / spread);   // screen px = map px * 2^zoom
    z = Math.max(MIN_Z, Math.min(maxZ, z));

    const now = w.targetZoom !== undefined ? w.targetZoom : w.getZoom();
    window.__aaoLast = {spread, want: z, now, at: Date.now()}; // for troubleshooting
    if (Math.abs(z - now) < DEADBAND) return;
    const next = now + Math.max(-MAX_STEP, Math.min(MAX_STEP, z - now));
    w.targetZoom = next; // TH.GL's engine animates to targetZoom
  }

  // ---- auto discover: stand on a pin for 3 s and it is marked discovered ----
  // Uses your live position from the TH.GL app (Peer Link) and TH.GL's own
  // "auto discovered" list, so it shows and syncs like the app's own marks.
  const DWELL_MS = 3000;     // how long you must stay on it
  const RADIUS = 2000;       // game units (cm): within 20 m counts as "on it" (pins are not exact)
  let discoverOn = false, discTimer = 0, peersRef = null, markFn = null, meId = '', meAt = 0, lastStatus = 0;
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
    if (!peersRef || !Object.values(peersRef).some(v => v && v.player)) {
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
        key: best ? best.key : '', held, need: DWELL_MS}, '*');
    }
    if (done.length) {
      try { markDiscovered(done); } catch (e) { /* TH.GL changed; try again next time */ markFn = null; }
      done.forEach(id => near.delete(id));
    }
  }

  window.__aaoDisc = {near, myPosition, findMark: () => markDiscovered.length && (markFn || walkFibers(s => s && typeof s === 'object' && typeof s.markAutoDiscovered === 'function' ? s.markAutoDiscovered : null))}; // troubleshooting

  function setDiscover(on) {
    discoverOn = !!on;
    clearInterval(discTimer);
    near.clear();
    if (discoverOn) discTimer = setInterval(discoverTick, 250);
  }

  function setAuto(on) {
    autoOn = !!on;
    clearInterval(timer);
    if (autoOn) { pausedUntil = 0; timer = setInterval(tick, 800); tick(); }
  }

  window.addEventListener('message', e => {
    const d = e.data;
    if (e.source !== window.parent || !d || typeof d !== 'object' || !d.aao) return;
    if (d.aao === 'bars') setBars(!!d.hidden);
    if (d.aao === 'autozoom') setAuto(d.on);
    if (d.aao === 'autodiscover') setDiscover(d.on);
  });
  // ask the overlay for the current settings once this page is up
  if (window.parent !== window) window.parent.postMessage({aao: 'hello'}, '*');
})();
