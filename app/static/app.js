/* Pobrefy — SPA sem build step */
'use strict';

// ------------------------------------------------------------------ utils
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const isMobile = () => matchMedia('(max-width: 760px)').matches;
const icon = (id, cls = 'i') => `<svg class="${cls}"><use href="#i-${id}"/></svg>`;
const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem('npeh.' + k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem('npeh.' + k, JSON.stringify(v)); } catch { /* sem storage */ } },
};

function fmt(sec) {
  if (!sec || isNaN(sec)) return '0:00';
  sec = Math.floor(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}
function fmtLong(sec) {
  sec = Math.round(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  if (h) return `${h} h ${m} min`;
  return `${m} min ${sec % 60} s`;
}
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const ago = (ts) => {
  const d = Math.floor(Date.now() / 1000) - ts;
  if (d < 60) return 'agora mesmo';
  if (d < 3600) return `há ${Math.floor(d / 60)} min`;
  if (d < 86400) return `há ${Math.floor(d / 3600)} h`;
  if (d < 86400 * 30) return `há ${Math.floor(d / 86400)} dias`;
  return new Date(ts * 1000).toLocaleDateString('pt-PT', { day: 'numeric', month: 'short', year: 'numeric' });
};

async function api(path, opts = {}) {
  const o = { method: opts.method || (opts.json !== undefined ? 'POST' : 'GET'), headers: {} };
  if (opts.json !== undefined) { o.body = JSON.stringify(opts.json); o.headers['Content-Type'] = 'application/json'; }
  const r = await fetch('/api' + path, o);
  if (r.status === 401 && path !== '/login') { showLogin(); throw new Error('auth'); }
  if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail || r.statusText); }
  return r.json();
}

let toastTimer;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 2800);
}

// ------------------------------------------------------------------ arte
const coverUrl = (albumId) => `/api/cover/${albumId}`;
function art(albumId, has, cls = '') {
  return has && albumId
    ? `<img src="${coverUrl(albumId)}" class="${cls}" loading="lazy" alt="">`
    : `<div class="ph ${cls}">${icon('note')}</div>`;
}
function artistArt(a, cls = '') {
  return a.cover_album
    ? `<img src="${coverUrl(a.cover_album)}" class="round ${cls}" loading="lazy" alt="">`
    : `<div class="ph round ${cls}">${icon('user')}</div>`;
}
function playlistArt(p, cls = '') {
  const c = p.covers || [];
  if (c.length >= 4) return `<div class="mosaic ${cls}">${c.slice(0, 4).map((id) => `<img src="${coverUrl(id)}" loading="lazy" alt="">`).join('')}</div>`;
  if (c.length) return `<img src="${coverUrl(c[0])}" class="${cls}" loading="lazy" alt="">`;
  return `<div class="ph ${cls}">${icon('note')}</div>`;
}
const likedArt = (cls = '') => `<div class="liked-cover ${cls}">${icon('heart-fill')}</div>`;

async function dominantColor(src) {
  return new Promise((res) => {
    const img = new Image();
    img.onload = () => {
      try {
        const c = document.createElement('canvas'); c.width = c.height = 12;
        const x = c.getContext('2d'); x.drawImage(img, 0, 0, 12, 12);
        const d = x.getImageData(0, 0, 12, 12).data;
        let r = 0, g = 0, b = 0, n = 0;
        for (let i = 0; i < d.length; i += 4) {
          const mx = Math.max(d[i], d[i + 1], d[i + 2]), mn = Math.min(d[i], d[i + 1], d[i + 2]);
          const w = 1 + (mx - mn) / 32; // valoriza cores saturadas
          r += d[i] * w; g += d[i + 1] * w; b += d[i + 2] * w; n += w;
        }
        r /= n; g /= n; b /= n;
        const k = Math.max(r, g, b) > 170 ? 170 / Math.max(r, g, b) : 1; // evita brancos
        res(`rgb(${Math.round(r * k)},${Math.round(g * k)},${Math.round(b * k)})`);
      } catch { res(null); }
    };
    img.onerror = () => res(null);
    img.src = src;
  });
}

// ------------------------------------------------------------------ registo de listas
const lists = {};       // key -> [tracks]
let listSeq = 0;
function registerList(tracks, ctx) { const k = 'l' + ++listSeq; lists[k] = { tracks, ctx }; return k; }

// ------------------------------------------------------------------ player
const audio = $('#audio');
const P = {
  queue: [], idx: -1, original: null, manual: 0,
  shuffle: store.get('shuffle', false), repeat: store.get('repeat', 'off'),
  ctx: null, counted: false,
};
const current = () => P.queue[P.idx];

function shuffleArr(a) { a = a.slice(); for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; }

function playList(tracks, start = 0, ctx = null, forceShuffle = null) {
  if (!tracks.length) return toast('Não há músicas para tocar');
  if (forceShuffle !== null) { P.shuffle = forceShuffle; store.set('shuffle', P.shuffle); }
  P.ctx = ctx; P.manual = 0;
  if (P.shuffle) {
    P.original = tracks.slice();
    const first = forceShuffle && start === 0 ? Math.floor(Math.random() * tracks.length) : start;
    const rest = tracks.filter((_, i) => i !== first);
    P.queue = [tracks[first], ...shuffleArr(rest)];
    P.idx = 0;
  } else {
    P.original = null; P.queue = tracks.slice(); P.idx = start;
  }
  load(true);
}

function load(autoplay = true, at = 0) {
  const t = current();
  if (!t) return;
  P.counted = false;
  audio.src = `/api/stream/${t.id}`;
  if (at) audio.addEventListener('loadedmetadata', () => { audio.currentTime = at; }, { once: true });
  if (autoplay) audio.play().catch(() => {});
  updateNow();
  saveState();
}

function togglePlay() {
  if (!current()) {
    // sem fila: toca a lista que estiver no ecrã
    const k = $('#view [data-list]')?.dataset.list;
    if (k && lists[k]) playList(lists[k].tracks, 0, lists[k].ctx);
    return;
  }
  audio.paused ? audio.play().catch(() => {}) : audio.pause();
}

function next(auto = false) {
  if (!P.queue.length) return;
  if (auto && P.repeat === 'one') { audio.currentTime = 0; audio.play(); return; }
  if (P.manual > 0) P.manual--;
  if (P.idx < P.queue.length - 1) P.idx++;
  else if (P.repeat === 'all') P.idx = 0;
  else { audio.pause(); audio.currentTime = 0; return; }
  load(true);
}
function prev() {
  if (audio.currentTime > 3 || P.idx === 0) { audio.currentTime = 0; return; }
  P.idx--; load(true);
}
function toggleShuffle() {
  P.shuffle = !P.shuffle; store.set('shuffle', P.shuffle);
  const cur = current();
  if (cur) {
    if (P.shuffle) {
      P.original = P.queue.slice();
      const rest = P.queue.filter((_, i) => i !== P.idx);
      P.queue = [cur, ...shuffleArr(rest)]; P.idx = 0;
    } else if (P.original) {
      P.queue = P.original; P.original = null;
      P.idx = Math.max(0, P.queue.findIndex((t) => t.id === cur.id));
    }
    P.manual = 0;
  }
  updateModes(); renderQueue(); saveState();
}
function cycleRepeat() {
  P.repeat = { off: 'all', all: 'one', one: 'off' }[P.repeat];
  store.set('repeat', P.repeat); updateModes();
}
function addToQueue(tracks, nextUp = false) {
  if (!P.queue.length) { playList(tracks, 0); return; }
  const pos = nextUp ? P.idx + 1 : P.idx + 1 + P.manual;
  P.queue.splice(pos, 0, ...tracks);
  if (P.original) P.original.push(...tracks);
  P.manual += tracks.length;
  renderQueue(); saveState();
  toast(nextUp ? 'Vai tocar a seguir' : (tracks.length > 1 ? `${tracks.length} músicas adicionadas à fila` : 'Adicionada à fila'));
}

