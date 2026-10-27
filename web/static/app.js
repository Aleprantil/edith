/* EDITH · web app. JavaScript semplice, nessuna libreria. Ogni dato mostrato passa da textContent o dal filtro safeHTML. */
(() => {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const state = { me: null, tts: false, busy: false, barOpen: false, lastReply: null, listening: false, speaking: false, voiceBlocked: false, ac: null, stopCapture: null, abortCapture: null, data: {}, desk: false };
  const SPOKEN_NAME = 'Edith';                         // come la pronuncia la voce: se suona male prova una grafia diversa (per esempio 'Èdit')
  const INTRO_URL = '/static/intro.mp3';               // il brano lo metti tu in web/static/intro.mp3 (non è incluso)
  const INTRO_MAX_MS = 24000;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const SESSION = (() => {
    let id = sessionStorage.getItem('edith_session');
    if (!id) { id = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now()).replace(/[^A-Za-z0-9_-]/g, '').slice(0, 40).padEnd(12, 'x'); sessionStorage.setItem('edith_session', id); }
    return id;
  })();

  /* ---------- utilità ---------- */
  function el(tag, props, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return n;
  }
  // importi all'italiana: 1.159,40 (il formato del browser omette il punto sotto i 10.000)
  const eurNum = (n) => { const [i, f] = Math.abs(Number(n) || 0).toFixed(2).split('.'); return (n < 0 ? '−' : '') + i.replace(/\B(?=(\d{3})+$)/g, '.') + ',' + f; };
  const money = (n) => eurNum(n) + ' €';
  const num = (n, d = 0) => (n == null || isNaN(n)) ? '–' : Number(n).toFixed(d);
  const gb = (b) => b == null ? '–' : (b >= 2 ** 40 ? (b / 2 ** 40).toFixed(1) + ' TB' : b >= 2 ** 30 ? (b / 2 ** 30).toFixed(1) + ' GB' : Math.round(b / 2 ** 20) + ' MB');
  function dur(s) {
    s = Math.max(0, Math.floor(s || 0));
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? `${d} g ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
  }
  function ago(ts) {
    if (!ts) return '';
    const s = Date.now() / 1000 - ts;
    return s < 3600 ? `${Math.max(1, Math.floor(s / 60))} min fa` : s < 86400 ? `${Math.floor(s / 3600)} h fa` : `${Math.floor(s / 86400)} g fa`;
  }
  function level(v, warn, bad) { return v == null ? '' : v >= bad ? 'bad' : v >= warn ? 'warn' : ''; }

  /* HTML del server -> solo grassetto, corsivo, codice, a capo, link http(s) */
  function safeHTML(str) {
    const doc = new DOMParser().parseFromString('<div>' + (str || '') + '</div>', 'text/html');
    const out = document.createDocumentFragment();
    const OK = { B: 1, STRONG: 1, I: 1, EM: 1, CODE: 1, BR: 1, A: 1 };
    (function walk(node, parent) {
      for (const n of node.childNodes) {
        if (n.nodeType === 3) parent.appendChild(document.createTextNode(n.nodeValue));
        else if (n.nodeType === 1) {
          if (n.tagName === 'SCRIPT' || n.tagName === 'STYLE') continue;
          if (OK[n.tagName]) {
            const c = document.createElement(n.tagName.toLowerCase());
            if (n.tagName === 'A') { const h = n.getAttribute('href') || ''; if (/^https?:\/\//i.test(h)) { c.href = h; c.target = '_blank'; c.rel = 'noopener noreferrer'; } }
            walk(n, c); parent.appendChild(c);
          } else walk(n, parent);
        }
      }
    })(doc.body.firstChild, out);
    return out;
  }

  async function api(path, opts = {}) {
    const init = { credentials: 'same-origin', headers: { 'X-Requested-With': 'edith' } };
    if (opts.body) { init.method = 'POST'; init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
    const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 80000);
    init.signal = ctl.signal;
    try {
      const r = await fetch(path, init);
      if (r.status === 401) { location.reload(); throw new Error('non autorizzato'); }
      return await r.json();
    } finally { clearTimeout(timer); }
  }

  /* ======================================================================
     Sfera di particelle. Pensata per consumare poco:
       - punti disegnati a gruppi per fascia di profondità (un solo riempimento per fascia), niente ombre sfocate;
       - 30 fotogrammi al secondo (regolari) da ferma, 60 quando ascolta, pensa o parla e nei 10 secondi dopo un tocco;
       - si ferma del tutto dopo un minuto di inattività e quando l'app non è visibile;
       - con «Riduci movimento» attivo non si anima.
     ====================================================================== */
  const STATES = {
    idle:      { spin: .30, amp: .030, arc: .70, arcs: 1, fps: 30 },
    listening: { spin: .62, amp: .062, arc: 1.4, arcs: 1, fps: 60 },
    thinking:  { spin: 1.7, amp: .050, arc: 3.4, arcs: 2, fps: 60 },
    speaking:  { spin: .55, amp: .070, arc: 1.2, arcs: 1, fps: 60 },
  };
  const FREEZE_MS = 60000, HOT_MS = 10000;      // ferma dopo 1 minuto di inattività; 60 fotogrammi/s per 10 secondi dopo un tocco
  const reduceMotion = window.matchMedia ? matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };

  const glowSprite = (() => {
    const c = document.createElement('canvas'); c.width = c.height = 48;
    const g = c.getContext('2d'), gr = g.createRadialGradient(24, 24, 0, 24, 24, 24);
    gr.addColorStop(0, 'rgba(255,236,200,.8)'); gr.addColorStop(.2, 'rgba(255,159,28,.42)'); gr.addColorStop(1, 'rgba(255,159,28,0)');
    g.fillStyle = gr; g.fillRect(0, 0, 48, 48);
    return c;
  })();

  class Sphere {
    constructor(cv, { points, mini }) {
      this.cv = cv; this.x = cv.getContext('2d'); this.mini = !!mini; this.N = points;
      this.px = new Float32Array(points); this.py = new Float32Array(points); this.pz = new Float32Array(points); this.hot = new Uint8Array(points);
      for (let i = 0; i < points; i++) {
        const y = 1 - 2 * (i + .5) / points, r = Math.sqrt(1 - y * y), a = i * 2.399963;
        this.px[i] = Math.cos(a) * r; this.py[i] = y; this.pz[i] = Math.sin(a) * r; this.hot[i] = i % (mini ? 23 : 31) === 0 ? 1 : 0;
      }
      this.B = 8; this.buckets = Array.from({ length: this.B }, () => []); this.hots = [];
      this.p = { spin: STATES.idle.spin, amp: STATES.idle.amp, arc: STATES.idle.arc };
      this.state = 'idle'; this.ry = .9; this.arcA = 0; this.boost = 0; this.lastDraw = 0; this.W = 0; this.visible = true; this.level = 0; this.lv = 0;
    }
    resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2), w = this.cv.getBoundingClientRect().width;
      if (!w) return false;
      const s = Math.round(w * dpr);
      if (this.cv.width !== s) { this.cv.width = this.cv.height = s; }
      this.W = s; return true;
    }
    setState(name) { this.state = STATES[name] ? name : 'idle'; }
    draw(t, dt) {
      if (!this.W && !this.resize()) return;
      const x = this.x, W = this.W, cx = W / 2, cy = W / 2, mini = this.mini, R = W * (mini ? .30 : .355), u = W / (mini ? 80 : 680);
      const p = this.p, S = STATES[this.state], k = Math.min(1, dt * 4);
      p.spin += (S.spin - p.spin) * k; p.amp += (S.amp - p.amp) * k; p.arc += (S.arc - p.arc) * k;
      this.ry += p.spin * dt; this.arcA += p.arc * dt;
      const boost = this.boost; this.boost = Math.max(0, boost - dt * 1.1);
      this.lv += (this.level - this.lv) * Math.min(1, dt * 12);                 // volume della voce che ascolta
      let amp = p.amp * (1 + boost * 1.6) + Math.min(.09, this.lv * .5); if (this.state === 'speaking') amp *= 1 + .45 * Math.sin(t * 9);
      x.clearRect(0, 0, W, W);

      if (!mini) {                                                  // orbite sottili
        x.save(); x.translate(cx, cy); x.strokeStyle = 'rgba(255,196,107,.26)'; x.lineWidth = 2 * u;
        for (let j = 0; j < 2; j++) { x.save(); x.rotate(-.5 + j * .9 + t * (.05 + j * .03)); x.beginPath(); x.ellipse(0, 0, R * 1.38, R * .42, 0, 0, 7); x.stroke(); x.restore(); }
        x.restore();
      }
      // proiezione dei punti, raggruppati per profondità
      const ry = this.ry, rx = .42, cyr = Math.cos(ry), syr = Math.sin(ry), cxr = Math.cos(rx), sxr = Math.sin(rx), B = this.B;
      for (let b = 0; b < B; b++) this.buckets[b].length = 0; this.hots.length = 0;
      for (let i = 0; i < this.N; i++) {
        const px = this.px[i], py = this.py[i], pz = this.pz[i];
        const wob = 1 + amp * Math.sin(px * 4 + t * 1.7) * Math.cos(py * 3 - t * 1.3) + amp * .45 * Math.sin(pz * 7 + t * 2.3);
        const X = px * wob, Y = py * wob, Z = pz * wob, x1 = X * cyr + Z * syr, z1 = -X * syr + Z * cyr, y1 = Y * cxr - z1 * sxr, z2 = Y * sxr + z1 * cxr;
        const d = (z2 + 1) / 2, arr = this.hot[i] ? this.hots : this.buckets[Math.min(B - 1, (d * B) | 0)];
        arr.push(cx + x1 * R, cy + y1 * R, d);
      }
      for (let b = 0; b < B; b++) {
        const arr = this.buckets[b]; if (!arr.length) continue;
        const d = (b + .5) / B, al = .14 + .86 * d * d, s = (mini ? .8 + 1.7 * d : 1.1 + 2.7 * d) * u;
        x.fillStyle = `rgba(255,${150 + 60 * d | 0},${28 + 60 * d * d | 0},${al})`; x.beginPath();
        for (let i = 0; i < arr.length; i += 3) { x.moveTo(arr[i] + s, arr[i + 1]); x.arc(arr[i], arr[i + 1], s, 0, 7); }
        x.fill();
      }
      const hs = (mini ? 11 : 19) * u;                              // punti luminosi: alone + nucleo chiaro
      for (let i = 0; i < this.hots.length; i += 3) x.drawImage(glowSprite, this.hots[i] - hs, this.hots[i + 1] - hs, hs * 2, hs * 2);
      x.fillStyle = 'rgba(255,240,215,.95)'; x.beginPath();
      for (let i = 0; i < this.hots.length; i += 3) { const r = (mini ? 1.1 : 2.1) * u * (.6 + this.hots[i + 2]); x.moveTo(this.hots[i] + r, this.hots[i + 1]); x.arc(this.hots[i], this.hots[i + 1], r, 0, 7); }
      x.fill();

      x.save(); x.translate(cx, cy);                                // archi luminosi
      x.strokeStyle = 'rgba(255,159,28,.92)'; x.lineWidth = (mini ? 2 : 3) * u; x.lineCap = 'round';
      x.save(); x.rotate(this.arcA); x.beginPath(); x.arc(0, 0, R * 1.17, 0, .9); x.stroke(); x.restore();
      if (S.arcs > 1) { x.save(); x.rotate(-this.arcA * .8 + 2); x.beginPath(); x.arc(0, 0, R * 1.26, 0, 1.4); x.stroke(); x.restore(); }
      if (this.state === 'listening' || this.state === 'speaking') {  // onde che si allargano
        for (let j = 0; j < 2; j++) { const ph = (t * (this.state === 'speaking' ? .9 : .7) + j * .5) % 1; x.strokeStyle = `rgba(255,159,28,${.4 * (1 - ph)})`; x.lineWidth = 2 * u; x.beginPath(); x.arc(0, 0, R * (1.05 + ph * .5), 0, 7); x.stroke(); }
      }
      x.restore();
      this.lastDraw = t;
    }
  }

  const big = new Sphere($('#orb-cv'), { points: 1000 });
  const engines = [big];
  let raf = 0, lastActivity = performance.now(), hotUntil = 0, orbTimer = 0;

  function frame(ms) {
    raf = 0;
    if (document.visibilityState !== 'visible') return;
    const t = ms / 1000; let anyActive = false;
    for (const e of engines) {
      const base = (state.desk && e.state === 'idle') ? 15 : STATES[e.state].fps, fps = (base >= 60 || (ms < hotUntil && !state.desk)) ? 60 : base;
      const since = ms - e.lastMs;                                    // si disegna 1 fotogramma ogni 2 dello schermo (30) o ogni 1 (60): cadenza regolare
      if (!(e.lastMs >= 0) || since >= 1000 / fps - 4) { e.draw(t, Math.min(.1, e.lastMs >= 0 ? since / 1000 : .016)); e.lastMs = ms; }
      if (e.state !== 'idle') anyActive = true;
    }
    if (!anyActive && !state.desk && ms > hotUntil && ms - lastActivity > FREEZE_MS) return;   // fermo: l'ultimo fotogramma resta a schermo (non in modalità scrivania)
    raf = requestAnimationFrame(frame);
  }
  function drawStill() { for (const e of engines) { e.lastMs = -1; e.draw(1.7, .016); } }
  function wake() {
    const n = performance.now(); lastActivity = n; hotUntil = n + HOT_MS;
    if (reduceMotion.matches) { drawStill(); return; }
    if (!raf && document.visibilityState === 'visible') raf = requestAnimationFrame(frame);
  }
  function setOrb(name) {
    clearTimeout(orbTimer);
    const st = (name === 'pulse' || !name) ? 'idle' : name;
    for (const e of engines) { e.setState(st); if (name === 'pulse') e.boost = 1; }
    document.body.classList.toggle('energized', st !== 'idle');
    if (name === 'pulse') orbTimer = setTimeout(() => setOrb(state.barOpen && document.activeElement === $('#input') ? 'listening' : ''), 1000);
    wake();
  }
  function relayout() { engines.forEach((e) => { e.W = 0; e.resize(); }); wake(); }
  let rz = 0; window.addEventListener('resize', () => { clearTimeout(rz); rz = setTimeout(relayout, 150); });
  for (const ev of ['pointerdown', 'keydown', 'touchstart']) document.addEventListener(ev, wake, { passive: true, capture: true });
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') wake(); });
  if (reduceMotion.addEventListener) reduceMotion.addEventListener('change', wake);

  /* ---------- orologio e testata ---------- */
  function tickClock() {
    const d = new Date(), h = $('#time');
    h.replaceChildren(String(d.getHours()).padStart(2, '0'), el('span', { text: ':' }), String(d.getMinutes()).padStart(2, '0'));
    updateNext(); updateDimmer();
    setTimeout(tickClock, (60 - d.getSeconds()) * 1000 - d.getMilliseconds() + 40);
  }
  function setDateLine(label) {
    $('#dateline').textContent = label || new Date().toLocaleDateString('it-IT', { weekday: 'long', day: 'numeric', month: 'long' });
  }
  function setHello(greeting) {
    const name = state.me && state.me.owner;
    $('#hello').textContent = (greeting || 'Ciao') + (name ? ' · ' + name : '');
  }
  function setStatus(d) {
    const s = $('#status'); s.replaceChildren(); s.classList.remove('bad');
    if (!d.nextcloud) return;
    s.classList.toggle('bad', !d.nextcloud.ok);
    s.append(el('b', { text: '●' }), ' NEXTCLOUD' + (d.nextcloud.ok && d.nextcloud.ms ? ' ' + d.nextcloud.ms + ' ms' : (d.nextcloud.ok ? '' : ' ✗')));
  }

  /* ---------- schede ---------- */
  const rowEv = (e) => el('li', { class: 'ev' + (e.past ? ' past' : '') + (e.now ? ' now' : '') },
    el('div', { class: 'ev-time' }, e.all_day ? 'Tutto' : e.time, e.all_day ? el('small', {}, 'il giorno') : (e.end ? el('small', {}, '→ ' + e.end) : null)),
    el('div', {}, el('div', { class: 'ev-title', text: e.title }),
      (e.calendar || e.location) ? el('div', { class: 'ev-meta', text: [e.location && '📍 ' + e.location, e.calendar].filter(Boolean).join(' · ') }) : null));

  function renderToday(d) {
    const root = el('div');
    if (d.summary) root.append(el('p', { class: 'summary', text: d.summary }));
    if (d.events.length) root.append(el('ul', { class: 'timeline' }, d.events.map(rowEv)));
    else if (!d.errors.some((e) => e.startsWith('Calendario'))) root.append(el('div', { class: 'empty', text: 'Nessun impegno oggi. 🎉' }));
    if (d.windows.length) root.append(el('div', { class: 'chips-row' }, d.windows.map((w) =>
      el('span', { class: 'chip' }, `libero ${w.from}–${w.to}`, el('small', { text: ' · ' + (w.minutes >= 60 ? Math.floor(w.minutes / 60) + ' h' + (w.minutes % 60 ? ' ' + (w.minutes % 60) : '') : w.minutes + ' min') })))));
    for (const g of d.upcoming) root.append(el('div', { class: 'day-label', text: g.label }), el('ul', { class: 'timeline' }, g.events.map(rowEv)));
    for (const e of d.errors) root.append(el('div', { class: 'errbox', text: '⚠️ ' + e }));
    return root;
  }

  function bar(pct, cls) { const b = el('div', { class: 'bar ' + (cls || '') }); const i = el('i'); b.append(i); requestAnimationFrame(() => { i.style.width = Math.max(2, Math.min(100, pct || 0)) + '%'; }); return b; }

  /* ---------- grafici (SVG disegnato nella pagina: il server manda solo i numeri) ---------- */
  const SVGNS = 'http://www.w3.org/2000/svg', AMB = '#ff9f1c', AMB2 = '#ffc46b', MUT = '#8d8071', GRID = '#2c261f', GOOD = '#9be15d', MONO = 'Space Mono, ui-monospace, monospace';
  function svg(tag, attrs, ...kids) {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    for (const kid of kids.flat()) if (kid != null) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return n;
  }
  const cum = (a) => { let t = 0; return a.map((v) => (t += v)); };
  const short = (n) => n >= 1000 ? (n / 1000).toFixed(1).replace('.', ',') + 'k' : String(Math.round(n));
  function chartBox(vb, label) {
    const w = svg('svg', { viewBox: `0 0 ${vb[0]} ${vb[1]}`, role: 'img', 'aria-label': label, preserveAspectRatio: 'xMidYMid meet' });
    w.style.width = '100%'; w.style.height = 'auto'; w.style.display = 'block'; w.style.overflow = 'visible';
    return w;
  }

  // andamento del mese: spesa accumulata giorno per giorno, con il mese scorso tratteggiato
  function paceChart(s) {
    const Wd = 360, H = 150, L = 4, R = 10, T = 18, B = 22, days = s.month_days, cur = cum(s.daily), prev = cum(s.daily_prev);
    const maxV = Math.max(1, cur[cur.length - 1] || 0, prev[prev.length - 1] || 0) * 1.08;
    const X = (d) => L + (d - 1) / Math.max(1, days - 1) * (Wd - L - R), Y = (v) => T + (1 - v / maxV) * (H - T - B);
    const g = chartBox([Wd, H], `Spese accumulate nel mese: ${money(cur[cur.length - 1] || 0)} finora`);
    g.append(svg('defs', {}, svg('linearGradient', { id: 'gpace', x1: 0, y1: 0, x2: 0, y2: 1 },
      svg('stop', { offset: '0', 'stop-color': AMB, 'stop-opacity': '.38' }), svg('stop', { offset: '1', 'stop-color': AMB, 'stop-opacity': '0' }))));
    for (const f of [0, .5, 1]) g.append(svg('line', { x1: L, x2: Wd - R, y1: Y(maxV * f / 1.08), y2: Y(maxV * f / 1.08), stroke: GRID, 'stroke-width': 1 }));
    g.append(svg('text', { x: L, y: 10, fill: MUT, 'font-size': 10, 'font-family': MONO }, short(maxV / 1.08) + ' €'));
    if (prev.length && prev[prev.length - 1] > 0) {
      g.append(svg('path', { d: prev.map((v, i) => `${i ? 'L' : 'M'}${X(i + 1).toFixed(1)} ${Y(v).toFixed(1)}`).join(''), fill: 'none', stroke: MUT, 'stroke-width': 1.5, 'stroke-dasharray': '3 4', 'stroke-linecap': 'round' }));
    }
    if (cur.length) {
      const pts = cur.map((v, i) => `${X(i + 1).toFixed(1)} ${Y(v).toFixed(1)}`);
      g.append(svg('path', { d: `M${X(1).toFixed(1)} ${Y(0)} L${pts.join(' L')} L${X(cur.length).toFixed(1)} ${Y(0)} Z`, fill: 'url(#gpace)' }));
      g.append(svg('path', { d: 'M' + pts.join(' L'), fill: 'none', stroke: AMB, 'stroke-width': 2.4, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
      const ex = X(cur.length), ey = Y(cur[cur.length - 1]);
      g.append(svg('circle', { cx: ex, cy: ey, r: 9, fill: AMB, 'fill-opacity': .18 }), svg('circle', { cx: ex, cy: ey, r: 4, fill: '#fff4dc' }));
      g.append(svg('text', { x: Math.min(ex, Wd - R - 2), y: Math.max(ey - 13, 12), fill: AMB2, 'font-size': 11, 'font-family': MONO, 'text-anchor': ex > Wd * .7 ? 'end' : 'start' }, eurNum(cur[cur.length - 1]) + ' €'));
    }
    for (const d of [1, 8, 15, 22, days]) g.append(svg('text', { x: X(d), y: H - 6, fill: MUT, 'font-size': 10, 'font-family': MONO, 'text-anchor': d === 1 ? 'start' : d === days ? 'end' : 'middle' }, String(d)));
    return g;
  }

  // ultimi 6 mesi: entrate (verde) e spese (ambra)
  function monthsChart(ms) {
    const Wd = 360, H = 150, L = 4, R = 4, T = 18, B = 22, gw = (Wd - L - R) / ms.length, bw = Math.min(15, gw / 2.7);
    const maxV = Math.max(1, ...ms.map((m) => Math.max(m.expense, m.income)));
    const Y = (v) => T + (1 - v / maxV) * (H - T - B);
    const g = chartBox([Wd, H], 'Entrate e spese degli ultimi sei mesi');
    g.append(svg('line', { x1: L, x2: Wd - R, y1: H - B, y2: H - B, stroke: GRID, 'stroke-width': 1 }));
    ms.forEach((m, i) => {
      const cx = L + gw * i + gw / 2, last = i === ms.length - 1;
      for (const [v, dx, col, op] of [[m.income, -bw / 2 - 1.5, GOOD, .85], [m.expense, bw / 2 + 1.5, AMB, 1]]) {
        const h = Math.max(v > 0 ? 2 : 0, (H - B) - Y(v));
        if (v > 0) g.append(svg('rect', { x: cx + dx - bw / 2, y: H - B - h, width: bw, height: h, rx: 3, fill: col, 'fill-opacity': last ? op : op * .62 }));
      }
      if (m.expense > 0) g.append(svg('text', { x: cx + bw / 2 + 1.5, y: Math.max(11, Y(m.expense) - 4), fill: last ? AMB2 : MUT, 'font-size': 9.5, 'font-family': MONO, 'text-anchor': 'middle' }, short(m.expense)));
      g.append(svg('text', { x: cx, y: H - 6, fill: last ? AMB2 : MUT, 'font-size': 10, 'font-family': MONO, 'text-anchor': 'middle' }, m.label));
    });
    return g;
  }

  function renderFinance(d) {
    const root = el('div');
    root.append(el('div', { class: 'balance' + (d.balance < 0 ? ' neg' : '') }, eurNum(d.balance), el('small', { text: '€' })),
      el('div', { class: 'kv' }, el('div', {}, 'Entrate', el('b', { text: money(d.income) })), el('div', {}, 'Spese', el('b', { text: money(d.expense) }))));
    const sr = d.series;
    if (sr && sr.daily && sr.daily.some((v) => v > 0)) {
      root.append(el('div', { class: 'sec2', text: 'Andamento del mese' }), paceChart(sr),
        el('div', { class: 'card-sub', text: 'linea piena: questo mese · tratteggio: il mese scorso' }));
    }
    if (sr && sr.months && sr.months.some((m) => m.expense || m.income)) {
      root.append(el('div', { class: 'sec2', text: 'Ultimi 6 mesi' }), monthsChart(sr.months),
        el('div', { class: 'chips-row' }, el('span', { class: 'chip good' }, 'entrate'), el('span', { class: 'chip' }, 'spese')));
    }
    if (d.categories.length) {
      root.append(el('div', { class: 'sec2', text: 'Dove vanno i soldi' }));
      for (const c of d.categories) root.append(el('div', { class: 'bar-row' }, el('div', { class: 'bar-label' }, el('span', { text: c.label }), el('span', { text: money(c.total) })), bar(c.pct)));
    }
    if (d.budgets.length) {
      root.append(el('div', { class: 'sec2', text: 'Budget' }));
      for (const b of d.budgets) root.append(el('div', { class: 'bar-row' }, el('div', { class: 'bar-label' }, el('span', { text: b.label }), el('span', { text: `${eurNum(b.spent)} / ${money(b.limit)}` })), bar(b.pct, b.pct >= 100 ? 'bad' : b.pct >= 80 ? 'warn' : 'good')));
    }
    if (d.last.length) {
      root.append(el('div', { class: 'sec2', text: 'Ultimi movimenti' }));
      for (const t of d.last) root.append(el('div', { class: 'tx' }, el('div', {}, t.label, el('small', { text: [t.date.slice(5).split('-').reverse().join('/'), t.note].filter(Boolean).join(' · ') })),
        el('div', { class: 'amt ' + (t.type === 'entrata' ? 'in' : ''), text: (t.type === 'entrata' ? '+' : '−') + money(t.amount) })));
    }
    if (!d.categories.length && !d.last.length) root.append(el('div', { class: 'empty', text: 'Nessun movimento questo mese. Prova: «ho speso 10 euro di pizza».' }));
    return root;
  }

  /* promemoria: casella da spuntare, con «annulla» per 5 secondi */
  function renderTasks(d) {
    if (!d.lists.length) return el('div', { class: 'empty', text: 'Nessun promemoria aperto. Prova: «aggiungimi un promemoria domani: …».' });
    const root = el('div');
    for (const g of d.lists) {
      if (d.lists.length > 1) root.append(el('div', { class: 'day-label', text: g.name }));
      root.append(el('ul', { class: 'tasks' }, g.items.map((t) => taskRow(t))));
    }
    if (d.hidden > 0) root.append(el('div', { class: 'card-sub', text: `e altri ${d.hidden}` }));
    return root;
  }
  function taskRow(t) {
    const chk = el('button', { class: 'chk', type: 'button', 'aria-label': 'Spunta: ' + t.title, title: 'Spunta' });
    const lab = el('span', { class: 'task-due ' + t.level, text: t.label });
    const body = el('div', { class: 'task-body' }, el('div', { class: 'task-title', text: t.title }), t.label ? lab : null);
    const li = el('li', { class: 'task' }, chk, body);
    let timer = 0;
    const undo = el('button', { class: 'undo', type: 'button', text: 'Annulla' });
    chk.addEventListener('click', async () => {
      if (li.dataset.busy) return;
      li.dataset.busy = '1'; li.classList.add('done');                       // subito a schermo; poi il server conferma
      try {
        const r = await api('/api/tasks/complete', { body: { uid: t.uid, list: t.list } });
        if (!r.ok) throw new Error(r.error || 'errore');
        lab.replaceWith(undo);
        timer = setTimeout(() => { li.classList.add('gone'); setTimeout(() => loadCard('tasks', true), 350); }, 5000);
      } catch (e) {
        li.classList.remove('done'); delete li.dataset.busy;
        body.append(el('div', { class: 'errbox', text: '⚠️ ' + (e.message || 'Non riesco a spuntarlo') }));
      }
    });
    undo.addEventListener('click', async () => {
      clearTimeout(timer); undo.disabled = true;
      try {
        const r = await api('/api/tasks/reopen', { body: { uid: t.uid, list: t.list } });
        if (!r.ok) throw new Error(r.error || 'errore');
        li.classList.remove('done'); delete li.dataset.busy; undo.replaceWith(lab); undo.disabled = false;
      } catch (e) { undo.disabled = false; undo.textContent = 'Riprova'; }
    });
    return li;
  }

  function renderNotes(d) {
    if (!d.items.length) return el('div', { class: 'empty', text: 'Nessuna nota. Prova: «prendi nota: latte, uova».' });
    return el('div', {}, d.items.map((n) => el('button', { class: 'note', onclick: () => ask('Leggimi la nota ' + n.title) },
      el('b', { text: n.title }), (n.preview || n.modified) ? el('span', { text: [n.preview, ago(n.modified)].filter(Boolean).join(' — ') }) : null)));
  }

  function renderNews(d) {
    if (!d.groups.length) return el('div', { class: 'empty', text: 'Nessuna notizia. Controlla i feed in Nextcloud News.' });
    return el('div', {}, d.groups.map((g) => el('div', { class: 'news-group' }, el('h3', { text: g.title }),
      g.items.map((it) => el(it.url ? 'a' : 'div', { class: 'news-item', href: it.url || null, target: it.url ? '_blank' : null, rel: it.url ? 'noopener noreferrer' : null },
        it.title, el('small', { text: [it.source, it.age].filter(Boolean).join(' · ') }))))));
  }

  function renderServer(d) {
    const root = el('div');
    for (const a of d.alerts || []) root.append(el('div', { class: 'alert' + (a.level === 'warn' ? ' warn' : '') }, safeHTML(a.html)));
    const t = d.temp.cpu, ramPct = d.mem.pct, diskPct = d.root && d.root.pct;
    const tile = (label, value, lvl) => el('div', { class: 'tile ' + (lvl || '') }, el('b', { text: value }), el('small', { text: label }));
    root.append(el('div', { class: 'tiles' },
      tile('CPU', d.cpu.pct == null ? '–' : num(d.cpu.pct) + '%', level(d.cpu.pct, 60, 85)),
      tile('RAM', ramPct == null ? '–' : num(ramPct) + '%', level(ramPct, 75, 90)),
      tile('Temp', t == null ? '–' : num(t) + '°', level(t, 70, 80)),
      tile('Disco', diskPct == null ? '–' : num(diskPct) + '%', level(diskPct, 80, 90))));
    const chips = el('div', { class: 'chips-row' });
    chips.append(el('span', { class: 'chip' }, `⏱ ${dur(d.uptime_s)}`));
    if (d.nextcloud) chips.append(el('span', { class: 'chip ' + (d.nextcloud.ok ? 'good' : 'bad'), title: d.nextcloud.why || '' }, d.nextcloud.ok ? `Nextcloud ✓${d.nextcloud.ms ? ' ' + d.nextcloud.ms + ' ms' : ''}` : 'Nextcloud ✗'));
    for (const m of d.mounts || []) chips.append(el('span', { class: 'chip ' + (m.mounted && !m.ro ? 'good' : 'bad') }, `${m.path} ${m.mounted ? (m.ro ? 'sola lettura' : (m.pct != null ? num(m.pct) + '%' : '✓')) : 'NON montato'}`));
    const p = d.power || {};
    if (p.ac != null) chips.append(el('span', { class: 'chip ' + (p.ac ? '' : 'bad') }, p.ac ? 'rete elettrica' : 'a batteria' + (p.battery && p.battery.pct != null ? ' ' + p.battery.pct + '%' : '')));
    for (const c of d.docker || []) chips.append(el('span', { class: 'chip ' + (c.state === 'running' ? 'good' : 'bad') }, c.name));
    for (const s of d.smart || []) chips.append(el('span', { class: 'chip ' + (s.status === 'PASSED' && !s.timeout ? 'good' : s.status === 'FAILED' ? 'bad' : '') }, `${(s.dev || '').split('/').pop()}${s.status === 'SKIPPED' ? ' (USB)' : ''}`));
    for (const n of d.net || []) chips.append(el('span', { class: 'chip ' + (n.up ? '' : 'warn') }, `${n.name}${n.up ? '' : ' ✗'}`));
    root.append(chips);
    root.append(el('div', { class: 'card-sub', text: `carico ${(d.cpu.load || []).map((x) => x.toFixed(2)).join(' ')} · RAM ${gb(d.mem.used)} / ${gb(d.mem.total)}${d.probe ? '' : ' · sonda host non attiva'}` }));
    return root;
  }

  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const CARDS = [
    { id: 'today', title: 'Agenda', render: renderToday, every: 60, meta: (d) => d.events.length ? plural(d.events.length, 'impegno', 'impegni') : 'libero' },
    { id: 'tasks', title: 'Promemoria', render: renderTasks, every: 60, meta: (d) => d.total ? (d.overdue ? `${d.overdue} scadut${d.overdue === 1 ? 'o' : 'i'}` : plural(d.total, 'aperto', 'aperti')) : 'tutto fatto' },
    { id: 'finance', title: 'Finanze', render: renderFinance, every: 300, meta: (d) => d.month_label.toLowerCase() },
    { id: 'notes', title: 'Note', render: renderNotes, every: 300, meta: (d) => plural(d.total, 'nota', 'note') },
    { id: 'news', title: 'Notizie', render: renderNews, every: 900, meta: (d) => plural(d.groups.length, 'argomento', 'argomenti') },
    { id: 'server', title: 'Sistema', render: renderServer, every: 60, meta: (d) => d.host },
  ];
  const cardState = {};

  function buildCards() {
    const host = $('#cards');
    CARDS.forEach((c, i) => {
      const btn = el('button', { class: 'mini', 'aria-label': 'Aggiorna ' + c.title, title: 'Aggiorna', onclick: () => loadCard(c.id, true) }, '↻');
      const meta = el('span', { class: 'r' });
      const body = el('div', { class: 'card-body' }, el('div', { class: 'skeleton' }), el('div', { class: 'skeleton mid' }), el('div', { class: 'skeleton short' }));
      const stamp = el('div', { class: 'updated' });
      const card = el('section', { class: 'card', id: 'card-' + c.id },
        el('div', { class: 'sec' }, el('span', { class: 'n', text: String(i + 1).padStart(2, '0') }), el('span', { class: 'ttl', text: c.title, role: 'button', tabindex: '0', title: 'Comprimi o espandi', onclick: () => toggleCollapse(c.id), onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleCollapse(c.id); } } }), el('span', { class: 'l' }), meta, btn), body, stamp);
      host.append(card);
      cardState[c.id] = { card, body, btn, meta, stamp, at: 0, loading: false, unavailable: false };
    });
  }

  async function loadCard(id, force) {
    const spec = CARDS.find((c) => c.id === id), st = cardState[id];
    if (!spec || !st || st.loading) return;
    if (ui && ui.hidden.includes(id) && !ALWAYS_LOADED.includes(id) && !force) return;                 // scheda nascosta: non pesa sul server
    st.loading = true; st.btn.classList.add('spin');
    try {
      const r = await api(`/api/cards/${id}${force ? '?refresh=1' : ''}`);
      if (r.unavailable) { st.unavailable = true; applyLayout(); return; }
      if (!r.ok) throw new Error(r.error || 'errore');
      st.body.replaceChildren(spec.render(r.data));
      st.meta.textContent = spec.meta(r.data);
      st.at = Date.now(); st.stamp.textContent = 'aggiornato alle ' + new Date().toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
      state.data[id] = r.data;
      if (id === 'today') renderHero(r.data);
      if (id === 'server') setStatus(r.data);
    } catch (e) {
      st.body.replaceChildren(el('div', { class: 'errbox' }, '⚠️ ' + (e.message || 'Errore di rete'), el('button', { onclick: () => loadCard(id, true) }, 'Riprova')));
    } finally { st.loading = false; st.btn.classList.remove('spin'); }
  }

  function refreshStale() {
    if (document.visibilityState !== 'visible') return;
    for (const c of CARDS) {
      const st = cardState[c.id];
      if (!st || st.loading || st.unavailable || (ui.hidden.includes(c.id) && !ALWAYS_LOADED.includes(c.id))) continue;
      if (Date.now() - st.at > c.every * 1000) loadCard(c.id, false);
    }
  }

  /* ---------- barra flottante e risposta sotto la sfera ---------- */
  const gbar = $('#gbar'), scrim = $('#scrim'), input = $('#input'), answerEl = $('#answer'), qEl = $('#q'), aEl = $('#a');
  const wide = () => window.innerWidth >= 940;

  function placeBar() {                       // al centro della sfera, sempre sopra la tastiera
    const r = $('#orb').getBoundingClientRect(), vv = window.visualViewport;
    const top0 = vv ? vv.offsetTop : 0, vh = vv ? vv.height : window.innerHeight;
    gbar.style.left = (r.left + r.width / 2) + 'px';
    gbar.style.top = Math.min(Math.max(r.top + r.height / 2, top0 + 70), top0 + vh - 80) + 'px';
  }
  function openBar() {
    if (state.barOpen) return;
    state.barOpen = true; gbar.hidden = false; scrim.hidden = false;
    if (!wide()) window.scrollTo(0, 0);
    placeBar(); input.focus(); setTimeout(() => { placeBar(); input.focus(); }, 150);   // subito (iOS apre la tastiera solo dentro il tocco) e un secondo tentativo
    setOrb('listening');
  }
  function closeBar() {
    if (!state.barOpen) return;
    state.barOpen = false; gbar.hidden = true; scrim.hidden = true; input.blur();
    if (!state.busy) setOrb('');
  }

  function setAnswer(question) { answerEl.hidden = false; answerEl.classList.remove('err'); qEl.textContent = question || ''; aEl.replaceChildren(); }
  function showError(msg) { answerEl.hidden = false; answerEl.classList.add('err'); aEl.replaceChildren(msg); }

  function showNotice(msg) { answerEl.hidden = false; answerEl.classList.remove('err'); qEl.textContent = ''; aEl.replaceChildren(msg); }
  const dots = () => el('span', { class: 'typing' }, el('i'), el('i'), el('i'));

  async function ask(text) { closeBar(); return send(text); }

  async function send(text) {
    text = (text || '').trim();
    if (!text || state.busy) return;
    state.busy = true; input.value = '';
    if (!wide()) window.scrollTo({ top: 0, behavior: 'smooth' });          // la risposta compare sotto la sfera, in alto
    setAnswer(text); aEl.append(dots());
    $('#btn-send').disabled = true; setOrb('thinking');
    try {
      handleReply(await api('/api/assistant', { body: { text, session: SESSION, spoken: state.tts } }));
    } catch (e) {
      showError('Non riesco a raggiungere il server. Controlla che Tailscale sia acceso e riprova.'); setOrb('');
    } finally { state.busy = false; $('#btn-send').disabled = false; }
  }

  async function sendVoice(wav) {
    state.busy = true;
    if (!wide()) window.scrollTo({ top: 0, behavior: 'smooth' });
    setAnswer(''); aEl.append(dots()); setOrb('thinking');
    try {
      const r = await apiBlob('/api/voice', wav);
      handleReply(r, { voice: true });
      qEl.textContent = r.heard || '';                                     // cosa ha capito: lo vedi solo a risposta data, non mentre ascolta
    } catch (e) {
      showError('Non riesco a raggiungere il server. Controlla che Tailscale sia acceso e riprova.'); setOrb('');
    } finally { state.busy = false; }
  }

  function handleReply(r, opts = {}) {
    confirmCtl = null;
    if (r.error) { showError('Richiesta non riuscita: ' + r.error); setOrb(''); return; }
    answerEl.hidden = false; answerEl.classList.toggle('err', !r.ok);
    aEl.replaceChildren(safeHTML(r.html));
    const ctl = r.confirm ? addConfirm(r.confirm) : null;
    const auto = !(r.confirm && r.confirm.listen === false);               // l'«annulla» dopo aver spuntato non fa riascoltare
    for (const id of r.refresh || []) loadCard(id, true);
    state.lastReply = r;
    if ((opts.voice || state.tts) && r.speak) speak(r.speak).then(() => { if (opts.voice && auto && ctl && confirmCtl === ctl) listenConfirm(); });
    else setOrb('pulse');
  }

  let confirmCtl = null;
  function addConfirm(c) {
    const box = el('div', { class: 'confirm' });
    const ctl = {
      busy: false, choices: c.choices || null, token: c.token,
      decide: async (value, voice, token) => {
        if (ctl.busy) return;
        ctl.busy = true; box.querySelectorAll('button').forEach((b) => { b.disabled = true; }); if (confirmCtl === ctl) confirmCtl = null; setOrb('thinking');
        try { handleReply(await api('/api/confirm', { body: { session: SESSION, token: token || c.token, yes: value, spoken: !!voice || state.tts } }), { voice: !!voice }); }
        catch (e) { showError('Non riesco a raggiungere il server.'); setOrb(''); }
      },
    };
    if (c.choices && c.choices.length) {                                    // più possibilità: un pulsante per ciascuna
      c.choices.forEach((ch, i) => box.append(el('button', { class: 'btn primary', type: 'button', text: `${i + 1}. ${ch.label}`, onclick: () => ctl.decide(true, false, ch.token) })));
      box.append(el('button', { class: 'btn', type: 'button', text: c.no || 'Annulla', onclick: () => ctl.decide(false, false, c.token) }));
    } else {
      box.append(el('button', { class: 'btn primary', type: 'button', text: c.yes || 'Sì', onclick: () => ctl.decide(true, false) }),
                 el('button', { class: 'btn', type: 'button', text: c.no || 'Annulla', onclick: () => ctl.decide(false, false) }));
    }
    aEl.append(box); confirmCtl = ctl;
    return ctl;
  }

  /* ---------- voce ---------- */
  const synth = 'speechSynthesis' in window ? window.speechSynthesis : null;
  const canVoice = () => !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && (window.AudioContext || window.webkitAudioContext));
  function setHint(text, live) { const h = $('#hint'); h.textContent = text || (canVoice() && !state.voiceBlocked ? 'TOCCA PER PARLARE' : 'TOCCA PER SCRIVERE'); h.classList.toggle('live', !!live); }
  function getAC() { if (!state.ac) { const AC = window.AudioContext || window.webkitAudioContext; state.ac = new AC(); } return state.ac; }

  function pickVoice() {                                  // la migliore voce italiana installata (quelle «migliorate» suonano molto meglio)
    if (!synth) return null;
    const it = synth.getVoices().filter((v) => v.lang && v.lang.toLowerCase().replace('_', '-').startsWith('it'));
    const score = (v) => (/premium|enhanced|migliorat|avanzat|natural|neural/i.test(v.name) ? 3 : 0) + (/siri/i.test(v.name) ? 2 : 0);
    return it.sort((x, y) => score(y) - score(x))[0] || null;
  }
  function chunks(text) {                                 // frasi corte: su iPhone le frasi lunghe si interrompono
    const out = []; let cur = '';
    for (const p of (text.match(/[^.!?;]+[.!?;]*\s*/g) || [text])) { if ((cur + p).length > 190 && cur) { out.push(cur); cur = ''; } cur += p; }
    if (cur.trim()) out.push(cur);
    return out;
  }
  function stopSpeaking() { if (synth) synth.cancel(); if (state.speakEnd) state.speakEnd(); }
  function speak(text) {
    return new Promise((resolve) => {
      if (!synth || !text) { resolve(); return; }
      synth.cancel();
      const parts = chunks(text); let i = 0, done = false, started = false;
      const finish = () => { if (done) return; done = true; state.speaking = false; state.speakEnd = null; setOrb(state.barOpen ? 'listening' : ''); resolve(); };
      state.speakEnd = finish;
      let guard = 0;
      const next = () => {
        clearTimeout(guard);
        if (done) return;
        if (i >= parts.length) { finish(); return; }
        const part = parts[i++], u = new SpeechSynthesisUtterance(part); u.lang = 'it-IT'; u.rate = 1.0; u.pitch = 1.0;
        const v = pickVoice(); if (v) u.voice = v;
        u.onstart = () => { started = true; state.speaking = true; setOrb('speaking'); };
        u.onend = next; u.onerror = next;
        guard = setTimeout(next, 4000 + part.length * 95);           // a volte la fine della frase non viene segnalata (anche su iPhone): non restare appesi
        synth.speak(u);
      };
      next();
      setTimeout(() => { if (!started) finish(); }, 3000);   // se il telefono non parte, non restare appesi
    });
  }
  function setTTS(on, announce) {
    state.tts = on; localStorage.setItem('edith_tts', on ? '1' : '0');
    const b = $('#btn-tts'); b.setAttribute('aria-pressed', on ? 'true' : 'false'); b.textContent = on ? '🔊' : '🔇';
    if (!on) stopSpeaking();
    if (on && announce) speak('Lettura a voce attivata');     // dentro un tocco: sblocca l'audio su iPhone
  }

  /* registrazione: l'audio si cattura nella pagina, si porta a 16 kHz e va al server come WAV (formato che Gemini legge sempre) */
  function toWav16k(parts, inRate) {
    let n = 0; for (const c of parts) n += c.length;
    const all = new Float32Array(n); let o = 0; for (const c of parts) { all.set(c, o); o += c.length; }
    const ratio = inRate / 16000, m = Math.floor(n / ratio), pcm = new Int16Array(m);
    for (let i = 0; i < m; i++) {                         // media dei campioni: evita i fruscii del semplice salto
      const a = Math.floor(i * ratio), z = Math.max(a + 1, Math.min(n, Math.floor((i + 1) * ratio)));
      let sum = 0; for (let k = a; k < z; k++) sum += all[k];
      const v = Math.max(-1, Math.min(1, sum / (z - a))); pcm[i] = v < 0 ? v * 32768 : v * 32767;
    }
    const buf = new ArrayBuffer(44 + m * 2), dv = new DataView(buf), w = (off, str) => { for (let i = 0; i < str.length; i++) dv.setUint8(off + i, str.charCodeAt(i)); };
    w(0, 'RIFF'); dv.setUint32(4, 36 + m * 2, true); w(8, 'WAVE'); w(12, 'fmt '); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
    dv.setUint32(24, 16000, true); dv.setUint32(28, 32000, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true); w(36, 'data'); dv.setUint32(40, m * 2, true);
    new Int16Array(buf, 44, m).set(pcm);
    return new Blob([buf], { type: 'audio/wav' });
  }

  async function apiBlob(path, blob) {
    const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 80000);
    try {
      const r = await fetch(path, { method: 'POST', credentials: 'same-origin', signal: ctl.signal, body: blob,
        headers: { 'X-Requested-With': 'edith', 'Content-Type': 'audio/wav', 'X-Session': SESSION } });
      if (r.status === 401) { location.reload(); throw new Error('non autorizzato'); }
      return await r.json();
    } finally { clearTimeout(timer); }
  }

  // Ascolta finché smetti di parlare (o tocchi di nuovo la sfera). Ritorna il WAV, oppure null se non ha sentito nulla.
  async function captureWav({ hint = 'TI ASCOLTO', maxMs = 20000, noSpeechMs = 6500, endSilenceMs = 1100 } = {}) {
    if (state.listening) return null;
    state.listening = true; stopSpeaking(); setHint(hint, true); setOrb('listening');
    let stream, src, proc, sink, ctx;
    const cleanup = () => {
      try { if (proc) { proc.onaudioprocess = null; proc.disconnect(); } if (src) src.disconnect(); if (sink) sink.disconnect(); } catch (e) { /* già chiuso */ }
      if (stream) stream.getTracks().forEach((t) => t.stop());
      if (navigator.audioSession) { try { navigator.audioSession.type = 'auto'; } catch (e) { /* non supportato */ } }
      big.level = 0; state.listening = false; state.stopCapture = state.abortCapture = null; setHint();
    };
    try {
      if (navigator.audioSession) { try { navigator.audioSession.type = 'play-and-record'; } catch (e) { /* non supportato */ } }
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      ctx = getAC(); if (ctx.state === 'suspended') await ctx.resume();
      src = ctx.createMediaStreamSource(stream); proc = ctx.createScriptProcessor(4096, 1, 1); sink = ctx.createGain(); sink.gain.value = 0;
      src.connect(proc); proc.connect(sink); sink.connect(ctx.destination);   // collegato, ma senza far uscire audio
    } catch (e) { cleanup(); setOrb(''); throw e; }
    const rate = ctx.sampleRate;
    return new Promise((resolve, reject) => {
      const parts = []; let started = false, lastVoice = 0, floor = null, heard = false, zeroSince = null, over = false;
      const t0 = performance.now();
      const end = (fn) => { if (over) return; over = true; cleanup(); fn(); };
      state.stopCapture = () => end(() => resolve(started && parts.length ? toWav16k(parts, rate) : null));   // secondo tocco: ho finito
      state.abortCapture = () => end(() => resolve(null));
      proc.onaudioprocess = (e) => {
        if (over) return;
        const copy = new Float32Array(e.inputBuffer.getChannelData(0)); parts.push(copy);
        let sum = 0, nz = false; for (let i = 0; i < copy.length; i++) { const v = copy[i]; sum += v * v; if (v !== 0) nz = true; }
        const rms = Math.sqrt(sum / copy.length), now = performance.now(), elapsed = now - t0;
        big.level = rms;
        if (nz) { heard = true; zeroSince = null; } else if (zeroSince == null) zeroSince = now;
        if (!heard && zeroSince != null && now - zeroSince > 1500) { end(() => reject(new Error('mic_silent'))); return; }   // iOS a volte apre un microfono muto
        floor = floor == null ? rms : Math.min(rms, floor * 1.02 + 0.0004);                                            // rumore di fondo: segue il minimo (sale piano, scende subito), così funziona anche se parli subito
        if (elapsed < 300) return;
        if (rms > Math.max(0.02, floor * 3)) { started = true; lastVoice = now; }
        if (started && now - lastVoice > endSilenceMs && elapsed > 900) end(() => resolve(toWav16k(parts, rate)));
        else if (!started && elapsed > noSpeechMs) end(() => resolve(null));
        else if (elapsed > maxMs) end(() => resolve(started ? toWav16k(parts, rate) : null));
      };
    });
  }

  function micError(e, auto) {
    setOrb('');
    if (auto) { showNotice('Tocca la sfera per rispondere.'); return; }              // partito da solo (conferma, presentazione): su iPhone può servire un tocco
    const n = (e && e.name) || '';
    if (n === 'NotAllowedError' || n === 'SecurityError') {
      state.voiceBlocked = true; setHint();
      showError('Il microfono è bloccato per questo sito. Consentilo nelle impostazioni del browser (su iPhone: Impostazioni → Safari → Microfono). Intanto puoi scrivere con il tasto ⌨.');
    } else if (n === 'NotFoundError') showError('Non trovo nessun microfono su questo dispositivo.');
    else if (e && e.message === 'mic_silent') showError('Il microfono non sente nulla. Chiudi e riapri l\'app e riprova (a volte succede dopo che il telefono ha parlato).');
    else showError('Non riesco a usare il microfono (' + (n || 'errore') + '). Puoi scrivere con il tasto ⌨.');
  }

  async function startVoice(auto) {
    if (state.busy || state.listening) return;
    closeBar();
    let wav;
    try { wav = await captureWav({ hint: 'TI ASCOLTO' }); } catch (e) { micError(e, auto); return; }
    if (!wav) { setOrb(''); showNotice('Non ti ho sentito. Tocca la sfera e parla.'); return; }
    await sendVoice(wav);
  }

  // risposta «sì» / «no» a voce: solo frasi brevi e inequivocabili, altrimenti restano i pulsanti
  function parseYesNo(text) {
    const t = (text || '').toLowerCase().replace(/[.,!?;:]/g, ' ').replace(/\s+/g, ' ').trim();
    if (!t || t.split(' ').length > 4) return null;
    const yes = /^(s[iì]|certo|ok|okay|va bene|confermo|conferma|fallo|procedi|vai|esatto|d'accordo|sposta|cancella)( |$)/.test(t);
    const no = /^(no|annulla|lascia( stare| perdere)?|non farlo|ferma(ti)?|stop|niente|negativo)( |$)/.test(t);
    return yes === no ? null : yes;
  }
  // «il primo», «secondo», «2», «uno»… -> indice (0, 1, …); -1 = annulla; null = non capito
  function parseChoice(text, n) {
    const t = (text || '').toLowerCase().replace(/[.,!?;:]/g, ' ').replace(/\s+/g, ' ').trim();
    if (!t || t.split(' ').length > 5) return null;
    if (/^(annulla|nessun[oa]?|no|lascia|niente|ferma)/.test(t)) return -1;
    const forms = [/(^| )(prim[oa]|uno|1|a)( |$)/, /(^| )(second[oa]|due|2|b)( |$)/, /(^| )(terz[oa]|tre|3|c)( |$)/, /(^| )(quart[oa]|quattro|4|d)( |$)/];
    const hit = forms.slice(0, n).map((f, i) => (f.test(t) ? i : -2)).filter((i) => i >= 0);
    return hit.length === 1 ? hit[0] : null;
  }
  async function listenConfirm() {
    const ctl = confirmCtl;
    if (!ctl || state.listening || state.busy) return;
    for (let attempt = 0; attempt < 2 && confirmCtl === ctl; attempt++) {
      let wav;
      try { wav = await captureWav({ hint: ctl.choices ? 'QUALE?' : 'SÌ O NO?', noSpeechMs: 7000, endSilenceMs: 800, maxMs: 6000 }); } catch (e) { micError(e, true); return; }
      if (!wav) break;
      let text = ''; try { text = (await apiBlob('/api/transcribe', wav)).text || ''; } catch (e) { /* restano i pulsanti */ }
      if (ctl.choices) {                                                   // scelta tra più promemoria
        const i = parseChoice(text, ctl.choices.length);
        if (i === -1) { ctl.decide(false, true); return; }
        if (i !== null) { ctl.decide(true, true, ctl.choices[i].token); return; }
        if (attempt === 0) await speak('Scusa, non ho capito. Dimmi il primo, il secondo, oppure annulla.');
        continue;
      }
      const yn = parseYesNo(text);
      if (yn !== null) { ctl.decide(yn, true); return; }
      if (attempt === 0) await speak('Scusa, non ho capito. Dimmi sì o no, oppure usa i pulsanti.');
    }
    setOrb('');
  }

  // «Presentati»: musica scelta da te (web/static/intro.mp3, se c'è) e la voce di EDITH che saluta; poi si mette ad ascoltare
  async function playIntro() {
    if (state.listening || state.busy) return;
    const nick = state.me && (state.me.nick || state.me.owner);
    const line = `Ehi${nick ? ' ' + nick : ''}, sono ${SPOKEN_NAME}, il tuo assistente virtuale. Cosa ti serve?`;
    stopSpeaking(); if (synth) synth.speak(new SpeechSynthesisUtterance(''));     // dentro il tocco: sblocca la voce
    let gain = null, music = null;
    try {
      const ac = getAC(); if (ac.state === 'suspended') await ac.resume();
      music = new Audio(INTRO_URL); music.preload = 'auto';
      const node = ac.createMediaElementSource(music); gain = ac.createGain(); gain.gain.value = .9; node.connect(gain); gain.connect(ac.destination);
      await music.play();
    } catch (e) { music = null; }                                                  // brano mancante o bloccato: solo la voce
    const level = (v, secs = .35) => { if (gain) { const t = gain.context.currentTime; gain.gain.cancelScheduledValues(t); gain.gain.setValueAtTime(gain.gain.value, t); gain.gain.linearRampToValueAtTime(v, t + secs); } };
    const cap = music ? setTimeout(() => { level(0, 1.5); setTimeout(() => music.pause(), 1600); }, INTRO_MAX_MS) : 0;
    setOrb('speaking');
    if (music) await sleep(2200);                                                  // prima il brano, poi la voce sopra
    level(.22); await speak(line); level(.8, .5);
    if (music) { await sleep(2500); level(0, 1.8); await sleep(1900); music.pause(); clearTimeout(cap); }
    startVoice(true);
  }

  /* ---------- collegamenti ---------- */
  function onOrb() {
    if (state.listening) { if (state.stopCapture) state.stopCapture(); return; }       // secondo tocco: ho finito di parlare
    if (state.speaking) { stopSpeaking(); return; }                                    // interrompe la voce
    if (state.busy) return;
    if (confirmCtl) { listenConfirm(); return; }                                       // c'è una conferma in attesa: rispondi sì o no
    if (state.barOpen) { closeBar(); return; }
    if (canVoice() && !state.voiceBlocked) startVoice(); else openBar();
  }
  function stopAll() { if (state.abortCapture) state.abortCapture(); stopSpeaking(); closeBar(); }

  function setupBar() {
    $('#orb').addEventListener('click', onOrb);
    $('#btn-kbd').addEventListener('click', () => { if (state.listening) state.abortCapture && state.abortCapture(); stopSpeaking(); openBar(); });
    $('#btn-intro').addEventListener('click', playIntro);
    const mic = $('#btn-mic'); mic.hidden = !canVoice(); mic.addEventListener('click', () => { closeBar(); startVoice(); });
    scrim.addEventListener('click', closeBar);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { stopAll(); closePanel(); exitDesk(); } });
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') stopAll(); });
    gbar.addEventListener('submit', (e) => {
      e.preventDefault();
      const t = input.value.trim(); if (!t) return;
      if (wide()) input.focus(); else closeBar();       // sul telefono la tastiera si chiude: la risposta resta visibile sotto la sfera
      send(t);
    });
    input.addEventListener('focus', () => { if (!state.busy) setOrb('listening'); });
    input.addEventListener('blur', () => { if (!state.busy && !state.barOpen && !state.speaking) setOrb(''); });
    const fit = () => { if (state.barOpen) placeBar(); };
    window.addEventListener('resize', fit);
    if (window.visualViewport) { window.visualViewport.addEventListener('resize', fit); window.visualViewport.addEventListener('scroll', fit); }
  }

  /* ---------- personalizzazione: schede, tasti rapidi, frasi, scrivania ---------- */
  const UI_KEY = 'edith_ui';
  const QUICK_CATALOG = [
    ['Cosa faccio oggi?', 'Oggi'], ['Quando sono libero domani?', 'Libero domani'], ['Che promemoria ho?', 'Promemoria'],
    ['Quanto ho speso questo mese?', 'Spese'], ['Ultime news', 'News'], ['Come sta il server?', 'Server'], ['Cosa ricordi di me?', 'Memoria'],
  ];
  const ALWAYS_LOADED = ['today', 'server'];            // alimentano la testata (orologio, meteo, stato): si caricano anche con la scheda nascosta
  let ui = null;

  function normalizeUI(v) {
    const ids = CARDS.map((c) => c.id), known = QUICK_CATALOG.map((q) => q[0]);
    const arr = (x) => (Array.isArray(x) ? x.filter((i) => typeof i === 'string') : []);
    const uniq = (a, ok) => a.filter((id, i) => ok.includes(id) && a.indexOf(id) === i);
    return {
      order: uniq(arr(v.order), ids), hidden: uniq(arr(v.hidden), ids), collapsed: uniq(arr(v.collapsed), ids),
      quick: Array.isArray(v.quick) ? uniq(arr(v.quick), known).slice(0, 4) : QUICK_CATALOG.slice(0, 3).map((q) => q[0]),
      quotes: v.quotes !== false, dim: v.dim !== false,
    };
  }
  function loadUI() { try { ui = normalizeUI(JSON.parse(localStorage.getItem(UI_KEY) || '{}') || {}); } catch (e) { ui = normalizeUI({}); } }
  function saveUI() { try { localStorage.setItem(UI_KEY, JSON.stringify(ui)); } catch (e) { /* memoria piena o bloccata: vale per questa sessione */ } }
  const effectiveOrder = () => { const ids = CARDS.map((c) => c.id); return [...ui.order, ...ids.filter((id) => !ui.order.includes(id))]; };

  function applyLayout() {                                // ordine, schede nascoste o compresse, numerazione
    const host = $('#cards'); let n = 0;
    for (const id of effectiveOrder()) {
      const st = cardState[id]; if (!st) continue;
      host.append(st.card);                               // append sposta il nodo: così si riordina
      const hide = st.unavailable || ui.hidden.includes(id);
      st.card.hidden = hide; st.card.classList.toggle('collapsed', ui.collapsed.includes(id));
      if (!hide) { n += 1; const num = st.card.querySelector('.n'); if (num) num.textContent = String(n).padStart(2, '0'); }
    }
  }
  function toggleCollapse(id) { ui.collapsed = ui.collapsed.includes(id) ? ui.collapsed.filter((x) => x !== id) : [...ui.collapsed, id]; saveUI(); applyLayout(); }

  /* testata: meteo a tre giorni, alba e tramonto, prossimo impegno */
  function renderHero(d) {
    setDateLine(d.date_label); setHello(d.greeting);
    const box = $('#wx3'); box.replaceChildren();
    let days = d.weather_days || [];
    if (!days.length && d.weather) days = [{ label: 'oggi', emoji: d.weather.emoji, tmin: d.weather.tmin, tmax: d.weather.tmax, desc: d.weather.label, pop: d.weather.pop }];
    for (const w of days) {
      const rain = w.pop != null && w.pop >= 50 ? el('span', { class: 'rain', text: ` ☔${w.pop}%` }) : null;
      box.append(el('span', { class: 'wxi', title: w.desc + (w.pop != null ? ` · pioggia ${w.pop}%` : '') }, el('b', { text: w.label }), ` ${w.emoji} ${w.tmin}–${w.tmax}°`, rain));
    }
    $('#sunline').textContent = d.sun && (d.sun.rise || d.sun.set) ? `🌅 ${d.sun.rise || '–'}   🌇 ${d.sun.set || '–'}` : '';
    updateNext();
  }
  const hm = (dt) => dt.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
  function inLabel(mins) {
    if (mins <= 0) return 'ora';
    if (mins < 60) return `tra ${mins} min`;
    const h = Math.floor(mins / 60), r = mins % 60;
    return `tra ${h} h${r ? ' ' + r + ' min' : ''}`;
  }
  function nextEvent(d, now) {
    const timed = (d.events || []).filter((e) => e.start_iso && e.end_iso).map((e) => ({ e, s: new Date(e.start_iso), f: new Date(e.end_iso) })).sort((a, b) => a.s - b.s);
    const cur = timed.find((x) => x.s <= now && now < x.f);
    if (cur) return { when: 'in corso', title: cur.e.title, loc: cur.e.location, extra: `fino alle ${hm(cur.f)}`, now: true };
    const nxt = timed.find((x) => x.s > now);
    if (nxt) { const m = Math.ceil((nxt.s - now) / 60000); return { when: m < 360 ? inLabel(m) : `alle ${hm(nxt.s)}`, title: nxt.e.title, loc: nxt.e.location }; }
    for (const g of d.upcoming || []) {                   // niente più oggi: il primo dei prossimi giorni
      const t = (g.events || []).filter((e) => e.start_iso).map((e) => ({ e, s: new Date(e.start_iso) })).sort((a, b) => a.s - b.s)[0];
      if (t) return { when: `${g.label === 'Domani' ? 'domani' : g.label.split(' ')[0]} ${hm(t.s)}`, title: t.e.title, loc: t.e.location };
    }
    return null;
  }
  function updateNext() {
    const box = $('#next'), d = state.data.today;
    if (!d) { box.hidden = true; return; }
    const n = nextEvent(d, new Date());
    box.className = 'next' + (n && n.now ? ' now' : ''); box.hidden = false; box.replaceChildren();
    if (!n) { box.append(el('span', { class: 'when', text: 'libero' }), el('span', { class: 't', text: 'nessun impegno in vista' })); return; }
    box.append(el('span', { class: 'when', text: n.when }), el('span', { class: 't', text: n.title }));
    if (n.extra) box.append(el('span', { class: 'loc', text: n.extra }));
    if (n.loc) box.append(el('span', { class: 'loc', text: '📍 ' + n.loc }));
  }

  /* tasti rapidi sotto la sfera */
  function renderQuick() {
    const box = $('#quick'); box.replaceChildren();
    for (const text of ui.quick) {
      const q = QUICK_CATALOG.find((x) => x[0] === text);
      if (q) box.append(el('button', { type: 'button', text: q[1], title: q[0], onclick: () => ask(q[0]) }));
    }
  }

  /* frasi: casuali, ma quelle di contesto compaiono più spesso quando il momento è giusto */
  let quotes = null, lastQ = [];
  try { lastQ = JSON.parse(sessionStorage.getItem('edith_q') || '[]').filter((x) => typeof x === 'string').slice(0, 12); } catch (e) { lastQ = []; }
  async function loadQuotes() {
    if (quotes) return quotes;
    try { const r = await fetch('/static/frasi.json', { credentials: 'same-origin' }); const j = await r.json(); quotes = Array.isArray(j.frasi) ? j.frasi.filter((f) => f && typeof f.t === 'string') : []; } catch (e) { quotes = []; }
    return quotes;
  }
  function flags(now = new Date()) {                      // cosa è vero adesso: i nomi sono quelli usati in frasi.json
    const h = now.getHours(), dow = now.getDay(), mo = now.getMonth(), f = {};
    f.mattina = h >= 5 && h < 12; f.pomeriggio = h >= 12 && h < 18; f.sera = h >= 18 && h < 23; f.notte = h >= 23 || h < 5;
    f.lunedi = dow === 1; f.martedi = dow === 2; f.mercoledi = dow === 3; f.giovedi = dow === 4; f.venerdi = dow === 5; f.sabato = dow === 6; f.domenica = dow === 0;
    f.weekend = dow === 0 || dow === 6; f.inverno = [11, 0, 1, 2].includes(mo); f.estate = [5, 6, 7, 8].includes(mo);
    const t = state.data.today, tk = state.data.tasks, sv = state.data.server;
    if (t) {
      const w = t.weather, lab = w ? String(w.label).toLowerCase() : '', evs = t.events || [], dom = (t.upcoming || []).find((g) => g.label === 'Domani');
      f.pioggia = /piogg|rovesc|temporal|piovigg/.test(lab); f.neve = /neve|nevisc/.test(lab); f.sereno = /sereno/.test(lab);
      f.freddo = !!w && w.tmax <= 8; f.caldo = !!w && w.tmax >= 28;
      f.esame = [...evs, ...((dom && dom.events) || [])].some((e) => /esam/i.test(e.title));
      f.allenamento = evs.some((e) => /allenament|calcio|partita/i.test(e.title + ' ' + e.calendar));
      f.lezione = evs.some((e) => /lezion|\blez\b|\blab\b|universit/i.test(e.title + ' ' + e.calendar));
      f.giornata_libera = evs.length === 0; f.giornata_piena = evs.length >= 5;
    }
    if (tk) { f.scaduti = tk.overdue > 0; f.tutto_fatto = tk.total === 0; }
    if (sv) {
      const bad = (sv.alerts && sv.alerts.length) || (sv.nextcloud && sv.nextcloud.ok === false) || (sv.mounts || []).some((m) => !m.mounted || m.ro);
      f.server_avviso = !!bad; f.server_ok = !bad;
    }
    return f;
  }
  function pickQuote() {
    if (!quotes || !quotes.length) return null;
    const f = flags(), fresh = quotes.filter((q) => !lastQ.includes(q.t)), base = fresh.length ? fresh : quotes;
    const hasC = (q) => Array.isArray(q.c) && q.c.length > 0;
    const ctx = base.filter((q) => hasC(q) && q.c.every((k) => f[k])), gen = base.filter((q) => !hasC(q));
    const pool = ctx.length && (Math.random() < 0.6 || !gen.length) ? ctx : (gen.length ? gen : base);
    const text = pool[Math.floor(Math.random() * pool.length)].t;
    lastQ = [text, ...lastQ].slice(0, 12);
    try { sessionStorage.setItem('edith_q', JSON.stringify(lastQ)); } catch (e) { /* pazienza */ }
    return text;
  }
  async function showQuote() {
    const box = $('#quote');
    if (!ui.quotes) { box.hidden = true; return; }
    await loadQuotes();
    const t = pickQuote();
    if (t) { box.textContent = t; box.hidden = false; } else box.hidden = true;
  }

  /* pannello «Aspetto» */
  const panel = $('#panel'), pscrim = $('#pscrim');
  function openPanel() { closeBar(); buildPanel(); panel.hidden = false; pscrim.hidden = false; }
  function closePanel() { panel.hidden = true; pscrim.hidden = true; }
  function changed(rebuild = true) { saveUI(); applyLayout(); renderQuick(); if (rebuild) buildPanel(); }
  function buildPanel() {
    const keep = panel.scrollTop;
    const sw = (on, label, fn) => { const b = el('button', { class: 'switch', type: 'button', role: 'switch', 'aria-checked': on ? 'true' : 'false', 'aria-label': label }); b.addEventListener('click', () => fn(b.getAttribute('aria-checked') !== 'true')); return b; };
    const root = el('div', {}, el('button', { class: 'p-close', type: 'button', 'aria-label': 'Chiudi', onclick: closePanel }, '×'), el('h2', { text: 'Aspetto' }));

    const cardsBox = el('div', { class: 'p-sec' }, el('h3', {}, 'Schede ', el('small', { text: '· tocca il titolo di una scheda per comprimerla' })));
    const order = effectiveOrder();
    order.forEach((id, i) => {
      const spec = CARDS.find((c) => c.id === id), off = ui.hidden.includes(id);
      const move = (dir) => { const o = effectiveOrder(), k = o.indexOf(id); [o[k], o[k + dir]] = [o[k + dir], o[k]]; ui.order = o; changed(); };
      cardsBox.append(el('div', { class: 'p-row' + (off ? ' off' : '') },
        el('button', { class: 'p-btn', type: 'button', 'aria-label': 'Sposta su ' + spec.title, text: '▲', disabled: i === 0, onclick: () => move(-1) }),
        el('button', { class: 'p-btn', type: 'button', 'aria-label': 'Sposta giù ' + spec.title, text: '▼', disabled: i === order.length - 1, onclick: () => move(1) }),
        el('span', { class: 'nm', text: spec.title }),
        sw(!off, (off ? 'Mostra ' : 'Nascondi ') + spec.title, (on) => {
          ui.hidden = on ? ui.hidden.filter((x) => x !== id) : [...ui.hidden, id];
          changed(); if (on && cardState[id] && !cardState[id].at) loadCard(id, false);
        })));
    });
    root.append(cardsBox);

    const quick = el('div', { class: 'p-sec' }, el('h3', {}, 'Tasti rapidi ', el('small', { text: '· fino a 4' })), el('div', { class: 'p-checks' }, QUICK_CATALOG.map((q) => {
      const on = ui.quick.includes(q[0]);
      return el('button', { type: 'button', text: q[1], 'aria-pressed': on ? 'true' : 'false', onclick: () => {
        if (on) ui.quick = ui.quick.filter((x) => x !== q[0]); else if (ui.quick.length < 4) ui.quick = [...ui.quick, q[0]]; else return;
        changed();
      } });
    })));
    root.append(quick);

    const screen = el('div', { class: 'p-sec' }, el('h3', { text: 'Schermo' }),
      el('div', { class: 'p-row' }, el('span', { class: 'nm', text: 'Mostra le frasi' }), sw(ui.quotes, 'Mostra le frasi', (on) => { ui.quotes = on; saveUI(); showQuote(); buildPanel(); })),
      el('div', { class: 'p-row' }, el('span', { class: 'nm', text: 'Attenua di notte in modalità scrivania (22–07)' }), sw(ui.dim, 'Attenua di notte', (on) => { ui.dim = on; saveUI(); updateDimmer(); buildPanel(); })),
      el('div', { class: 'p-actions' },
        el('button', { class: 'btn', type: 'button', text: 'Un\'altra frase', onclick: showQuote }),
        el('button', { class: 'btn primary', type: 'button', text: '☾ Modalità scrivania', onclick: enterDesk }),
        el('button', { class: 'btn', type: 'button', text: 'Ripristina', onclick: () => { ui = normalizeUI({}); saveUI(); applyLayout(); renderQuick(); showQuote(); buildPanel(); } })));
    root.append(screen);
    root.append(el('p', { class: 'card-sub', text: 'Le scelte restano su questo dispositivo.' }));
    panel.replaceChildren(root); panel.scrollTop = keep;
  }

  /* modalità scrivania: orologio grande, prossimo impegno e frase; schermo acceso e, di notte, attenuato */
  let wakeLock = null, fullscreen = false;
  async function lockScreen() {
    try {
      if (navigator.wakeLock && state.desk && document.visibilityState === 'visible' && !wakeLock) {
        wakeLock = await navigator.wakeLock.request('screen');
        wakeLock.addEventListener('release', () => { wakeLock = null; });
      }
    } catch (e) { /* non supportato o negato: lo schermo seguirà le impostazioni del telefono */ }
  }
  function unlockScreen() { try { if (wakeLock) wakeLock.release(); } catch (e) { /* già rilasciato */ } wakeLock = null; }
  function updateDimmer() { const h = new Date().getHours(); $('#dimmer').hidden = !(state.desk && ui && ui.dim && (h >= 22 || h < 7)); }
  function enterDesk() {
    if (state.desk) return;
    closeBar(); closePanel(); state.desk = true; document.body.classList.add('desk'); $('#desk-exit').hidden = false;
    window.scrollTo(0, 0); updateDimmer(); lockScreen(); wake(); setTimeout(relayout, 60);
    if (!(window.matchMedia && matchMedia('(pointer: coarse)').matches) && document.documentElement.requestFullscreen) {
      document.documentElement.requestFullscreen().then(() => { fullscreen = true; }).catch(() => { fullscreen = false; });
    }
  }
  function exitDesk() {
    if (!state.desk) return;
    state.desk = false; document.body.classList.remove('desk'); $('#desk-exit').hidden = true; $('#dimmer').hidden = true; unlockScreen();
    if (document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(() => { /* già uscito */ });
    fullscreen = false; setTimeout(relayout, 60); wake();
  }
  function setupCustomisation() {
    $('#btn-panel').addEventListener('click', openPanel);
    pscrim.addEventListener('click', closePanel);
    $('#btn-desk').addEventListener('click', enterDesk);
    $('#desk-exit').addEventListener('click', exitDesk);
    $('#quote').addEventListener('click', showQuote);
    document.addEventListener('fullscreenchange', () => { if (!document.fullscreenElement && state.desk && fullscreen) exitDesk(); });
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') { lockScreen(); updateNext(); updateDimmer(); } });
  }

  async function init() {
    loadUI();
    tickClock(); setDateLine(); setHint();
    buildCards(); applyLayout(); setupBar(); renderQuick(); setupCustomisation();
    big.resize(); wake();
    if (!synth) $('#btn-tts').hidden = true;
    else { setTTS(localStorage.getItem('edith_tts') === '1', false); $('#btn-tts').addEventListener('click', () => setTTS(!state.tts, true)); }
    if (!synth) $('#btn-intro').hidden = true;
    $('#btn-refresh').addEventListener('click', () => CARDS.forEach((c) => loadCard(c.id, true)));
    try { state.me = await api('/api/me'); if (state.me.assistant) { document.title = state.me.assistant; } setHello(''); } catch (e) { return; }
    const loads = CARDS.map((c) => loadCard(c.id, false));
    const forQuote = ['today', 'tasks', 'server'].map((id) => loads[CARDS.findIndex((c) => c.id === id)]);
    Promise.race([Promise.allSettled(forQuote), sleep(2500)]).then(showQuote);   // la frase si sceglie quando conosco il momento (agenda, server, promemoria)
    setInterval(refreshStale, 20000);
    document.addEventListener('visibilitychange', refreshStale);
  }
  (document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve()).then(init);
})();
