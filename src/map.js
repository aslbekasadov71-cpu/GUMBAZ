/* ───────── map: o'zimizning yengil xarita (tashqi kutubxonasiz) ───────── */
const TILE = 256;
const MERC = {
  px(lat, lon, z) { const s = TILE * Math.pow(2, z), si = Math.sin(Math.max(-85.0511, Math.min(85.0511, lat)) * Math.PI / 180);
    return {x: (lon + 180) / 360 * s, y: (0.5 - Math.log((1 + si) / (1 - si)) / (4 * Math.PI)) * s}; },
  ll(x, y, z) { const s = TILE * Math.pow(2, z), n = Math.PI - 2 * Math.PI * y / s;
    return {lat: 180 / Math.PI * Math.atan(Math.sinh(n)), lon: x / s * 360 - 180}; }
};
function GMap(el, o) {
  const M = {lat: o.lat, lon: o.lon, z: o.zoom || 16, W: 0, H: 0, fx: 0, fy: 0, ox: 0, oy: 0, tiles: new Map(), marks: [], moving: false, loaded: 0, failed: 0, dead: false};
  const pane = document.createElement("div"); pane.className = "gpane"; el.appendChild(pane);
  const mk = document.createElement("div"); mk.className = "gmarks"; el.appendChild(mk);
  const osm = (z, x, y) => `https://tile.openstreetmap.org/${z}/${x}/${y}.png`;
  let url = o.tileUrl || osm;  // standart: OpenStreetMap
  const clampZ = z => Math.max(4, Math.min(19, z));
  M.center = () => MERC.px(M.lat, M.lon, M.z);
  M.toScreen = (lat, lon) => { const c = M.center(), p = MERC.px(lat, lon, M.z); return {x: p.x - c.x + M.fx, y: p.y - c.y + M.fy}; };
  M.toLatLon = (sx, sy) => { const c = M.center(); return MERC.ll(c.x + sx - M.fx, c.y + sy - M.fy, M.z); };
  M.setCenter = (lat, lon) => { M.lat = Math.max(-85, Math.min(85, lat)); M.lon = ((lon + 540) % 360) - 180; M.draw(); };
  M.panBy = (dx, dy) => { const c = M.center(), p = MERC.ll(c.x - dx, c.y - dy, M.z); M.setCenter(p.lat, p.lon); };
  M.zoomAround = (z, sx, sy) => { const a = M.toLatLon(sx, sy); M.z = clampZ(z); const p = MERC.px(a.lat, a.lon, M.z), c = MERC.ll(p.x - (sx - M.fx), p.y - (sy - M.fy), M.z); M.setCenter(c.lat, c.lon); };
  M.measure = () => { const r = el.getBoundingClientRect(); M.W = r.width; M.H = r.height; M.ox = r.left; M.oy = r.top; M.fx = M.W / 2; M.fy = o.focusY ? o.focusY(M.H) : M.H / 2; o.onMeasure && o.onMeasure(M); M.draw(); };
  M.setTiles = fn => { url = fn || osm; M.tiles.forEach(t => t.el.remove()); M.tiles.clear(); M.loaded = M.failed = 0; M.draw(); };
  M.addMarker = (lat, lon, node) => { const m = {lat, lon, el: node}; mk.appendChild(node); M.marks.push(m); M.draw(); return m; };
  let raf = 0; M.draw = () => { if (!raf && !M.dead) raf = requestAnimationFrame(render); };
  function render() {
    raf = 0; if (!M.W) return;
    const tz = Math.round(clampZ(M.z)), n = Math.pow(2, tz), want = new Set(); let pending = 0;
    const place = (t, z) => { const s = Math.pow(2, M.z - z), c = MERC.px(M.lat, M.lon, z), x = (t.x * TILE - c.x) * s + M.fx, y = (t.y * TILE - c.y) * s + M.fy;
      t.el.style.transform = `translate3d(${x}px,${y}px,0) scale(${s * 1.004})`; return {x, y, sz: TILE * s}; };
    const c = MERC.px(M.lat, M.lon, tz), s = Math.pow(2, M.z - tz);
    const x0 = Math.floor((c.x - M.fx / s) / TILE), x1 = Math.floor((c.x + (M.W - M.fx) / s) / TILE);
    const y0 = Math.max(0, Math.floor((c.y - M.fy / s) / TILE)), y1 = Math.min(n - 1, Math.floor((c.y + (M.H - M.fy) / s) / TILE));
    for (let ty = y0; ty <= y1; ty++) for (let tx = x0; tx <= x1; tx++) {
      const wx = ((tx % n) + n) % n, key = tz + "/" + tx + "/" + ty; want.add(key);
      let t = M.tiles.get(key);
      if (!t) {
        const im = new Image(); im.className = "gt"; im.alt = ""; im.decoding = "async"; im.draggable = false;
        t = {z: tz, x: tx, y: ty, el: im, ok: false, tries: 0};
        im.onload = () => { t.ok = true; im.classList.add("ld"); M.loaded++; M.draw(); o.onTileOk && o.onTileOk(); };
        im.onerror = () => { if (t.tries++ === 0) im.src = `tiles/${tz}/${wx}/${ty}.png`; else { M.failed++; if (!M.loaded && M.failed > 3) o.onTileFail && o.onTileFail(); } };
        im.src = url(tz, wx, ty); M.tiles.set(key, t); pane.appendChild(im);
      }
      t.el.style.zIndex = 2; place(t, tz); if (!t.ok) pending++;
    }
    M.tiles.forEach((t, key) => {
      if (want.has(key)) return;
      if (!(pending && t.ok && Math.abs(t.z - tz) <= 2)) { t.el.remove(); M.tiles.delete(key); return; }
      const r = place(t, t.z); t.el.style.zIndex = 1;
      if (r.x > M.W || r.y > M.H || r.x + r.sz < 0 || r.y + r.sz < 0) { t.el.remove(); M.tiles.delete(key); }
    });
    M.marks.forEach(m => { const p = M.toScreen(m.lat, m.lon); m.el.style.transform = `translate3d(${p.x}px,${p.y}px,0)`; });
  }
  /* harakat */
  let anim = 0, inert = 0;
  const startMove = () => { if (!M.moving) { M.moving = true; o.onMoveStart && o.onMoveStart(); } };
  const endMove = () => { if (M.moving) { M.moving = false; o.onMoveEnd && o.onMoveEnd(); } };
  const stopAnim = () => { cancelAnimationFrame(anim); cancelAnimationFrame(inert); anim = inert = 0; };
  const ease = k => k < .5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
  M.flyTo = (lat, lon, z = M.z, ms = 700) => {
    stopAnim(); startMove();
    const f = {lat: M.lat, lon: M.lon, z: M.z}, t0 = performance.now(), dz = clampZ(z) - f.z;
    const far = Math.hypot(M.toScreen(lat, lon).x - M.fx, M.toScreen(lat, lon).y - M.fy) > M.W * 1.5;
    const step = now => { const k = Math.min(1, (now - t0) / ms), e = ease(k);
      M.lat = f.lat + (lat - f.lat) * e; M.lon = f.lon + (lon - f.lon) * e;
      M.z = f.z + dz * e - (far ? Math.sin(Math.PI * k) * 1.5 : 0); M.draw();
      if (k < 1) anim = requestAnimationFrame(step); else { anim = 0; endMove(); } };
    anim = requestAnimationFrame(step);
  };
  M.zoomTo = (z, sx = M.fx, sy = M.fy) => { const a = M.toLatLon(sx, sy), zz = clampZ(z), p = MERC.px(a.lat, a.lon, zz), c = MERC.ll(p.x - (sx - M.fx), p.y - (sy - M.fy), zz); M.flyTo(c.lat, c.lon, zz, 380); };
  const pts = new Map(); let pinch = null, moved = false, lastT = 0, vel = {x: 0, y: 0}, tapT = 0, tapP = null;
  el.addEventListener("pointerdown", e => {
    if (e.target.closest("button,input,.msheet")) return;
    try { el.setPointerCapture(e.pointerId); } catch (_) {}
    stopAnim(); pts.set(e.pointerId, {x: e.clientX, y: e.clientY, sx: e.clientX, sy: e.clientY}); moved = false; lastT = performance.now(); vel = {x: 0, y: 0};
    if (pts.size === 2) { const [a, b] = [...pts.values()]; pinch = {d: Math.hypot(a.x - b.x, a.y - b.y) || 1, z: M.z, mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2}; }
  });
  el.addEventListener("pointermove", e => {
    const p = pts.get(e.pointerId); if (!p) return;
    if (pts.size === 1) {
      if (!moved && Math.hypot(e.clientX - p.sx, e.clientY - p.sy) < 4) return;
      moved = true; startMove();
      const dx = e.clientX - p.x, dy = e.clientY - p.y, now = performance.now(), dt = Math.max(1, now - lastT);
      M.panBy(dx, dy); vel = {x: .75 * dx / dt + .25 * vel.x, y: .75 * dy / dt + .25 * vel.y}; lastT = now;
    }
    p.x = e.clientX; p.y = e.clientY;
    if (pts.size === 2 && pinch) {
      moved = true; startMove();
      const [a, b] = [...pts.values()], d = Math.hypot(a.x - b.x, a.y - b.y), mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      M.panBy(mx - pinch.mx, my - pinch.my); pinch.mx = mx; pinch.my = my;
      M.zoomAround(pinch.z + Math.log2(d / pinch.d), mx - M.ox, my - M.oy);
    }
  });
  const up = e => {
    if (!pts.has(e.pointerId)) return; pts.delete(e.pointerId);
    if (pts.size === 1) { pinch = null; const q = [...pts.values()][0]; q.sx = q.x; q.sy = q.y; lastT = performance.now(); vel = {x: 0, y: 0}; return; }
    if (pts.size) return;
    if (!moved) {
      const now = performance.now();
      if (now - tapT < 320 && tapP && Math.hypot(tapP.x - e.clientX, tapP.y - e.clientY) < 30) { tapT = 0; M.zoomTo(Math.round(M.z) + 1, e.clientX - M.ox, e.clientY - M.oy); }
      else { tapT = now; tapP = {x: e.clientX, y: e.clientY}; }
      return;
    }
    pinch = null;
    if (performance.now() - lastT > 70) vel = {x: 0, y: 0};
    let t0 = performance.now();
    const step = now => { const dt = Math.min(40, now - t0); t0 = now; const f = Math.pow(.93, dt / 16); vel.x *= f; vel.y *= f;
      if (Math.hypot(vel.x, vel.y) < .03) { inert = 0; return endMove(); }
      M.panBy(vel.x * dt, vel.y * dt); inert = requestAnimationFrame(step); };
    inert = requestAnimationFrame(step);
  };
  el.addEventListener("pointerup", up); el.addEventListener("pointercancel", up);
  let wT; el.addEventListener("wheel", e => { e.preventDefault(); stopAnim(); startMove(); M.zoomAround(M.z - e.deltaY / 250, e.clientX - M.ox, e.clientY - M.oy); clearTimeout(wT); wT = setTimeout(endMove, 220); }, {passive: false});
  M.onResize = () => M.measure(); window.addEventListener("resize", M.onResize);
  M.destroy = () => { M.dead = true; stopAnim(); cancelAnimationFrame(raf); window.removeEventListener("resize", M.onResize); };
  requestAnimationFrame(M.measure);
  return M;
}
const dist = (a, b, c, d) => { const R = 6371, r = Math.PI / 180, x = Math.sin((c - a) * r / 2) ** 2 + Math.cos(a * r) * Math.cos(c * r) * Math.sin((d - b) * r / 2) ** 2; return 2 * R * Math.asin(Math.sqrt(x)); };
const kmTxt = km => km < 1 ? `${Math.round(km * 1000 / 10) * 10} m` : `${km.toFixed(1).replace(".", ",")} km`;
const isCoords = a => /^\s*-?\d+\.\d+,\s*-?\d+\.\d+\s*$/.test(a || "");
const PIN_SVG = `<svg viewBox="0 0 48 60" width="48" height="60" aria-hidden="true"><defs><linearGradient id="pg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3b7bf0"/><stop offset="1" stop-color="#0f2f6e"/></linearGradient></defs><path d="M24 2C12.4 2 3 11.2 3 22.6c0 14.6 17.4 32.3 19.4 34.3a2.3 2.3 0 0 0 3.2 0C27.6 54.9 45 37.2 45 22.6 45 11.2 35.6 2 24 2z" fill="url(#pg)"/><circle cx="24" cy="22.5" r="10.5" fill="#fff"/><circle cx="24" cy="22.5" r="5" fill="#f5b301"/></svg>`;
/* Xarita uslublari: Mapbox (token bo'lsa) — sputnik/sxema, aks holda OpenStreetMap */
const MAP_STYLES = {sat: "mapbox/satellite-streets-v12", map: "mapbox/streets-v12"};
function tileFn(token, style) {
  if (!token) return null;
  return (z, x, y) => `https://api.mapbox.com/styles/v1/${MAP_STYLES[style] || MAP_STYLES.sat}/tiles/256/${z}/${x}/${y}@2x?access_token=${encodeURIComponent(token)}`;
}
function mapStyle(v) { try { if (v) localStorage.setItem("gx_mapstyle", v); return localStorage.getItem("gx_mapstyle") || "sat"; } catch (e) { return v || "sat"; } }
/* ───────── Yandex xaritasi (JS API 2.1) — kalit berilsa: uylar va uy raqamlari bilan ───────── */
let _ymP = null;
function loadYmaps(key) {
  if (window.ymaps && window.ymaps.Map) return Promise.resolve(window.ymaps);
  if (_ymP) return _ymP;
  _ymP = new Promise((res, rej) => {
    const s = document.createElement("script");
    s.src = "https://api-maps.yandex.ru/2.1/?apikey=" + encodeURIComponent(key) + "&lang=ru_RU";
    s.onload = () => window.ymaps ? window.ymaps.ready(() => res(window.ymaps), rej) : rej("noymaps");
    s.onerror = () => rej("load");
    document.head.appendChild(s); setTimeout(() => rej("timeout"), 15000);
  }).catch(e => { _ymP = null; throw e; });
  return _ymP;
}
const YTYPE = v => v === "sat" ? "yandex#hybrid" : "yandex#map";
function YMap(el, o) {
  const M = {lat: o.lat, lon: o.lon, z: o.zoom || 16, W: 0, H: 0, fx: 0, fy: 0, ym: null, q: [], marks: [], dead: false, yandex: true, moving: false};
  const box = document.createElement("div"); box.className = "ybox"; el.appendChild(box);
  el.classList.add("ymode");
  M.measure = () => { if (M.dead) return; const r = el.getBoundingClientRect(); M.W = r.width; M.H = r.height; M.fx = M.W / 2;
    M.fy = Math.min(M.H / 2, o.focusY ? o.focusY(M.H) : M.H / 2); box.style.height = Math.round(2 * M.fy) + "px";
    o.onMeasure && o.onMeasure(M); if (M.ym) M.ym.container.fitToViewport(); };
  const run = f => { if (M.ym) f(M.ym); else M.q.push(f); };
  const sync = () => { const c = M.ym.getCenter(); M.lat = c[0]; M.lon = c[1]; M.z = M.ym.getZoom(); };
  loadYmaps(o.ykey).then(ymaps => {
    if (M.dead) return;
    const map = M.ym = new ymaps.Map(box, {center: [M.lat, M.lon], zoom: Math.round(M.z), controls: [], type: YTYPE(o.ystyle)},
      {suppressMapOpenBlock: true, yandexMapDisablePoiInteractivity: true});
    map.events.add("actionbegin", () => { if (!M.moving) { M.moving = true; o.onMoveStart && o.onMoveStart(); } });
    map.events.add("actionend", () => { sync(); if (M.moving) { M.moving = false; o.onMoveEnd && o.onMoveEnd(); } });
    M.q.splice(0).forEach(f => f(map)); M.measure(); o.onTileOk && o.onTileOk();
  }).catch(() => {  // Yandex yuklanmasa — o'zimizning xaritaga o'tamiz
    if (M.dead) return; box.remove(); el.classList.remove("ymode");
    const g = GMap(el, Object.assign({}, o, {lat: M.lat, lon: M.lon, zoom: M.z}));
    ["lat", "lon", "z", "W", "H", "fx", "fy", "moving"].forEach(k => Object.defineProperty(M, k, {get: () => g[k], set: v => { g[k] = v; }, configurable: true}));
    Object.keys(g).forEach(k => { if (typeof g[k] === "function") M[k] = g[k]; });
    M.yandex = false; M.setType = () => {}; M.marks.forEach(m => g.addMarker(m.lat, m.lon, m.el));
  });
  M.flyTo = (lat, lon, z = M.z, ms = 600) => run(map => { o.onMoveStart && !M.moving && o.onMoveStart(); M.moving = true;
    map.setCenter([lat, lon], Math.round(z), {duration: ms}).then(() => { sync(); if (M.moving) { M.moving = false; o.onMoveEnd && o.onMoveEnd(); } }); });
  M.zoomTo = z => run(map => map.setZoom(Math.max(4, Math.min(19, Math.round(z))), {duration: 250}));
  M.setTiles = () => {};
  M.setType = v => run(map => map.setType(YTYPE(v)));
  M.addMarker = (lat, lon, node) => {
    const m = {lat, lon, el: node}; M.marks.push(m);
    run(map => { const geo = /udot/.test(node.className);
      const cap = (node.querySelector && node.querySelector("b") || {}).textContent || "";
      m.pm = new window.ymaps.Placemark([lat, lon], geo ? {} : {iconCaption: cap}, {preset: geo ? "islands#geolocationIcon" : "islands#blueDotIconWithCaption"});
      map.geoObjects.add(m.pm); });
    return m;
  };
  M.draw = () => {};
  M.destroy = () => { M.dead = true; window.removeEventListener("resize", M.measure); try { M.ym && M.ym.destroy(); } catch (e) {} };
  window.addEventListener("resize", M.measure); requestAnimationFrame(M.measure);
  return M;
}
/* kalit bo'lsa Yandex, aks holda Mapbox / OpenStreetMap */
function makeMap(el, o) { return o.ykey ? YMap(el, o) : GMap(el, o); }