function saveState() {
  store.set('player', { queue: P.queue.slice(0, 1000), idx: P.idx, original: P.original?.slice(0, 1000) || null, ctx: P.ctx, time: audio.currentTime || 0 });
}
function restoreState() {
  const s = store.get('player', null);
  audio.volume = store.get('volume', 0.8);
  if (s && s.queue?.length && s.idx >= 0) {
    Object.assign(P, { queue: s.queue, idx: Math.min(s.idx, s.queue.length - 1), original: s.original, ctx: s.ctx });
    load(false, s.time || 0);
  }
  updateModes(); updateVolume();
}

function updateModes() {
  for (const id of ['#btn-shuffle', '#nf-shuffle']) $(id).classList.toggle('on', P.shuffle), $(id).classList.add('dot');
  for (const id of ['#btn-repeat', '#nf-repeat']) {
    const b = $(id); b.classList.toggle('on', P.repeat !== 'off'); b.classList.add('dot');
    b.title = { off: 'Repetir', all: 'Repetir tudo', one: 'Repetir uma' }[P.repeat];
    b.innerHTML = icon('repeat') + (P.repeat === 'one' ? '<small style="position:absolute;font-size:8px;font-weight:800;top:-4px;right:-5px">1</small>' : '');
  }
}

function updateNow() {
  const t = current();
  $('#player').classList.toggle('idle', !t);
  if (!t) return;
  const src = t.has_cover ? coverUrl(t.album_id) : '/static/icons/icon-192.png';
  $('#np-cover').src = src; $('#nf-cover').src = src;
  $('#np-title').textContent = t.title; $('#np-title').href = `#/album/${t.album_id}`;
  $('#np-artist').textContent = t.artist; $('#np-artist').href = `#/artist/${t.artist_id}`;
  $('#nf-title').textContent = t.title; $('#nf-artist').textContent = t.artist;
  $('#nf-context').textContent = P.ctx?.label ? `A tocar de ${P.ctx.label}` : 'A tocar';
  updateLikeButtons();
  document.title = `${t.title} • ${t.artist}`;
  dominantColor(src).then((c) => c && document.documentElement.style.setProperty('--np-bg', c));
  if ('mediaSession' in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: t.title, artist: t.artist, album: t.album || '',
      artwork: [{ src: new URL(src, location.href).href, sizes: '600x600', type: 'image/jpeg' }],
    });
  }
  markPlaying(); renderQueue();
}
function updateLikeButtons() {
  const t = current(); if (!t) return;
  for (const id of ['#np-like', '#nf-like']) {
    const b = $(id); b.classList.toggle('on', !!t.liked);
    b.innerHTML = icon(t.liked ? 'heart-fill' : 'heart');
  }
}
function markPlaying() {
  const t = current(), playing = !audio.paused;
  $$('.tr[data-id]').forEach((r) => {
    const on = t && +r.dataset.id === t.id;
    r.classList.toggle('playing', !!on); r.classList.toggle('is-playing', !!on && playing);
  });
  $$('.lib-item[data-ctx]').forEach((el) => el.classList.toggle('playing', el.dataset.ctx === P.ctx?.key));
  const ico = playing ? 'pause' : 'play';
  $('#btn-play').innerHTML = icon(ico); $('#nf-play').innerHTML = icon(ico); $('#mini-play').innerHTML = icon(ico);
  $$('[data-play-ctx]').forEach((b) => {
    const mine = P.ctx && b.dataset.playCtx === P.ctx.key;
    b.innerHTML = icon(mine && playing ? 'pause' : 'play');
    b.classList.toggle('on', !!(mine && playing));
  });
}

audio.addEventListener('play', markPlaying);
audio.addEventListener('pause', () => { markPlaying(); saveState(); });
audio.addEventListener('ended', () => next(true));
audio.addEventListener('error', () => {
  if (!audio.src || !current()) return;
  toast('Não foi possível tocar esta faixa — a saltar');
  setTimeout(() => next(), 800);
});
let lastSave = 0;
audio.addEventListener('timeupdate', () => {
  const d = audio.duration || current()?.duration || 0, c = audio.currentTime;
  if (!seekDrag) setSlider($('#seek'), d ? c / d : 0), setSlider($('#nf-seek'), d ? c / d : 0);
  $('#t-cur').textContent = $('#nf-cur').textContent = fmt(c);
  $('#t-dur').textContent = $('#nf-dur').textContent = fmt(d);
  $('#mini-fill').style.width = (d ? (c / d) * 100 : 0) + '%';
  if (!P.counted && c > Math.min(30, d / 2)) { P.counted = true; api(`/plays/${current().id}`, { json: {} }).catch(() => {}); }
  if (Date.now() - lastSave > 5000) { lastSave = Date.now(); saveState(); }
  if ('mediaSession' in navigator && d && navigator.mediaSession.setPositionState) {
    try { navigator.mediaSession.setPositionState({ duration: d, position: Math.min(c, d), playbackRate: 1 }); } catch { /* */ }
  }
});
if ('mediaSession' in navigator) {
  const ms = navigator.mediaSession;
  ms.setActionHandler('play', () => audio.play());
  ms.setActionHandler('pause', () => audio.pause());
  ms.setActionHandler('previoustrack', prev);
  ms.setActionHandler('nexttrack', () => next());
  try { ms.setActionHandler('seekto', (e) => { audio.currentTime = e.seekTime; }); } catch { /* */ }
}

// sliders
let seekDrag = false;
function setSlider(el, f) {
  f = Math.max(0, Math.min(1, f || 0));
  $('.fill', el).style.width = f * 100 + '%'; $('.knob', el).style.left = f * 100 + '%';
}
function makeSlider(el, { onInput, onCommit, dragFlag }) {
  const frac = (e) => { const r = el.getBoundingClientRect(); return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)); };
  el.addEventListener('pointerdown', (e) => {
    el.setPointerCapture(e.pointerId); el.classList.add('drag'); dragFlag?.(true);
    const f = frac(e); setSlider(el, f); onInput?.(f);
    const move = (ev) => { const f2 = frac(ev); setSlider(el, f2); onInput?.(f2); };
    const up = (ev) => {
      el.classList.remove('drag'); dragFlag?.(false); onCommit?.(frac(ev));
      el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up);
    };
    el.addEventListener('pointermove', move); el.addEventListener('pointerup', up);
  });
}
for (const id of ['#seek', '#nf-seek']) {
  makeSlider($(id), {
    dragFlag: (v) => (seekDrag = v),
    onInput: (f) => { $('#t-cur').textContent = $('#nf-cur').textContent = fmt(f * (audio.duration || 0)); },
    onCommit: (f) => { if (audio.duration) audio.currentTime = f * audio.duration; },
  });
}
makeSlider($('#vol'), { onInput: (f) => { audio.volume = f; audio.muted = false; updateVolume(); }, onCommit: () => store.set('volume', audio.volume) });
function updateVolume() {
  setSlider($('#vol'), audio.muted ? 0 : audio.volume);
  $('#btn-vol').innerHTML = icon(audio.muted || audio.volume === 0 ? 'mute' : 'volume');
}

// fila
function renderQueue() {
  const panel = $('#queue-panel');
  if (panel.hidden && !$('#modal .queue-sheet')) return;
  const t = current();
  const upNext = P.queue.slice(P.idx + 1, P.idx + 81);
  const item = (tr, i, cur) => `<div class="q-item ${cur ? 'cur' : ''}" data-action="queue-jump" data-qi="${i}">
      ${art(tr.album_id, tr.has_cover)}<div class="meta"><div class="t">${esc(tr.title)}</div><div class="s">${esc(tr.artist)}</div></div>
      ${cur ? '' : `<button class="icon-btn" data-action="queue-remove" data-qi="${i}" title="Remover">${icon('close')}</button>`}</div>`;
  const html = t
    ? `<h4>A tocar</h4>${item(t, P.idx, true)}
       ${upNext.length ? `<h4>${P.manual ? 'A seguir na fila' : 'A seguir'}${P.ctx?.label ? ` · ${esc(P.ctx.label)}` : ''}</h4>` : ''}
       ${upNext.map((tr, j) => item(tr, P.idx + 1 + j, false)).join('')}
       ${P.queue.length - P.idx - 1 > 80 ? `<p class="dl-hint">+ ${P.queue.length - P.idx - 81} músicas</p>` : ''}`
    : '<div class="empty"><h3>A fila está vazia</h3><p>Escolhe algo para ouvir.</p></div>';
  $('#queue-list').innerHTML = html;
  const sheet = $('#modal .queue-sheet .q-body'); if (sheet) sheet.innerHTML = html;
}

// ------------------------------------------------------------------ likes
async function setLike(track, on) {
  await api(`/likes/${track.id}`, { method: on ? 'PUT' : 'DELETE' });
  for (const l of Object.values(lists)) l.tracks.forEach((t) => { if (t.id === track.id) t.liked = on; });
  P.queue.forEach((t) => { if (t.id === track.id) t.liked = on; });
  P.original?.forEach((t) => { if (t.id === track.id) t.liked = on; });
  track.liked = on;
  $$(`.tr[data-id="${track.id}"] .like-btn`).forEach((b) => { b.classList.toggle('on', on); b.innerHTML = icon(on ? 'heart-fill' : 'heart'); });
  updateLikeButtons();
  toast(on ? 'Adicionada a Músicas de que gostas' : 'Removida de Músicas de que gostas');
  if (route.name === 'liked') render();
  loadLibrary();
}

// ------------------------------------------------------------------ componentes
function trackTable(tracks, ctx, { album = true, cover = true, numbers = 'index', removable = null, head = true, discs = false } = {}) {
  if (!tracks.length) return '';
  const key = registerList(tracks, ctx);
  const cls = album ? '' : 'no-album';
  let html = `<div class="tracks" data-list="${key}" data-ctx="${esc(ctx?.key || '')}">`;
  if (head) html += `<div class="tr head ${cls}"><div class="n">#</div><div>Título</div>${album ? '<div>Álbum</div>' : ''}<div class="dur">${icon('clock', 'i t')}</div><div></div></div>`;
  let lastDisc = null;
  const multiDisc = discs && new Set(tracks.map((t) => t.disc_no || 1)).size > 1;
  tracks.forEach((t, i) => {
    if (multiDisc && (t.disc_no || 1) !== lastDisc) { lastDisc = t.disc_no || 1; html += `<div class="disc">${icon('note')} Disco ${lastDisc}</div>`; }
    const n = numbers === 'track' ? (t.track_no || i + 1) : i + 1;
    html += `<div class="tr ${cls}" data-id="${t.id}" data-i="${i}" ${removable ? `data-entry="${t.entry_id}"` : ''}>
      <div class="n"><span>${n}</span><span class="eq"><b></b><b></b><b></b><b></b></span>${icon('play', 'i play')}</div>
      <div class="main-cell">${cover ? art(t.album_id, t.has_cover) : ''}<div style="min-width:0"><div class="tt">${esc(t.title)}</div>
        <div class="ta"><a href="#/artist/${t.artist_id}">${esc(t.artist)}</a></div></div></div>
      ${album ? `<div class="alb"><a href="#/album/${t.album_id}">${esc(t.album || '')}</a></div>` : ''}
      <div class="dur"><button class="icon-btn like-btn ${t.liked ? 'on' : ''}" data-action="like">${icon(t.liked ? 'heart-fill' : 'heart')}</button><span class="t">${fmt(t.duration)}</span></div>
      <button class="icon-btn more" data-action="track-menu">${icon('more')}</button>
    </div>`;
  });
  return html + '</div>';
}

function card({ href, artHtml, title, sub, playCtx, round = false }) {
  return `<div class="card" data-href="${href}">
    <div class="art">${artHtml}${playCtx ? `<button class="fab" data-action="play-ctx" data-play-ctx="${playCtx}" aria-label="Reproduzir">${icon('play')}</button>` : ''}</div>
    <div class="t" ${round ? 'style="text-align:left"' : ''}>${esc(title)}</div><div class="s">${sub}</div></div>`;
}
const albumCard = (a) => card({ href: `#/album/${a.id}`, artHtml: art(a.id, a.has_cover), title: a.title, sub: `${esc(a.year || '')}${a.year ? ' • ' : ''}${esc(a.artist)}`, playCtx: `album:${a.id}` });
const artistCard = (a) => card({ href: `#/artist/${a.id}`, artHtml: artistArt(a), title: a.name, sub: 'Artista', playCtx: `artist:${a.id}`, round: true });
const playlistCard = (p) => card({ href: `#/playlist/${p.id}`, artHtml: playlistArt(p), title: p.name, sub: `Playlist • ${plural(p.track_count, 'música', 'músicas')}`, playCtx: `playlist:${p.id}` });
const trackCard = (t, key, i) => `<div class="card" data-action="play-from" data-list="${key}" data-i="${i}">
    <div class="art">${art(t.album_id, t.has_cover)}<button class="fab" data-action="play-from" data-list="${key}" data-i="${i}">${icon('play')}</button></div>
    <div class="t">${esc(t.title)}</div><div class="s">${esc(t.artist)}</div></div>`;

function section(title, inner, moreHref) {
  return `<section class="section"><div class="section-head"><h2>${esc(title)}</h2>${moreHref ? `<a href="${moreHref}">Mostrar tudo</a>` : ''}</div>${inner}</section>`;
}

// resolve um contexto (album:3, playlist:2, artist:1, liked) para faixas
async function ctxTracks(key) {
  const [kind, id] = key.split(':');
  if (kind === 'album') { const a = await api(`/albums/${id}`); return { tracks: a.tracks, ctx: { key, label: a.title, href: `#/album/${id}` } }; }
  if (kind === 'playlist') { const p = await api(`/playlists/${id}`); return { tracks: p.tracks, ctx: { key, label: p.name, href: `#/playlist/${id}` } }; }
  if (kind === 'artist') { const a = await api(`/artists/${id}`); const all = await api(`/artists/${id}/tracks`); return { tracks: all, ctx: { key, label: a.name, href: `#/artist/${id}` } }; }
  if (kind === 'liked') return { tracks: await api('/liked'), ctx: { key, label: 'Músicas de que gostas', href: '#/liked' } };
  if (kind === 'all') return { tracks: await api('/tracks?limit=5000'), ctx: { key, label: 'Todas as músicas', href: '#/songs' } };
  return { tracks: [], ctx: null };
}
async function playCtx(key, shuffle = null) {
  if (P.ctx?.key === key && current() && shuffle === null) { togglePlay(); return; }
  const { tracks, ctx } = await ctxTracks(key);
  playList(tracks, 0, ctx, shuffle);
}

// ------------------------------------------------------------------ menus e modais
const menu = $('#menu');
function openMenu(x, y, html) {
  menu.innerHTML = html; menu.hidden = false;
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.min(x, innerWidth - r.width - 8) + 'px';
  menu.style.top = Math.max(8, Math.min(y, innerHeight - r.height - 8)) + 'px';
}
const closeMenu = () => { menu.hidden = true; menuCtx = null; };
let menuCtx = null;

function trackMenu(ev, tracks, opts = {}) {
  menuCtx = { tracks, ...opts };
  const t = tracks[0], single = tracks.length === 1;
  const html = `
    <button data-m="queue">${icon('queue')}Adicionar à fila</button>
    <button data-m="next">${icon('next')}Tocar a seguir</button>
    <button data-m="pl">${icon('plus')}Adicionar à playlist</button>
    ${opts.entry ? `<button data-m="remove">${icon('trash')}Remover desta playlist</button>` : ''}
    <hr>
    ${single ? `<button data-m="like">${icon(t.liked ? 'heart-fill' : 'heart')}${t.liked ? 'Remover dos gostos' : 'Guardar em Músicas de que gostas'}</button>
    <button data-m="artist">${icon('user')}Ir para o artista</button>
    <button data-m="album">${icon('note')}Ir para o álbum</button>` : ''}
    ${opts.extra || ''}`;
  const r = ev.target.closest('button,.tr,.card')?.getBoundingClientRect();
  isMobile() ? openSheet(html) : openMenu(ev.clientX || r?.left || 0, ev.clientY || r?.bottom || 0, html);
}
async function playlistPicker() {
  const pls = await api('/playlists');
  const html = `<div class="label">Adicionar à playlist</div>
    <button data-m="pl-new">${icon('plus')}Nova playlist</button><hr>
    ${pls.map((p) => `<button data-m="pl-add" data-pid="${p.id}">${icon('note')}${esc(p.name)}</button>`).join('') || '<div class="label">Ainda não tens playlists</div>'}`;
  if (isMobile()) openSheet(html); else { menu.innerHTML = html; }
}
function openSheet(html) {
  const m = $('#modal');
  m.innerHTML = `<div class="modal-card ctx-menu" style="position:static;width:100%;max-width:480px;align-self:end;margin-bottom:env(safe-area-inset-bottom)">${html}</div>`;
  m.hidden = false; m.dataset.sheet = '1';
}
const closeModal = () => { const m = $('#modal'); m.hidden = true; m.innerHTML = ''; delete m.dataset.sheet; };

async function handleMenu(action, btn) {
  const c = menuCtx; if (!c) return;
  const { tracks } = c;
  if (action === 'pl') { await playlistPicker(); return; }
  closeMenu(); if ($('#modal').dataset.sheet) closeModal();
  if (action === 'queue') addToQueue(tracks);
  if (action === 'next') addToQueue(tracks, true);
  if (action === 'like') setLike(tracks[0], !tracks[0].liked);
  if (action === 'artist') location.hash = `#/artist/${tracks[0].artist_id}`;
  if (action === 'album') location.hash = `#/album/${tracks[0].album_id}`;
  if (action === 'remove') { await api(`/playlists/${c.pid}/entries/${c.entry}`, { method: 'DELETE' }); toast('Removida da playlist'); render(); loadLibrary(); }
  if (action === 'pl-add') { await api(`/playlists/${btn.dataset.pid}/tracks`, { json: { track_ids: tracks.map((t) => t.id) } }); toast(`Adicionada a ${btn.textContent.trim()}`); loadLibrary(); }
  if (action === 'pl-new') {
    const name = await promptModal('Nova playlist', tracks.length === 1 ? tracks[0].title : 'Nova playlist');
    if (!name) return;
    const p = await api('/playlists', { json: { name } });
    await api(`/playlists/${p.id}/tracks`, { json: { track_ids: tracks.map((t) => t.id) } });
    toast(`Adicionada a ${name}`); loadLibrary();
  }
  if (action === 'pl-edit') editPlaylist(c.playlist);
  if (action === 'pl-delete') {
    if (!(await confirmModal(`Apagar "${c.playlist.name}"?`, 'A playlist é apagada da biblioteca. As músicas continuam no servidor.'))) return;
    await api(`/playlists/${c.playlist.id}`, { method: 'DELETE' }); toast('Playlist apagada'); loadLibrary(); location.hash = '#/library';
  }
}

function promptModal(title, value = '', placeholder = '') {
  return new Promise((res) => {
    const m = $('#modal');
    m.innerHTML = `<form class="modal-card"><h3>${esc(title)}</h3><input name="v" value="${esc(value)}" placeholder="${esc(placeholder)}" maxlength="120" required>
      <div class="row"><button type="button" class="ghost-btn" data-x>Cancelar</button><button class="primary-btn">Guardar</button></div></form>`;
    m.hidden = false;
    const f = $('form', m), inp = $('input', f);
    inp.focus(); inp.select();
    f.onsubmit = (e) => { e.preventDefault(); closeModal(); res(inp.value.trim()); };
    $('[data-x]', f).onclick = () => { closeModal(); res(null); };
  });
}
function confirmModal(title, text) {
  return new Promise((res) => {
    const m = $('#modal');
    m.innerHTML = `<div class="modal-card"><h3>${esc(title)}</h3><p style="color:var(--muted);margin:0 0 16px">${esc(text)}</p>
      <div class="row"><button class="ghost-btn" data-x>Cancelar</button><button class="primary-btn" data-ok>Apagar</button></div></div>`;
    m.hidden = false;
    $('[data-x]', m).onclick = () => { closeModal(); res(false); };
    $('[data-ok]', m).onclick = () => { closeModal(); res(true); };
  });
}
function editPlaylist(p) {
  const m = $('#modal');
  m.innerHTML = `<form class="modal-card"><h3>Editar detalhes</h3>
    <input name="name" value="${esc(p.name)}" maxlength="120" placeholder="Nome" required>
    <textarea name="description" rows="3" maxlength="200" placeholder="Adiciona uma descrição opcional">${esc(p.description || '')}</textarea>
    <div class="row"><button type="button" class="ghost-btn" data-x>Cancelar</button><button class="primary-btn">Guardar</button></div></form>`;
  m.hidden = false;
  const f = $('form', m); $('input', f).focus();
  $('[data-x]', f).onclick = closeModal;
  f.onsubmit = async (e) => {
    e.preventDefault();
    await api(`/playlists/${p.id}`, { method: 'PATCH', json: { name: f.name.value.trim(), description: f.description.value.trim() } });
    closeModal(); render(); loadLibrary();
  };
}
async function newPlaylist() {
  const pls = await api('/playlists');
  const p = await api('/playlists', { json: { name: `A minha playlist n.º ${pls.length + 1}` } });
  await loadLibrary(); location.hash = `#/playlist/${p.id}`;
}

// ------------------------------------------------------------------ biblioteca (sidebar)
let libFilter = store.get('libFilter', 'playlists');
async function loadLibrary() {
  $$('#lib-chips .chip').forEach((c) => c.classList.toggle('active', c.dataset.filter === libFilter));
  const el = $('#lib-list');
  try {
    let html = '';
    if (libFilter === 'playlists') {
      const [pls, liked] = await Promise.all([api('/playlists'), api('/liked')]);
      html += `<a class="lib-item" href="#/liked" data-ctx="liked">${likedArt('cover-sm')}<div class="meta"><div class="t">Músicas de que gostas</div><div class="s">Playlist • ${plural(liked.length, 'música', 'músicas')}</div></div></a>`;
      html += pls.map((p) => `<a class="lib-item" href="#/playlist/${p.id}" data-ctx="playlist:${p.id}">${playlistArt(p, 'cover-sm')}<div class="meta"><div class="t">${esc(p.name)}</div><div class="s">Playlist • ${plural(p.track_count, 'música', 'músicas')}</div></div></a>`).join('');
    } else if (libFilter === 'albums') {
      const al = await api('/albums?sort=recent');
      html = al.map((a) => `<a class="lib-item" href="#/album/${a.id}" data-ctx="album:${a.id}">${art(a.id, a.has_cover, 'cover-sm')}<div class="meta"><div class="t">${esc(a.title)}</div><div class="s">Álbum • ${esc(a.artist)}</div></div></a>`).join('');
    } else {
      const ar = await api('/artists');
      html = ar.map((a) => `<a class="lib-item" href="#/artist/${a.id}" data-ctx="artist:${a.id}">${artistArt(a, 'cover-sm')}<div class="meta"><div class="t">${esc(a.name)}</div><div class="s">Artista</div></div></a>`).join('');
    }
    el.innerHTML = html || '<p class="dl-hint" style="padding:12px">Nada por aqui ainda.</p>';
    highlightLib(); markPlaying();
  } catch { /* login */ }
}
function highlightLib() { $$('.lib-item').forEach((a) => a.classList.toggle('active', a.getAttribute('href') === location.hash)); }

// ------------------------------------------------------------------ vistas
const view = $('#view');
let route = { name: 'home' };
const routes = [
  [/^\/?$/, 'home'], [/^\/search$/, 'search'], [/^\/library$/, 'library'], [/^\/album\/(\d+)$/, 'album'],
  [/^\/artist\/(\d+)$/, 'artist'], [/^\/playlist\/(\d+)$/, 'playlist'], [/^\/liked$/, 'liked'],
  [/^\/downloads$/, 'downloads'], [/^\/songs$/, 'songs'], [/^\/albums$/, 'albums'], [/^\/artists$/, 'artists'],
];

async function render() {
  const [path, qs] = (location.hash.slice(1) || '/').split('?');
  let name = 'home', arg = null;
  for (const [re, n] of routes) { const m = path.match(re); if (m) { name = n; arg = m[1]; break; } }
  route = { name, arg, params: new URLSearchParams(qs || '') };
  $$('[data-nav]').forEach((a) => a.classList.toggle('active', a.dataset.nav === name));
  $('#search-box').hidden = name !== 'search';
  highlightLib(); closeMenu();
  view.style.removeProperty('--hero'); $('#topbar').style.removeProperty('--topbar-bg');
  clearInterval(dlTimer);
  try {
    await VIEWS[name](arg);
  } catch (e) {
    if (e.message !== 'auth') view.innerHTML = `<div class="view-inner"><div class="empty"><h3>Algo correu mal</h3><p>${esc(e.message)}</p></div></div>`;
  }
  markPlaying();
}

async function heroColor(src) {
  if (!src) return;
  const c = await dominantColor(src);
  if (c) { view.style.setProperty('--hero', c); $('#topbar').style.setProperty('--topbar-bg', `color-mix(in srgb, ${c} 45%, #121212)`); }
}

function greeting() { const h = new Date().getHours(); return h < 6 ? 'Boa noite' : h < 13 ? 'Bom dia' : h < 20 ? 'Boa tarde' : 'Boa noite'; }

const VIEWS = {
  async home() {
    const d = await api('/home');
    if (!d.stats.tracks) {
      view.innerHTML = `<div class="view-inner"><h1 class="greeting">${greeting()}</h1><div class="empty">${icon('download')}
        <h3>A tua biblioteca está vazia</h3><p>Cola um link do Spotify em Downloads, ou aponta a pasta de música existente e clica em "Atualizar biblioteca".</p>
        <a class="primary-btn" href="#/downloads">Ir para Downloads</a></div></div>`;
      return;
    }
    const sc = [`<div class="shortcut" data-href="#/liked">${likedArt()}<div class="t">Músicas de que gostas</div><button class="fab" data-action="play-ctx" data-play-ctx="liked">${icon('play')}</button></div>`];
    d.playlists.slice(0, 3).forEach((p) => sc.push(`<div class="shortcut" data-href="#/playlist/${p.id}">${playlistArt(p)}<div class="t">${esc(p.name)}</div><button class="fab" data-action="play-ctx" data-play-ctx="playlist:${p.id}">${icon('play')}</button></div>`));
    d.recent_albums.slice(0, 8 - sc.length).forEach((a) => sc.push(`<div class="shortcut" data-href="#/album/${a.id}">${art(a.id, a.has_cover)}<div class="t">${esc(a.title)}</div><button class="fab" data-action="play-ctx" data-play-ctx="album:${a.id}">${icon('play')}</button></div>`));
    let html = `<div class="view-inner"><h1 class="greeting">${greeting()}</h1>
      <p class="stats">${plural(d.stats.tracks, 'música', 'músicas')} · ${plural(d.stats.albums, 'álbum', 'álbuns')} · ${plural(d.stats.artists, 'artista', 'artistas')} · ${fmtLong(d.stats.duration)}</p>
      <div class="shortcuts">${sc.join('')}</div>`;
    if (d.recently_played.length) {
      const k = registerList(d.recently_played, { key: 'recent', label: 'Tocadas recentemente' });
      html += section('Tocadas recentemente', `<div class="grid row">${d.recently_played.map((t, i) => trackCard(t, k, i)).join('')}</div>`);
    }
    html += section('Adicionados recentemente', `<div class="grid row">${d.recent_albums.map(albumCard).join('')}</div>`, '#/albums');
    if (d.top_tracks.length >= 3) html += section('As mais ouvidas este mês', trackTable(d.top_tracks.slice(0, 5), { key: 'top', label: 'As mais ouvidas' }, { head: false }));
    html += section('Os teus artistas', `<div class="grid row">${d.artists.map(artistCard).join('')}</div>`, '#/artists');
    if (d.playlists.length) html += section('As tuas playlists', `<div class="grid row">${d.playlists.map(playlistCard).join('')}</div>`, '#/library');
    view.innerHTML = html + '</div>';
    const firstCover = d.recent_albums.find((a) => a.has_cover);
    if (firstCover) {
      const c = await dominantColor(coverUrl(firstCover.id));
      if (c) view.firstElementChild.style.background = `linear-gradient(color-mix(in srgb, ${c} 45%, #121212), #121212 340px)`;
    }
  },

  async search() {
    const q = route.params.get('q') || '';
    const inp = $('#search-input');
    if (inp.value !== q) inp.value = q;
    view.innerHTML = `<div class="view-inner" id="search-results"></div>`;
    if (!isMobile() || !q) setTimeout(() => inp.focus(), 50);
    await searchResults(q);
  },

  async library() {
    const [pls, albums, artists, liked] = await Promise.all([api('/playlists'), api('/albums?sort=recent'), api('/artists'), api('/liked')]);
    const f = route.params.get('f') || 'all';
    let items = '';
    if (f === 'all' || f === 'playlists') {
      items += playlistCardLiked(liked.length);
      items += pls.map(playlistCard).join('');
    }
    if (f === 'all' || f === 'albums') items += albums.map(albumCard).join('');
    if (f === 'all' || f === 'artists') items += artists.map(artistCard).join('');
    const chip = (k, l) => `<a class="chip ${f === k ? 'active' : ''}" href="#/library?f=${k}">${l}</a>`;
    view.innerHTML = `<div class="view-inner">
      <div style="display:flex;align-items:center;justify-content:space-between"><h1 class="page-title">A tua biblioteca</h1>
      <button class="ghost-btn" data-action="new-playlist">+ Playlist</button></div>
      <div class="filters">${chip('all', 'Tudo')}${chip('playlists', 'Playlists')}${chip('albums', 'Álbuns')}${chip('artists', 'Artistas')}<a class="chip" href="#/songs">Músicas</a></div>
      <div class="grid">${items}</div></div>`;
  },

  async album(id) {
    const a = await api(`/albums/${id}`);
    const ctx = { key: `album:${id}`, label: a.title, href: `#/album/${id}` };
    const src = a.has_cover ? coverUrl(id) : null;
    view.innerHTML = `
      <div class="hero">${art(id, a.has_cover, 'hero-cover')}<div style="min-width:0">
        <div class="kind">Álbum</div><h1 class="${a.title.length > 24 ? 'long' : ''}">${esc(a.title)}</h1>
        <div class="sub"><a href="#/artist/${a.artist_id}">${esc(a.artist)}</a>${a.year ? `<span class="dot">${esc(a.year)}</span>` : ''}
        <span class="dot">${plural(a.track_count, 'música', 'músicas')}, ${fmtLong(a.duration)}</span></div></div></div>
      <div class="action-bar">
        <button class="play-big" data-action="play-ctx" data-play-ctx="${ctx.key}">${icon('play')}</button>
        <button class="icon-btn ${P.shuffle ? 'on' : ''}" data-action="shuffle-ctx" data-key="${ctx.key}" title="Aleatório">${icon('shuffle')}</button>
        <button class="icon-btn" data-action="list-menu" title="Mais opções">${icon('more')}</button>
      </div>
      <div class="content">${trackTable(a.tracks, ctx, { album: false, cover: false, numbers: 'track', discs: true })}
        ${a.more.length ? section(`Mais de ${a.artist}`, `<div class="grid row">${a.more.map(albumCard).join('')}</div>`, `#/artist/${a.artist_id}`) : ''}
      </div>`;
    heroColor(src);
  },

  async artist(id) {
    const a = await api(`/artists/${id}`);
    const ctx = { key: `artist:${id}`, label: a.name, href: `#/artist/${id}` };
    view.innerHTML = `
      <div class="hero">${artistArt(a, 'hero-cover')}<div style="min-width:0">
        <div class="kind">Artista</div><h1 class="${a.name.length > 24 ? 'long' : ''}">${esc(a.name)}</h1>
        <div class="sub">${plural(a.track_count, 'música', 'músicas')} na biblioteca</div></div></div>
      <div class="action-bar">
        <button class="play-big" data-action="play-ctx" data-play-ctx="${ctx.key}">${icon('play')}</button>
        <button class="icon-btn ${P.shuffle ? 'on' : ''}" data-action="shuffle-ctx" data-key="${ctx.key}" title="Aleatório">${icon('shuffle')}</button>
        <button class="ghost-btn" data-action="download-more" data-q="${esc(a.name)}">Descarregar mais</button>
      </div>
      <div class="content">
        ${a.top_tracks.length ? section('Populares', trackTable(a.top_tracks, ctx, { head: false })) : ''}
        ${a.albums.length ? section('Discografia', `<div class="grid">${a.albums.map(albumCard).join('')}</div>`) : ''}
        ${a.appears_on.length ? section('Aparece em', `<div class="grid row">${a.appears_on.map(albumCard).join('')}</div>`) : ''}
      </div>`;
    heroColor(a.cover_album ? coverUrl(a.cover_album) : null);
  },

  async playlist(id) {
    const p = await api(`/playlists/${id}`);
    const ctx = { key: `playlist:${id}`, label: p.name, href: `#/playlist/${id}` };
    view.innerHTML = `
      <div class="hero">${playlistArt(p, 'hero-cover')}<div style="min-width:0">
        <div class="kind">Playlist</div><h1 class="${p.name.length > 24 ? 'long' : ''}" data-action="pl-edit" style="cursor:pointer">${esc(p.name)}</h1>
        ${p.description ? `<p class="hero-desc">${esc(p.description)}</p>` : ''}
        <div class="sub"><b>Nano</b><span class="dot">${plural(p.track_count, 'música', 'músicas')}${p.duration ? `, ${fmtLong(p.duration)}` : ''}</span></div></div></div>
      <div class="action-bar">
        ${p.tracks.length ? `<button class="play-big" data-action="play-ctx" data-play-ctx="${ctx.key}">${icon('play')}</button>
        <button class="icon-btn ${P.shuffle ? 'on' : ''}" data-action="shuffle-ctx" data-key="${ctx.key}" title="Aleatório">${icon('shuffle')}</button>` : ''}
        <button class="icon-btn" data-action="pl-menu" title="Mais opções">${icon('more')}</button>
      </div>
      <div class="content">
        ${trackTable(p.tracks, ctx, { removable: true })}
        <section class="section" style="margin-top:32px"><div class="section-head"><h2>${p.tracks.length ? 'Adicionar mais músicas' : 'Vamos encontrar algo para a tua playlist'}</h2></div>
          <div class="search-box" style="width:min(420px,100%);margin-bottom:12px">${icon('search')}<input id="pl-search" type="search" placeholder="Procurar músicas na biblioteca"></div>
          <div id="pl-results"></div></section>
      </div>`;
    view.dataset.pid = id; view._playlist = p;
    $('#pl-search').addEventListener('input', debounce(async (e) => {
      const q = e.target.value.trim();
      if (!q) { $('#pl-results').innerHTML = ''; return; }
      const r = await api(`/search?q=${encodeURIComponent(q)}`);
      $('#pl-results').innerHTML = r.tracks.slice(0, 10).map((t) => `<div class="tr no-album" data-id="${t.id}">
        <div class="n"></div><div class="main-cell">${art(t.album_id, t.has_cover)}<div style="min-width:0"><div class="tt">${esc(t.title)}</div><div class="ta">${esc(t.artist)}</div></div></div>
        <div></div><button class="ghost-btn" data-action="pl-quick-add" data-tid="${t.id}" style="grid-column:span 1">Adicionar</button></div>`).join('') || '<p class="dl-hint">Sem resultados na biblioteca.</p>';
    }, 250));
    heroColor(p.covers[0] ? coverUrl(p.covers[0]) : null);
  },

  async liked() {
    const tracks = await api('/liked');
    const ctx = { key: 'liked', label: 'Músicas de que gostas', href: '#/liked' };
    view.innerHTML = `
      <div class="hero" style="--hero:#5038a0">${likedArt('hero-cover')}<div><div class="kind">Playlist</div><h1>Músicas de que gostas</h1>
        <div class="sub"><b>Nano</b><span class="dot">${plural(tracks.length, 'música', 'músicas')}</span></div></div></div>
      <div class="action-bar" style="--hero:#5038a0">
        ${tracks.length ? `<button class="play-big" data-action="play-ctx" data-play-ctx="liked">${icon('play')}</button>
        <button class="icon-btn ${P.shuffle ? 'on' : ''}" data-action="shuffle-ctx" data-key="liked">${icon('shuffle')}</button>` : ''}</div>
      <div class="content">${tracks.length ? trackTable(tracks, ctx) : `<div class="empty">${icon('heart')}<h3>As músicas de que gostas aparecem aqui</h3><p>Guarda músicas tocando no ícone de coração.</p></div>`}</div>`;
    view.style.setProperty('--hero', '#5038a0'); $('#topbar').style.setProperty('--topbar-bg', '#2d2160');
  },

  async songs() {
    const sort = route.params.get('sort') || 'recent';
    const tracks = await api(`/tracks?limit=2000&sort=${sort}`);
    const chip = (k, l) => `<a class="chip ${sort === k ? 'active' : ''}" href="#/songs?sort=${k}">${l}</a>`;
    view.innerHTML = `<div class="view-inner"><h1 class="page-title">Todas as músicas</h1>
      <div class="filters">${chip('recent', 'Adicionadas recentemente')}${chip('title', 'Título')}${chip('artist', 'Artista')}
      <button class="primary-btn" style="margin-left:auto;padding:6px 18px;font-size:14px" data-action="play-ctx" data-play-ctx="all">Reproduzir tudo</button></div>
      ${trackTable(tracks, { key: 'all', label: 'Todas as músicas', href: '#/songs' })}</div>`;
  },

  async albums() {
    const sort = route.params.get('sort') || 'recent';
    const al = await api(`/albums?sort=${sort}`);
    const chip = (k, l) => `<a class="chip ${sort === k ? 'active' : ''}" href="#/albums?sort=${k}">${l}</a>`;
    view.innerHTML = `<div class="view-inner"><h1 class="page-title">Álbuns</h1>
      <div class="filters">${chip('recent', 'Recentes')}${chip('title', 'A–Z')}${chip('artist', 'Artista')}${chip('year', 'Ano')}</div>
      <div class="grid">${al.map(albumCard).join('')}</div></div>`;
  },

  async artists() {
    const ar = await api('/artists');
    view.innerHTML = `<div class="view-inner"><h1 class="page-title">Artistas</h1><div class="grid">${ar.map(artistCard).join('')}</div></div>`;
  },

  async downloads() {
    view.innerHTML = `<div class="view-inner">
      <h1 class="page-title">Downloads</h1>
      <div id="dl-warn"></div>
      <form class="dl-box" id="dl-form">
        <h2>Adicionar à biblioteca</h2>
        <p>Cola links do Spotify (faixa, álbum, playlist ou artista) ou escreve "artista - música". Um por linha.</p>
        <textarea id="dl-input" placeholder="https://open.spotify.com/album/…&#10;https://open.spotify.com/playlist/…&#10;Rosalía - Despechá"></textarea>
        <div class="dl-actions"><button class="primary-btn" type="submit">${'Descarregar'}</button>
        <span class="dl-hint">Os downloads correm um de cada vez no servidor. Podes fechar esta página.</span></div>
      </form>
      <div class="section-head"><h2>Atividade</h2><a href="javascript:void 0" data-action="dl-clear">Limpar concluídos</a></div>
      <div id="jobs"></div></div>`;
    const pre = route.params.get('q');
    if (pre) $('#dl-input').value = pre;
    $('#dl-form').onsubmit = async (e) => {
      e.preventDefault();
      const query = $('#dl-input').value.trim();
      if (!query) return;
      const r = await api('/downloads', { json: { query } });
      $('#dl-input').value = '';
      toast(r.ids.length > 1 ? `${r.ids.length} downloads na fila` : 'Download na fila');
      refreshJobs();
    };
    await refreshJobs();
    dlTimer = setInterval(refreshJobs, 2000);
  },
};
function playlistCardLiked(n) {
  return card({ href: '#/liked', artHtml: likedArt(), title: 'Músicas de que gostas', sub: `Playlist • ${plural(n, 'música', 'músicas')}`, playCtx: 'liked' });
}

// pesquisa
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
async function searchResults(q) {
  const box = $('#search-results'); if (!box) return;
  if (!q) {
    const tiles = [['#/songs', 'Músicas', '#e13300'], ['#/albums', 'Álbuns', '#1e3264'], ['#/artists', 'Artistas', '#8400e7'],
      ['#/liked', 'Gostos', '#5038a0'], ['#/library?f=playlists', 'Playlists', '#148a08'], ['#/downloads', 'Downloads', '#e8115b']];
    box.innerHTML = `<h1 class="page-title">Navegar</h1><div class="browse">${tiles.map(([h, l, c]) => `<a href="${h}" style="background:${c}">${l}</a>`).join('')}</div>`;
    return;
  }
  const r = await api(`/search?q=${encodeURIComponent(q)}`);
  if ($('#search-input').value.trim() !== q) return; // resposta antiga
  const nothing = !r.tracks.length && !r.albums.length && !r.artists.length && !r.playlists.length;
  const dlBtn = `<div class="dl-box" style="margin-top:8px"><h2>Não está na biblioteca?</h2><p>Procura "${esc(q)}" no Spotify/YouTube e descarrega para o servidor.</p>
     <button class="primary-btn" data-action="download-more" data-q="${esc(q)}">Descarregar "${esc(q.length > 40 ? q.slice(0, 40) + '…' : q)}"</button></div>`;
  if (/^https?:\/\/(open\.)?spotify\.com\//.test(q) || /^spotify:/.test(q)) {
    box.innerHTML = `<div class="dl-box"><h2>Link do Spotify detetado</h2><p>Queres descarregar isto para a tua biblioteca?</p>
      <button class="primary-btn" data-action="download-more" data-q="${esc(q)}">Descarregar</button></div>`;
    return;
  }
  if (nothing) { box.innerHTML = `<div class="empty"><h3>Nenhum resultado para "${esc(q)}"</h3><p>Verifica a ortografia ou descarrega-a.</p></div>${dlBtn}`; return; }
  const exactArtist = r.artists.find((a) => a.name.toLowerCase() === q.toLowerCase());
  let top = '';
  if (exactArtist || (r.artists.length && !r.tracks.length)) {
    const a = exactArtist || r.artists[0];
    top = `<div class="top-result" data-href="#/artist/${a.id}">${artistArt(a)}<div class="t">${esc(a.name)}</div><div class="s"><span class="tag" style="margin:0">Artista</span></div>
      <button class="fab" data-action="play-ctx" data-play-ctx="artist:${a.id}">${icon('play')}</button></div>`;
  } else if (r.tracks.length) {
    const t = r.tracks[0], k = registerList(r.tracks, { key: 'search', label: `Pesquisa "${q}"` });
    top = `<div class="top-result" data-action="play-from" data-list="${k}" data-i="0">${art(t.album_id, t.has_cover)}<div class="t">${esc(t.title)}</div>
      <div class="s"><span>${esc(t.artist)}</span><span class="tag">Música</span></div><button class="fab" data-action="play-from" data-list="${k}" data-i="0">${icon('play')}</button></div>`;
  } else if (r.albums.length) {
    const a = r.albums[0];
    top = `<div class="top-result" data-href="#/album/${a.id}">${art(a.id, a.has_cover)}<div class="t">${esc(a.title)}</div><div class="s">${esc(a.artist)}<span class="tag">Álbum</span></div>
      <button class="fab" data-action="play-ctx" data-play-ctx="album:${a.id}">${icon('play')}</button></div>`;
  }
  box.innerHTML = `
    <div class="search-top"><section><div class="section-head"><h2>Melhor resultado</h2></div>${top}</section>
      ${r.tracks.length ? `<section><div class="section-head"><h2>Músicas</h2></div>${trackTable(r.tracks.slice(0, 4), { key: 'search', label: `Pesquisa "${q}"` }, { head: false, album: false })}</section>` : '<div></div>'}</div>
    ${r.tracks.length > 4 ? section('Mais músicas', trackTable(r.tracks.slice(4), { key: 'search2', label: `Pesquisa "${q}"` }, { head: false })) : ''}
    ${r.artists.length ? section('Artistas', `<div class="grid row">${r.artists.map(artistCard).join('')}</div>`) : ''}
    ${r.albums.length ? section('Álbuns', `<div class="grid row">${r.albums.map(albumCard).join('')}</div>`) : ''}
    ${r.playlists.length ? section('Playlists', `<div class="grid row">${r.playlists.map(playlistCard).join('')}</div>`) : ''}
    ${dlBtn}`;
  markPlaying();
}
$('#search-input').addEventListener('input', debounce((e) => {
  const q = e.target.value.trim();
  history.replaceState(null, '', q ? `#/search?q=${encodeURIComponent(q)}` : '#/search');
  route.params = new URLSearchParams(q ? `q=${q}` : '');
  searchResults(q);
}, 220));

// downloads
let dlTimer = null, knownJobs = {};
const ST = { queued: 'Na fila', running: 'A descarregar', done: 'Concluído', partial: 'Parcial', failed: 'Falhou', cancelled: 'Cancelado' };
async function refreshJobs() {
  let d;
  try { d = await api('/downloads'); } catch { return; }
  checkJobChanges(d.jobs);
  const box = $('#jobs'); if (!box) return;
  $('#dl-warn').innerHTML = d.spotdl ? '' : '<div class="warn">O spotDL não foi encontrado no servidor. Usa a imagem Docker incluída (já traz spotDL + ffmpeg).</div>';
  if (!d.jobs.length) { box.innerHTML = '<p class="dl-hint">Sem downloads ainda.</p>'; return; }
  box.innerHTML = d.jobs.map((j) => {
    const pct = j.total ? Math.round(((j.done + j.failed) / j.total) * 100) : 0;
    const active = j.status === 'running' || j.status === 'queued';
    const label = j.query.replace(/^https?:\/\/open\.spotify\.com\/(intl-\w+\/)?/, 'spotify/').replace(/\?.*$/, '');
    return `<div class="job"><div class="job-top"><div class="job-q" title="${esc(j.query)}">${esc(label)}</div>
      <span class="job-st ${j.status}">${ST[j.status] || j.status}</span>
      <button class="icon-btn" data-action="dl-cancel" data-id="${j.id}" title="${active ? 'Cancelar' : 'Remover'}">${icon(active ? 'close' : 'trash')}</button></div>
      ${j.status === 'running' || j.status === 'queued' ? `<div class="job-bar ${!j.total ? 'indet' : ''}"><div style="width:${pct}%"></div></div>` : ''}
      <div class="job-meta"><span>${j.total ? `${j.done}/${j.total} faixas` : (j.status === 'queued' ? 'à espera' : j.done ? `${j.done} faixas` : '')}${j.failed ? ` · ${j.failed} com erro` : ''}</span><span>${ago(j.finished_at || j.created_at)}</span></div>
      ${j.log_tail ? `<details><summary>Registo</summary><pre>${esc(j.log_tail)}</pre></details>` : ''}</div>`;
  }).join('');
  // mantém os <details> abertos entre atualizações
  box.querySelectorAll('details').forEach((det, i) => { if (openDetails.has(d.jobs[i]?.id)) det.open = true; det.ontoggle = () => (det.open ? openDetails.add(d.jobs[i].id) : openDetails.delete(d.jobs[i].id)); });
}
const openDetails = new Set();
function checkJobChanges(jobs) {
  let active = 0;
  for (const j of jobs) {
    const prevSt = knownJobs[j.id];
    if (prevSt && (prevSt === 'running' || prevSt === 'queued') && ['done', 'partial'].includes(j.status)) {
      toast(`Download concluído: ${j.done} ${j.done === 1 ? 'faixa' : 'faixas'}`);
      setTimeout(() => { loadLibrary(); if (['home', 'library', 'albums', 'songs'].includes(route.name)) render(); }, 2500);
    }
    knownJobs[j.id] = j.status;
    if (j.status === 'running' || j.status === 'queued') active++;
  }
  const b = $('#dl-badge'); b.hidden = !active; b.textContent = active;
  return active;
}
setInterval(async () => {
  if (route.name === 'downloads' || !Object.values(knownJobs).some((s) => s === 'running' || s === 'queued')) return;
  try { checkJobChanges((await api('/downloads')).jobs); } catch { /* */ }
}, 5000);

// ------------------------------------------------------------------ eventos
document.addEventListener('click', async (e) => {
  const mItem = e.target.closest('#menu [data-m], #modal [data-m]');
  if (mItem) { e.preventDefault(); handleMenu(mItem.dataset.m, mItem); return; }
  if (!menu.hidden && !e.target.closest('#menu')) closeMenu();
  if (e.target.id === 'modal') { closeModal(); return; }

  const a = e.target.closest('[data-action]');
  const row = e.target.closest('.tr[data-id]');
  const listEl = e.target.closest('[data-list]');
  const getTrack = () => { const l = lists[listEl?.dataset.list]; return l?.tracks[+row.dataset.i]; };

  if (a) {
    const act = a.dataset.action;
    e.preventDefault(); e.stopPropagation();
    switch (act) {
      case 'toggle': togglePlay(); break;
      case 'next': next(); break;
      case 'prev': prev(); break;
      case 'shuffle': toggleShuffle(); $$('[data-action="shuffle-ctx"]').forEach((b) => b.classList.toggle('on', P.shuffle)); break;
      case 'repeat': cycleRepeat(); break;
      case 'mute': audio.muted = !audio.muted; updateVolume(); break;
      case 'like-current': if (current()) setLike(current(), !current().liked); break;
      case 'toggle-queue': {
        if (isMobile()) { openQueueSheet(); break; }
        const qp = $('#queue-panel'); qp.hidden = !qp.hidden; $('#btn-queue').classList.toggle('on', !qp.hidden); renderQueue(); break;
      }
      case 'nf-queue': openQueueSheet(); break;
      case 'queue-jump': { const i = +a.dataset.qi; if (i !== P.idx) { if (i > P.idx) P.manual = Math.max(0, P.manual - (i - P.idx)); P.idx = i; load(true); } break; }
      case 'queue-remove': { const i = +a.dataset.qi; P.queue.splice(i, 1); if (i <= P.idx + P.manual && P.manual) P.manual--; renderQueue(); saveState(); break; }
      case 'open-now': if (isMobile() && current()) $('#now-full').hidden = false; break;
      case 'close-now': $('#now-full').hidden = true; break;
      case 'nf-menu': if (current()) trackMenu(e, [current()]); break;
      case 'like': { const t = getTrack(); if (t) setLike(t, !t.liked); break; }
      case 'track-menu': {
        const t = getTrack(); if (!t) break;
        const opts = row.dataset.entry ? { entry: row.dataset.entry, pid: view.dataset.pid } : {};
        trackMenu(e, [t], opts); break;
      }
      case 'play-from': { const l = lists[a.dataset.list]; if (l) playList(l.tracks, +a.dataset.i, l.ctx); break; }
      case 'play-ctx': playCtx(a.dataset.playCtx).catch((err) => toast(err.message)); break;
      case 'shuffle-ctx': playCtx(a.dataset.key, true).then(() => $$('[data-action="shuffle-ctx"]').forEach((b) => b.classList.add('on'))); break;
      case 'list-menu': {
        const k = $('#view [data-list]')?.dataset.list; if (!k) break;
        trackMenu(e, lists[k].tracks); break;
      }
      case 'pl-menu': {
        const p = view._playlist;
        menuCtx = { tracks: p.tracks, playlist: p };
        const html = `<button data-m="pl-edit">${icon('edit')}Editar detalhes</button>
          ${p.tracks.length ? `<button data-m="queue">${icon('queue')}Adicionar à fila</button>` : ''}
          <hr><button data-m="pl-delete">${icon('trash')}Apagar playlist</button>`;
        isMobile() ? openSheet(html) : openMenu(e.clientX, e.clientY, html);
        menuCtx = { tracks: p.tracks, playlist: p };
        break;
      }
      case 'pl-edit': editPlaylist(view._playlist); break;
      case 'pl-quick-add': {
        await api(`/playlists/${view.dataset.pid}/tracks`, { json: { track_ids: [+a.dataset.tid] } });
        toast('Adicionada à playlist'); const y = view.scrollTop; await render(); view.scrollTop = y; loadLibrary(); break;
      }
      case 'new-playlist': newPlaylist(); break;
      case 'rescan': await api('/scan', { json: {} }); toast('A atualizar a biblioteca…'); pollScan(); break;
      case 'download-more': location.hash = `#/downloads?q=${encodeURIComponent(a.dataset.q)}`; break;
      case 'dl-cancel': await api(`/downloads/${a.dataset.id}`, { method: 'DELETE' }); refreshJobs(); break;
      case 'dl-clear': await api('/downloads/clear', { json: {} }); refreshJobs(); break;
    }
    return;
  }
  if (row && !e.target.closest('a')) {
    if (isMobile()) { const l = lists[listEl?.dataset.list]; if (l) playList(l.tracks, +row.dataset.i, l.ctx); }
    else { $$('.tr.sel').forEach((r) => r.classList.remove('sel')); row.classList.add('sel'); }
    return;
  }
  const href = e.target.closest('[data-href]');
  if (href && !e.target.closest('a')) location.hash = href.dataset.href;
});
document.addEventListener('dblclick', (e) => {
  const row = e.target.closest('.tr[data-id]'), listEl = e.target.closest('[data-list]');
  if (!row || !listEl || e.target.closest('button,a')) return;
  const l = lists[listEl.dataset.list]; if (l) playList(l.tracks, +row.dataset.i, l.ctx);
});
document.addEventListener('contextmenu', (e) => {
  const row = e.target.closest('.tr[data-id]'), listEl = e.target.closest('[data-list]');
  if (!row || !listEl) return;
  const t = lists[listEl.dataset.list]?.tracks[+row.dataset.i]; if (!t) return;
  e.preventDefault();
  trackMenu(e, [t], row.dataset.entry ? { entry: row.dataset.entry, pid: view.dataset.pid } : {});
});
document.addEventListener('keydown', (e) => {
  if (e.target.matches('input, textarea')) return;
  if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
  if (e.key === 'Escape') { closeMenu(); closeModal(); $('#now-full').hidden = true; }
  if (e.altKey && e.key === 'ArrowRight') next();
  if (e.altKey && e.key === 'ArrowLeft') prev();
});
$('#lib-chips').addEventListener('click', (e) => {
  const c = e.target.closest('.chip'); if (!c) return;
  libFilter = c.dataset.filter; store.set('libFilter', libFilter); loadLibrary();
});
view.addEventListener('scroll', () => $('#topbar').classList.toggle('solid', view.scrollTop > 64), { passive: true });

function openQueueSheet() {
  const m = $('#modal');
  m.innerHTML = `<div class="modal-card queue-sheet" style="max-height:80vh;overflow:auto;align-self:end"><div style="display:flex;justify-content:space-between;align-items:center"><h3 style="margin:0">Fila</h3>
    <button class="icon-btn" onclick="document.getElementById('modal').hidden=true">${icon('close')}</button></div><div class="q-body"></div></div>`;
  m.hidden = false; renderQueue();
}
async function pollScan() {
  for (let i = 0; i < 600; i++) {
    await new Promise((r) => setTimeout(r, 1500));
    const s = await api('/scan');
    if (!s.running) { toast(`Biblioteca atualizada: +${s.added} / −${s.removed}`); loadLibrary(); render(); return; }
  }
}

// ------------------------------------------------------------------ login + arranque
function showLogin() { $('#login').hidden = false; $('#login-pass').focus(); }
$('#login-form').onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api('/login', { json: { password: $('#login-pass').value } });
    $('#login').hidden = true; boot();
  } catch (err) { $('#login-err').textContent = err.message; }
};

async function boot() {
  const me = await fetch('/api/me').then((r) => r.json());
  if (me.auth_enabled && !me.authed) { showLogin(); return; }
  restoreState();
  loadLibrary();
  render();
  try { checkJobChanges((await api('/downloads')).jobs); } catch { /* */ }
}
window.addEventListener('hashchange', () => { render(); view.scrollTop = 0; $('#now-full').hidden = true; });
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
$('#player').classList.add('idle');
boot();
