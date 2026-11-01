import { clamp, chapterAt, formatTime, formatDuration, formatRate, formatPercent, formatBytes, plural, round2,
  illustrationAt, bookProgress, skipLabel, normalizeBook, RATE_MIN, RATE_MAX, BOOST_MIN, BOOST_MAX } from './model.mjs';
import * as store from './store.mjs';
import { Booster, isIOS } from './booster.mjs';
import { offlineSupported, downloadStatus, downloadBook, deleteDownload, migrateLegacy, rememberBook, storageEstimate } from './offline.mjs';

const $ = id => document.getElementById(id);
const audio = $('audio');
const booster = new Booster(audio);
const html = document.documentElement;
const SPEEDS = [0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3];
const BOOSTS = [1, 1.3, 1.5, 1.75, 2];
const TIMERS = [[15, '15 мин'], [30, '30 мин'], [45, '45 мин'], [60, '1 час'], [90, '1,5 часа'], ['chapter', 'До конца главы'], [0, 'Выключить']];

const state = {
  settings: store.loadSettings(), library: null, entries: [], book: null, raw: null, bookmarks: [],
  ready: false, chapter: -1, seeking: false, savedAt: 0, pendingRestore: null, resumeAfterLoad: false,
  timer: null, downloader: null, swReady: false, installEvent: null, wakeLock: null, shownImage: undefined, manualImage: false,
  lastJump: null, toastTimer: 0
};

// ---------- общие мелочи ----------
function toast(text, action, onAction, ms = 5000) {
  $('toast-text').textContent = text;
  const button = $('toast-action');
  button.hidden = !action; button.textContent = action || '';
  button.onclick = () => { hideToast(); if (onAction) onAction(); };
  $('toast').hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(hideToast, ms);
}
function hideToast() { $('toast').hidden = true; }
function status(text) { $('status-line').textContent = text || ''; }
function openSheet(id) { const d = $(id); if (!d.open) d.showModal(); }
function closeSheets() { document.querySelectorAll('dialog[open]').forEach(d => d.close()); }
function chips(container, items, current, onPick) {
  container.textContent = '';
  for (const [value, label] of items) {
    const b = document.createElement('button');
    b.className = 'chip'; b.textContent = label; b.type = 'button';
    b.setAttribute('aria-pressed', String(value === current));
    b.addEventListener('click', () => onPick(value));
    container.append(b);
  }
}
function coverElement(entry, className) {
  if (entry.coverUrl) {
    const img = document.createElement('img');
    img.className = className; img.alt = ''; img.loading = 'lazy'; img.decoding = 'async'; img.src = entry.coverUrl;
    return img;
  }
  const ph = document.createElement('div');
  ph.className = `${className} cover-ph`;
  ph.textContent = entry.title;
  return ph;
}

// ---------- настройки ----------
function applySettings() {
  const s = state.settings;
  const theme = s.theme === 'system' ? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark') : s.theme;
  html.dataset.theme = theme;
  html.style.setProperty('--scale', String(s.scale));
  document.querySelector('meta[name=theme-color]').content = theme === 'light' ? '#ffffff' : '#000000';
  document.body.classList.toggle('car', s.car);
  $('car-toggle').setAttribute('aria-pressed', String(s.car));
  document.querySelectorAll('.skip-n').forEach(n => { n.textContent = s.skip >= 60 ? `${s.skip / 60}м` : String(s.skip); });
  $('back').setAttribute('aria-label', `Назад на ${skipLabel(s.skip)}`);
  $('forward').setAttribute('aria-label', `Вперёд на ${skipLabel(s.skip)}`);
  booster.boost = s.boost; booster.setLeveling(s.leveling);
  $('volume-value').textContent = formatPercent(s.boost);
  updateWakeLock();
}
function setSetting(key, value) {
  state.settings[key] = value;
  store.saveSettings(state.settings);
  applySettings();
}

// ---------- библиотека ----------
async function fetchJSON(url) {
  const response = await fetch(url, { cache: 'no-cache' });
  if (!response.ok) throw Object.assign(new Error(`HTTP ${response.status}`), { status: response.status });
  return response.json();
}
async function loadLibrary() {
  const libraryUrl = new URL('library.json', document.baseURI);
  try {
    const lib = await fetchJSON(libraryUrl);
    state.library = lib;
    state.entries = (lib.books || []).map(b => ({ ...b, url: new URL(b.path, libraryUrl).href, coverUrl: b.cover ? new URL(b.cover, libraryUrl).href : '' }));
  } catch (error) {
    // Старое размещение одной книги: book.json рядом с index.html.
    try {
      const url = new URL('book.json', document.baseURI);
      const raw = await fetchJSON(url);
      const cover = raw.images && raw.images.cover ? new URL(raw.images.cover.src, url).href : '';
      state.library = { title: 'Между строк', books: [] };
      state.entries = [{ id: raw.id, title: raw.title, author: raw.author, duration: raw.duration, chapters: raw.chapters.length, size: raw.audio.size, url: url.href, coverUrl: cover }];
    } catch { state.entries = []; throw error; }
  }
  $('library-name').textContent = state.library.title || 'Между строк';
}

function renderLibrary() {
  const list = $('books');
  list.textContent = '';
  const query = $('library-search').value.toLocaleLowerCase('ru').trim();
  const entries = state.entries.filter(e => !query || `${e.title} ${e.author}`.toLocaleLowerCase('ru').includes(query));
  $('library-search').hidden = state.entries.length < 6;
  $('library-count').textContent = state.entries.length ? `${state.entries.length} ${plural(state.entries.length, 'книга', 'книги', 'книг')}` : '';
  $('library-empty').hidden = state.entries.length > 0;
  for (const entry of entries) {
    const progress = store.rawProgress(entry.id);
    const share = bookProgress(entry, progress);
    const li = document.createElement('li');
    const button = document.createElement('button');
    button.className = 'book-card';
    button.append(coverElement(entry, 'book-cover'));
    const info = document.createElement('span'); info.className = 'book-info';
    const title = document.createElement('b'); title.textContent = entry.title;
    const author = document.createElement('span'); author.className = 'meta'; author.textContent = entry.author || '';
    const meta = document.createElement('span'); meta.className = 'meta';
    meta.textContent = share > 0.995 ? 'Прослушана' : share > 0 ? `${Math.floor(share * 100)}% · осталось ${formatDuration(entry.duration * (1 - share))}` : formatDuration(entry.duration);
    const bar = document.createElement('span'); bar.className = 'progress-line';
    const fill = document.createElement('span'); fill.style.width = `${share * 100}%`; bar.append(fill);
    const badge = document.createElement('span'); badge.className = 'badge'; badge.hidden = true; badge.textContent = 'Скачана';
    info.append(title, author, meta, bar, badge);
    button.append(info);
    button.addEventListener('click', () => openBook(entry.id, { push: true }));
    li.append(button); list.append(li);
    if (state.swReady) cachedStatus(entry).then(s => { if (s && s.complete) badge.hidden = false; }).catch(() => {});
  }
  renderContinue();
}
async function cachedStatus(entry) {
  try { const raw = await fetchJSON(entry.url); return downloadStatus(normalizeBook(raw, entry.url)); } catch { return null; }
}
function renderContinue() {
  const id = store.lastBook();
  const entry = state.entries.find(e => e.id === id);
  const progress = entry && store.rawProgress(entry.id);
  $('continue').hidden = !entry;
  if (!entry) return;
  const cover = $('continue-cover');
  if (entry.coverUrl) { cover.src = entry.coverUrl; cover.hidden = false; } else cover.hidden = true;
  $('continue-title').textContent = entry.title;
  $('continue-meta').textContent = progress ? `${progress.chapterTitle || ''} · ${Math.floor(bookProgress(entry, progress) * 100)}% книги` : entry.author || '';
  $('continue-progress').style.width = `${bookProgress(entry, progress) * 100}%`;
}

function showLibrary(push) {
  if (push) history.pushState({ view: 'library' }, '', location.pathname + location.search);
  $('player-view').hidden = true; $('library-view').hidden = false;
  document.title = `${state.library?.title || 'Между строк'} · Аудиокниги`;
  renderLibrary();
}

// ---------- книга ----------
async function openBook(id, { push = false, autoplay = false } = {}) {
  const entry = state.entries.find(e => e.id === id);
  if (!entry) { showLibrary(false); toast('Книга не найдена в библиотеке'); return; }
  if (push) { history.pushState({ view: 'book', id }, '', `#book=${encodeURIComponent(id)}`); state.navPushed = true; }
  $('library-view').hidden = true; $('player-view').hidden = false;
  if (state.book && state.book.id === id) { render(); return; }
  if (state.book) { saveNow(); audio.pause(); }
  state.ready = false; state.chapter = -1; state.shownImage = undefined; state.manualImage = false; state.lastJump = null;
  setControls(false);
  status('Открываю книгу…');
  try {
    const raw = await fetchJSON(entry.url);
    const book = normalizeBook(raw, entry.url);
    state.book = book; state.raw = raw;
    state.bookmarks = store.loadBookmarks(book.id);
    store.setLastBook(book.id);
    document.title = `${book.title} · Между строк`;
    $('book-title').textContent = book.title; $('book-author').textContent = book.author || '';
    $('more-file').href = book.audioUrl; $('more-file').download = `${book.title}${book.audioUrl.match(/\.(m4b|m4a|mp3)/)?.[0] || '.m4b'}`;
    $('more-images').hidden = Object.keys(book.images).length < 2;
    buildChapters(); renderBookmarks();
    state.pendingRestore = store.loadProgress(book);
    state.resumeAfterLoad = autoplay || state.settings.autoplay;
    if (state.pendingRestore?.migrated) toast('Книга обновлена: продолжаем с того же места главы');
    audio.src = book.audioUrl;
    audio.load();
    render();
    if (state.swReady) {
      migrateLegacy(book).catch(() => {}).then(() => refreshDownload());
      rememberBook(book, raw).catch(() => {});
    } else refreshDownload();
  } catch {
    status('');
    toast('Не удалось открыть книгу. Проверьте соединение.');
  }
}

function setControls(enabled) {
  for (const id of ['play', 'back', 'forward', 'seek', 'prev-chapter', 'next-chapter']) $(id).disabled = !enabled;
}

function onMetadata() {
  const book = state.book;
  state.ready = true;
  const restore = state.pendingRestore; state.pendingRestore = null;
  audio.defaultPlaybackRate = audio.playbackRate = restore ? restore.rate : (audio.playbackRate || 1);
  audio.preservesPitch = true; audio.webkitPreservesPitch = true;
  if (restore) audio.currentTime = clamp(restore.position, 0, audio.duration || book.duration);
  setControls(true);
  status(restore ? `Продолжаем: ${formatTime(restore.position - book.chapters[chapterAt(book.chapters, restore.position)].start)} в главе` : '');
  render(); renderSpeed();
  if (state.resumeAfterLoad) { state.resumeAfterLoad = false; play(); }
}

function render() {
  const book = state.book;
  if (!book) return;
  const position = audio.currentTime || 0;
  const index = clamp(chapterAt(book.chapters, position), 0, book.chapters.length - 1);
  const chapter = book.chapters[index];
  if (index !== state.chapter) {
    state.chapter = index;
    $('chapter-title').textContent = chapter.title;
    $('chapter-counter').textContent = `Глава ${index + 1} из ${book.chapters.length}`;
    $('seek').max = String(Math.max(1, Math.floor(chapter.end - chapter.start)));
    $('prev-chapter').disabled = !state.ready;
    $('next-chapter').disabled = !state.ready || index >= book.chapters.length - 1;
    for (const row of $('chapters').children) row.firstChild.setAttribute('aria-current', String(Number(row.dataset.index) === index));
    updateMediaMetadata();
  }
  if (!state.seeking) {
    $('seek').value = String(Math.floor(position - chapter.start));
    $('seek').style.setProperty('--p', `${(position - chapter.start) / Math.max(1, chapter.end - chapter.start) * 100}%`);
    $('elapsed').textContent = formatTime(position - chapter.start);
    $('remaining').textContent = `−${formatTime(chapter.end - position)}`;
    $('seek').setAttribute('aria-valuetext', `${formatTime(position - chapter.start)} из ${formatTime(chapter.end - chapter.start)}`);
  }
  const rate = audio.playbackRate || 1;
  $('book-left').textContent = `${Math.floor(position / book.duration * 100)}% · ещё ${formatDuration((book.duration - position) / rate)}`;
  if (state.settings.autoImage && !state.manualImage) showImage(illustrationAt(book, position));
  else if (state.shownImage === undefined) showImage(book.images.cover ? 'cover' : Object.keys(book.images)[0] || null);
  updatePositionState();
}

function showImage(key) {
  if (key === state.shownImage) return;
  state.shownImage = key;
  const image = key && state.book.images[key];
  const art = $('art');
  if (image) { art.src = image.url; art.alt = image.alt || image.title || ''; art.hidden = false; }
  else { art.removeAttribute('src'); art.hidden = true; }
  $('art-button').classList.toggle('no-image', !image);
  $('art-button').dataset.title = state.book.title;
  $('art-caption').hidden = !image || key === 'cover';
  $('art-caption').textContent = image && key !== 'cover' ? image.title : '';
}

function updateMediaMetadata() {
  if (!('mediaSession' in navigator) || !window.MediaMetadata || !state.book) return;
  const book = state.book, chapter = book.chapters[state.chapter];
  const artwork = book.coverUrl ? [{ src: book.coverUrl, sizes: '512x512' }] : [{ src: new URL('icons/icon-512.png', document.baseURI).href, sizes: '512x512', type: 'image/png' }];
  try { navigator.mediaSession.metadata = new MediaMetadata({ title: chapter.title, artist: book.author || '', album: book.title, artwork }); } catch {}
}
function updatePositionState() {
  if (!state.ready || !navigator.mediaSession?.setPositionState || !Number.isFinite(audio.duration)) return;
  try { navigator.mediaSession.setPositionState({ duration: audio.duration, playbackRate: audio.playbackRate, position: clamp(audio.currentTime, 0, audio.duration) }); } catch {}
}

// ---------- сохранение ----------
function save(force) {
  if (!state.ready || !state.book) return;
  if (!force && Date.now() - state.savedAt < 3000) return;
  state.savedAt = Date.now();
  if (!store.saveProgress(state.book, audio.currentTime, audio.playbackRate)) status('Браузер не даёт сохранить место');
}
const saveNow = () => save(true);

// ---------- управление ----------
async function play() {
  if (!state.ready) return;
  if (audio.ended) jump(0);
  booster.unlock();
  await booster.resume();
  try { await audio.play(); }
  catch (error) { if (error.name === 'NotAllowedError') status('Нажмите ▶, чтобы продолжить'); else toast('Не удалось начать воспроизведение. Проверьте соединение или скачайте книгу.'); }
}
function togglePlay() { if (audio.paused) play(); else audio.pause(); }

function jump(position, { remember = false, label = '' } = {}) {
  if (!state.ready) return;
  const from = audio.currentTime;
  const to = clamp(position, 0, Math.max(0, (Number.isFinite(audio.duration) ? audio.duration : state.book.duration) - 0.2));
  audio.currentTime = to;
  if (remember && Math.abs(to - from) > 60) {
    state.lastJump = from;
    toast(label || `Переход: ${state.book.chapters[chapterAt(state.book.chapters, to)].title}`, 'Вернуться', () => jump(from), 8000);
  }
  render(); save(true);
}
const skip = sign => jump(audio.currentTime + sign * state.settings.skip);
function prevChapter() {
  const book = state.book, i = state.chapter, pos = audio.currentTime;
  jump(pos - book.chapters[i].start > 4 || i === 0 ? book.chapters[i].start : book.chapters[i - 1].start, { remember: true });
}
function nextChapter() {
  const book = state.book;
  if (state.chapter < book.chapters.length - 1) jump(book.chapters[state.chapter + 1].start, { remember: true });
}

function playbackUI() {
  const playing = !audio.paused;
  $('play').setAttribute('aria-label', playing ? 'Пауза' : 'Воспроизвести');
  $('play-icon').setAttribute('href', playing ? '#i-pause' : '#i-play');
  document.body.classList.toggle('playing', playing);
  if (navigator.mediaSession) navigator.mediaSession.playbackState = playing ? 'playing' : 'paused';
  updateWakeLock();
}

// ---------- оглавление и закладки ----------
function buildChapters() {
  const list = $('chapters');
  list.textContent = '';
  const fragment = document.createDocumentFragment();
  state.book.chapters.forEach((chapter, index) => {
    const li = document.createElement('li'); li.dataset.index = index;
    const b = document.createElement('button'); b.className = 'row';
    const n = document.createElement('span'); n.className = 'row-n'; n.textContent = String(index + 1);
    const t = document.createElement('span'); t.className = 'row-title'; t.textContent = chapter.title;
    const d = document.createElement('span'); d.className = 'row-meta'; d.textContent = formatTime(chapter.end - chapter.start);
    b.append(n, t, d);
    b.addEventListener('click', () => { closeSheets(); jump(chapter.start, { remember: true }); if (audio.paused) play(); });
    li.append(b); fragment.append(li);
  });
  list.append(fragment);
  $('chapter-search').value = ''; $('no-chapters').hidden = true;
}
function renderBookmarks() {
  const list = $('bookmarks'), book = state.book;
  list.textContent = '';
  const marks = [...state.bookmarks].sort((a, b) => a.position - b.position);
  $('bookmark-count').textContent = marks.length ? `(${marks.length})` : '';
  $('no-bookmarks').hidden = marks.length > 0;
  for (const mark of marks) {
    const li = document.createElement('li'); li.className = 'row-wrap';
    const b = document.createElement('button'); b.className = 'row';
    const chapter = book.chapters[chapterAt(book.chapters, mark.position)];
    const t = document.createElement('span'); t.className = 'row-title';
    t.textContent = mark.note ? `${mark.note}` : chapter.title;
    const d = document.createElement('span'); d.className = 'row-meta';
    d.textContent = `${mark.note ? chapter.title + ' · ' : ''}${formatTime(mark.position - chapter.start)}`;
    b.append(t, d);
    b.addEventListener('click', () => { closeSheets(); jump(mark.position, { remember: true }); });
    const edit = document.createElement('button'); edit.className = 'icon-btn'; edit.setAttribute('aria-label', 'Изменить закладку');
    edit.innerHTML = '<svg><use href="#i-more"/></svg>';
    edit.addEventListener('click', () => editBookmark(mark.id));
    li.append(b, edit); list.append(li);
  }
}
function addBookmark() {
  if (!state.ready) return;
  const mark = { id: Date.now().toString(36), position: round2(audio.currentTime), note: '', created: new Date().toISOString() };
  state.bookmarks.push(mark);
  store.saveBookmarks(state.book.id, state.bookmarks);
  renderBookmarks();
  const chapter = state.book.chapters[state.chapter];
  if (state.settings.car) toast(`Закладка: ${chapter.title}, ${formatTime(mark.position - chapter.start)}`);
  else toast(`Закладка: ${chapter.title}, ${formatTime(mark.position - chapter.start)}`, 'Подписать', () => editBookmark(mark.id));
}
let editing = null;
function editBookmark(id) {
  const mark = state.bookmarks.find(m => m.id === id);
  if (!mark) return;
  editing = mark;
  const chapter = state.book.chapters[chapterAt(state.book.chapters, mark.position)];
  $('note-where').textContent = `${chapter.title}, ${formatTime(mark.position - chapter.start)}`;
  $('note-text').value = mark.note || '';
  closeSheets(); openSheet('sheet-note');
}

// ---------- скорость и громкость ----------
function setRate(rate) {
  const value = round2(clamp(rate, RATE_MIN, RATE_MAX));
  audio.playbackRate = value; audio.defaultPlaybackRate = value;
  renderSpeed(); save(true);
}
function renderSpeed() {
  const rate = audio.playbackRate || 1;
  $('speed-value').textContent = formatRate(rate);
  $('speed-out').textContent = formatRate(rate);
  chips($('speed-presets'), SPEEDS.map(v => [v, formatRate(v)]), round2(rate), setRate);
}
function setBoost(value) {
  booster.unlock();
  setSetting('boost', round2(clamp(value, BOOST_MIN, BOOST_MAX)));
  renderVolume();
}
function renderVolume() {
  const s = state.settings;
  $('volume-out').textContent = formatPercent(s.boost);
  $('volume-range').value = String(Math.round(s.boost * 100));
  $('volume-range').style.setProperty('--p', `${(s.boost - BOOST_MIN) / (BOOST_MAX - BOOST_MIN) * 100}%`);
  $('leveling').checked = s.leveling;
  chips($('volume-presets'), BOOSTS.map(v => [v, formatPercent(v)]), s.boost, setBoost);
  const note = $('volume-note');
  if ((s.boost > 1 || s.leveling) && !booster.supported) { note.hidden = false; note.textContent = 'Этот браузер не умеет усиливать звук. Громкость ограничена 100%.'; }
  else if ((s.boost > 1 || s.leveling) && isIOS()) { note.hidden = false; note.textContent = 'На iPhone при усилении звук может прерываться на заблокированном экране. Если так случится, верните 100%.'; }
  else note.hidden = true;
}

// ---------- таймер сна ----------
function setTimer(value) {
  booster.setFade(1);
  if (!value) state.timer = null;
  else if (value === 'chapter') state.timer = { chapter: state.chapter };
  else state.timer = { left: value * 60 };
  renderTimer(); closeSheets();
  if (state.timer) toast(value === 'chapter' ? 'Остановлю в конце главы' : `Таймер сна: ${value} мин`);
}
function renderTimer() {
  const t = state.timer;
  const label = !t ? 'Сон' : t.chapter !== undefined ? 'Глава' : formatTime(t.left);
  $('timer-value').textContent = label;
  $('tool-timer').classList.toggle('on', Boolean(t));
  $('timer-state').textContent = !t ? 'Плеер остановится сам и плавно убавит звук в последние 10 секунд.' : t.chapter !== undefined ? 'Остановлю в конце текущей главы.' : `Осталось ${formatTime(t.left)} прослушивания.`;
  chips($('timer-options'), TIMERS, null, setTimer);
}
function chapterTimerCheck() {
  const t = state.timer;
  if (!t || t.chapter === undefined || audio.paused) return;
  const end = state.book.chapters[t.chapter].end;
  const left = (end - audio.currentTime) / (audio.playbackRate || 1);
  if (audio.currentTime >= end - 0.35 || chapterAt(state.book.chapters, audio.currentTime) !== t.chapter) {
    audio.pause(); booster.setFade(1); state.timer = null;
    // Следующий раз начнём ровно с новой главы.
    if (t.chapter < state.book.chapters.length - 1) jump(state.book.chapters[t.chapter + 1].start);
    toast('Таймер сна: глава закончилась'); renderTimer();
  } else booster.setFade(left < 8 ? clamp(left / 8, 0.05, 1) : 1);
}
function timerTick() {
  const t = state.timer;
  if (!t || audio.paused) return;
  if (t.chapter !== undefined) chapterTimerCheck();
  else {
    t.left -= 1;
    booster.setFade(t.left < 10 ? clamp(t.left / 10, 0.05, 1) : 1);
    if (t.left <= 0) { audio.pause(); booster.setFade(1); state.timer = null; jump(audio.currentTime - 15); toast('Таймер сна: пауза. Отмотал 15 секунд назад.'); }
  }
  renderTimer();
}

// ---------- офлайн ----------
async function refreshDownload() {
  const book = state.book;
  if (!book) return;
  const button = $('more-download'), title = $('dl-title'), info = $('dl-state'), bar = $('dl-progress');
  if (!offlineSupported()) { button.disabled = true; info.textContent = 'Нужна HTTPS-ссылка'; $('more-delete').hidden = true; bar.hidden = true; return; }
  if (!state.swReady) { button.disabled = true; info.textContent = 'Готовлю офлайн-режим…'; return; }
  const s = await downloadStatus(book);
  renderDownload(s);
}
function renderDownload(s) {
  const book = state.book;
  const total = book.audio.size, pct = Math.floor(s.bytes / total * 100);
  const button = $('more-download');
  button.disabled = s.complete && !state.downloader;
  $('dl-progress').hidden = !state.downloader;
  $('dl-progress').value = pct;
  $('more-delete').hidden = !s.bytes || Boolean(state.downloader);
  if (state.downloader) { $('dl-title').textContent = 'Остановить загрузку'; $('dl-state').textContent = `${pct}% · ${formatBytes(s.bytes)} из ${formatBytes(total)}`; }
  else if (s.complete) { $('dl-title').textContent = 'Книга скачана'; $('dl-state').textContent = `${formatBytes(total)} · слушается без интернета`; }
  else if (s.bytes) { $('dl-title').textContent = `Продолжить загрузку · ${pct}%`; $('dl-state').textContent = `Сохранено ${formatBytes(s.bytes)} из ${formatBytes(total)}`; }
  else { $('dl-title').textContent = 'Скачать для офлайна'; $('dl-state').textContent = `${formatBytes(total)} · держите приложение открытым`; }
}
async function toggleDownload() {
  const book = state.book;
  if (state.downloader) { state.downloader.abort(); return; }
  const before = await downloadStatus(book);
  const estimate = await storageEstimate();
  if (estimate && estimate.quota && estimate.quota - estimate.usage < book.audio.size - before.bytes + 8e6) { toast('Не хватает места на устройстве для этой книги'); return; }
  try { await navigator.storage?.persist?.(); } catch {}
  const controller = new AbortController();
  state.downloader = controller; renderDownload(before);
  try {
    await downloadBook(book, state.raw, controller.signal, renderDownload);
    toast(`«${book.title}» скачана — слушается без интернета`);
    // Переоткрываем аудио уже через service worker, сохраняя место и воспроизведение.
    if (state.book === book) {
      const wasPlaying = !audio.paused;
      state.pendingRestore = { position: audio.currentTime, rate: audio.playbackRate };
      state.resumeAfterLoad = wasPlaying; state.ready = false;
      const url = new URL(book.audioUrl); url.searchParams.set('offline', '1');
      audio.src = url.href; audio.load();
    }
  } catch (error) {
    if (error.name !== 'AbortError') toast(error.name === 'QuotaExceededError' ? 'Место закончилось. Скачанные части сохранены.' : `Загрузка остановлена: ${error.message}`);
  } finally {
    state.downloader = null;
    if (state.book === book) refreshDownload();
  }
}
async function renderDownloads() {
  const list = $('downloads');
  list.textContent = '';
  const estimate = await storageEstimate();
  $('storage-line').textContent = !offlineSupported() ? 'Офлайн-загрузка работает только по HTTPS-ссылке.'
    : estimate && estimate.quota ? `Занято ${formatBytes(estimate.usage || 0)} из доступных ${formatBytes(estimate.quota)}` : '';
  let any = false;
  for (const entry of state.entries) {
    let book;
    try { book = normalizeBook(await fetchJSON(entry.url), entry.url); } catch { continue; }
    const s = await downloadStatus(book);
    if (!s.bytes) continue;
    any = true;
    const li = document.createElement('li'); li.className = 'row-wrap';
    const info = document.createElement('div'); info.className = 'row';
    const t = document.createElement('span'); t.className = 'row-title'; t.textContent = book.title;
    const m = document.createElement('span'); m.className = 'row-meta'; m.textContent = s.complete ? formatBytes(s.bytes) : `${formatBytes(s.bytes)} · не до конца`;
    info.append(t, m);
    const del = document.createElement('button'); del.className = 'icon-btn'; del.setAttribute('aria-label', `Удалить загрузку «${book.title}»`);
    del.innerHTML = '<svg><use href="#i-trash"/></svg>';
    del.addEventListener('click', async () => { await deleteDownload(book); renderDownloads(); if (state.book?.id === book.id) refreshDownload(); });
    li.append(info, del); list.append(li);
  }
  if (!any) { const li = document.createElement('li'); li.className = 'empty'; li.textContent = 'Скачанных книг нет'; list.append(li); }
}

async function setupServiceWorker() {
  if (!offlineSupported()) return;
  try {
    const registration = await navigator.serviceWorker.register('./sw.js');
    let updateRequested = false;
    const offerUpdate = worker => {
      $('update-bar').hidden = false;
      $('update-now').onclick = () => { updateRequested = true; saveNow(); worker.postMessage('skip-waiting'); };
    };
    if (registration.waiting && navigator.serviceWorker.controller) offerUpdate(registration.waiting);
    registration.addEventListener('updatefound', () => {
      const worker = registration.installing;
      worker?.addEventListener('statechange', () => { if (worker.state === 'installed' && navigator.serviceWorker.controller) offerUpdate(worker); });
    });
    // Перезагружаемся только по нажатию «Обновить»: обновление не обрывает прослушивание.
    navigator.serviceWorker.addEventListener('controllerchange', () => { if (updateRequested) { updateRequested = false; saveNow(); location.reload(); } });
    await navigator.serviceWorker.ready;
    if (!navigator.serviceWorker.controller) await new Promise(resolve => { navigator.serviceWorker.addEventListener('controllerchange', resolve, { once: true }); setTimeout(resolve, 4000); });
    state.swReady = Boolean(navigator.serviceWorker.controller);
    // Каталог через service worker, чтобы библиотека открывалась и без сети.
    if (state.swReady) fetch('library.json', { cache: 'no-cache' }).catch(() => {});
    setInterval(() => registration.update().catch(() => {}), 60 * 60 * 1000);
  } catch {}
  if (state.book) { migrateLegacy(state.book).catch(() => {}).then(refreshDownload); rememberBook(state.book, state.raw).catch(() => {}); }
  if (!$('library-view').hidden) renderLibrary();
}

// ---------- режим «Авто» ----------
async function updateWakeLock() {
  const want = state.settings.car && state.settings.wakeLock && !audio.paused && !document.hidden;
  try {
    if (want && !state.wakeLock && navigator.wakeLock) {
      state.wakeLock = await navigator.wakeLock.request('screen');
      state.wakeLock.addEventListener('release', () => { state.wakeLock = null; });
    } else if (!want && state.wakeLock) { await state.wakeLock.release(); state.wakeLock = null; }
  } catch { state.wakeLock = null; }
}
function setupGestures() {
  const zone = $('gesture-zone');
  let start = null;
  zone.addEventListener('pointerdown', e => { if (state.settings.car) start = { x: e.clientX, y: e.clientY, t: Date.now() }; });
  zone.addEventListener('pointerup', e => {
    if (!state.settings.car || !start) return;
    const dx = e.clientX - start.x, dy = e.clientY - start.y;
    start = null;
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) { skip(dx > 0 ? 1 : -1); toast(dx > 0 ? `+${skipLabel(state.settings.skip)}` : `−${skipLabel(state.settings.skip)}`, '', null, 1200); }
    else if (Math.abs(dx) < 15 && Math.abs(dy) < 15) togglePlay();
  });
  zone.addEventListener('pointercancel', () => { start = null; });
}

// ---------- иллюстрации и настройки ----------
function renderImages() {
  const book = state.book;
  $('auto-image').checked = state.settings.autoImage && !state.manualImage;
  const thumbs = $('thumbs');
  thumbs.textContent = '';
  for (const [key, image] of Object.entries(book.images)) {
    const b = document.createElement('button'); b.className = 'thumb'; b.setAttribute('aria-pressed', String(key === state.shownImage));
    const img = document.createElement('img'); img.src = image.url; img.alt = '';
    const cap = document.createElement('span'); cap.textContent = image.title || key;
    b.append(img, cap);
    b.addEventListener('click', () => { state.manualImage = true; showImage(key); renderImages(); });
    thumbs.append(b);
  }
  const current = state.shownImage && book.images[state.shownImage];
  $('large-image').src = current ? current.url : ''; $('large-image').alt = current ? current.alt || '' : '';
  $('large-image').hidden = !current;
}
function renderSettings() {
  const s = state.settings;
  chips($('set-theme'), [['dark', 'Тёмная'], ['light', 'Светлая'], ['system', 'Как в системе']], s.theme, v => { setSetting('theme', v); renderSettings(); });
  chips($('set-scale'), [[1, 'Обычный'], [1.2, 'Крупный'], [1.4, 'Очень крупный']], s.scale, v => { setSetting('scale', v); renderSettings(); });
  chips($('set-skip'), [[10, '10 с'], [15, '15 с'], [30, '30 с'], [60, '1 мин']], s.skip, v => { setSetting('skip', v); renderSettings(); });
  $('set-wake').checked = s.wakeLock; $('set-autoplay').checked = s.autoplay;
  $('install').hidden = !state.installEvent;
  $('about').textContent = `Плеер 2.0 · ${state.entries.length} ${plural(state.entries.length, 'книга', 'книги', 'книг')} в библиотеке. Позиции и настройки хранятся на этом устройстве.`;
}

// ---------- маршрут ----------
function route() {
  const match = /^#book=(.+)$/.exec(location.hash);
  if (match) openBook(decodeURIComponent(match[1]));
  else showLibrary(false); // звук продолжает играть и в библиотеке
}

// ---------- события ----------
function bind() {
  audio.addEventListener('loadedmetadata', onMetadata);
  audio.addEventListener('timeupdate', () => { render(); save(false); chapterTimerCheck(); });
  audio.addEventListener('play', () => { playbackUI(); booster.resume(); });
  audio.addEventListener('playing', () => status(''));
  audio.addEventListener('pause', () => { playbackUI(); saveNow(); });
  audio.addEventListener('ended', () => { playbackUI(); saveNow(); render(); toast('Книга закончилась'); });
  audio.addEventListener('seeked', () => { render(); save(true); });
  audio.addEventListener('ratechange', () => { renderSpeed(); updatePositionState(); });
  audio.addEventListener('waiting', () => status('Загрузка аудио…'));
  audio.addEventListener('error', () => { if (state.book) { status(''); toast('Аудио недоступно. Проверьте интернет или скачайте книгу для офлайна.', '', null, 8000); } });

  $('play').addEventListener('click', togglePlay);
  $('back').addEventListener('click', () => skip(-1));
  $('forward').addEventListener('click', () => skip(1));
  $('prev-chapter').addEventListener('click', prevChapter);
  $('next-chapter').addEventListener('click', nextChapter);
  $('seek').addEventListener('input', () => { state.seeking = true; const c = state.book.chapters[state.chapter]; $('elapsed').textContent = formatTime(Number($('seek').value)); $('remaining').textContent = `−${formatTime(c.end - c.start - Number($('seek').value))}`; });
  $('seek').addEventListener('change', () => { state.seeking = false; jump(state.book.chapters[state.chapter].start + Number($('seek').value)); });

  $('to-library').addEventListener('click', () => {
    if (state.navPushed) { state.navPushed = false; history.back(); }
    else { history.replaceState({ view: 'library' }, '', location.pathname + location.search); showLibrary(false); }
  });
  $('car-toggle').addEventListener('click', () => { setSetting('car', !state.settings.car); toast(state.settings.car ? 'Режим «Авто»: тап по обложке — пауза, свайп — перемотка' : 'Обычный режим', '', null, 3000); });
  $('continue-play').addEventListener('click', () => openBook(store.lastBook(), { push: true, autoplay: true }));
  $('continue').addEventListener('click', e => { if (!e.target.closest('#continue-play')) openBook(store.lastBook(), { push: true }); });
  $('library-search').addEventListener('input', renderLibrary);
  $('open-settings').addEventListener('click', () => { renderSettings(); openSheet('sheet-settings'); });
  $('open-downloads').addEventListener('click', () => { renderDownloads(); openSheet('sheet-downloads'); });

  $('tool-speed').addEventListener('click', () => { renderSpeed(); openSheet('sheet-speed'); });
  $('speed-down').addEventListener('click', () => setRate((audio.playbackRate || 1) - 0.05));
  $('speed-up').addEventListener('click', () => setRate((audio.playbackRate || 1) + 0.05));
  $('tool-volume').addEventListener('click', () => { renderVolume(); openSheet('sheet-volume'); });
  $('volume-down').addEventListener('click', () => setBoost(state.settings.boost - 0.1));
  $('volume-up').addEventListener('click', () => setBoost(state.settings.boost + 0.1));
  $('volume-range').addEventListener('input', () => setBoost(Number($('volume-range').value) / 100));
  $('leveling').addEventListener('change', () => { booster.unlock(); setSetting('leveling', $('leveling').checked); renderVolume(); });
  $('tool-timer').addEventListener('click', () => { renderTimer(); openSheet('sheet-timer'); });
  $('tool-chapters').addEventListener('click', () => {
    selectTab('chapters'); openSheet('sheet-chapters');
    $('chapters').children[state.chapter]?.scrollIntoView({ block: 'center' });
  });
  $('tool-bookmark').addEventListener('click', addBookmark);
  $('tool-more').addEventListener('click', () => { refreshDownload(); $('images-state').textContent = `${Object.keys(state.book?.images || {}).length} шт.`; openSheet('sheet-more'); });
  $('more-download').addEventListener('click', () => toggleDownload().catch(() => toast('Не удалось открыть хранилище браузера')));
  $('more-delete').addEventListener('click', async () => { await deleteDownload(state.book); refreshDownload(); toast('Загрузка удалена'); });
  $('more-images').addEventListener('click', () => { closeSheets(); renderImages(); openSheet('sheet-images'); });
  $('more-settings').addEventListener('click', () => { closeSheets(); renderSettings(); openSheet('sheet-settings'); });
  $('art-button').addEventListener('click', () => { if (!state.settings.car && state.book && Object.keys(state.book.images).length) { renderImages(); openSheet('sheet-images'); } });
  $('auto-image').addEventListener('change', () => { state.manualImage = !$('auto-image').checked; setSetting('autoImage', $('auto-image').checked); state.shownImage = undefined; render(); renderImages(); });

  $('tab-chapters').addEventListener('click', () => selectTab('chapters'));
  $('tab-bookmarks').addEventListener('click', () => selectTab('bookmarks'));
  $('chapter-search').addEventListener('input', e => {
    const q = e.target.value.toLocaleLowerCase('ru').trim(); let count = 0;
    for (const row of $('chapters').children) { row.hidden = !row.textContent.toLocaleLowerCase('ru').includes(q); if (!row.hidden) count++; }
    $('no-chapters').hidden = count > 0;
  });
  $('note-form').addEventListener('submit', () => {
    if (editing) { editing.note = $('note-text').value.trim(); store.saveBookmarks(state.book.id, state.bookmarks); renderBookmarks(); }
  });
  $('note-delete').addEventListener('click', () => {
    state.bookmarks = state.bookmarks.filter(m => m !== editing);
    store.saveBookmarks(state.book.id, state.bookmarks); renderBookmarks(); closeSheets(); toast('Закладка удалена');
  });

  $('set-wake').addEventListener('change', () => setSetting('wakeLock', $('set-wake').checked));
  $('set-autoplay').addEventListener('change', () => setSetting('autoplay', $('set-autoplay').checked));
  $('export-progress').addEventListener('click', () => {
    saveNow();
    const blob = new Blob([JSON.stringify(store.exportAll(), null, 2)], { type: 'application/json' });
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = `прогресс-аудиокниг-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
  $('import-progress').addEventListener('change', async e => {
    const file = e.target.files[0]; e.target.value = '';
    if (!file) return;
    try {
      const count = store.importAll(JSON.parse(await file.text()));
      state.settings = store.loadSettings(); applySettings();
      toast(`Загружено записей: ${count}`);
      if (state.book) { const id = state.book.id; state.book = null; openBook(id); }
      renderLibrary();
    } catch (error) { toast(error.message || 'Не удалось прочитать файл'); }
  });
  $('install').addEventListener('click', async () => { if (state.installEvent) { await state.installEvent.prompt(); state.installEvent = null; renderSettings(); } });
  window.addEventListener('beforeinstallprompt', e => { e.preventDefault(); state.installEvent = e; });

  document.querySelectorAll('dialog').forEach(d => {
    d.querySelectorAll('.close').forEach(b => b.addEventListener('click', () => d.close()));
    d.addEventListener('click', e => { if (e.target === d) d.close(); }); // тап по затемнению
  });

  document.addEventListener('keydown', e => {
    if (!state.ready || $('player-view').hidden || e.altKey || e.ctrlKey || e.metaKey || document.querySelector('dialog[open]')) return;
    if (e.target.closest('input,select,textarea,[contenteditable=true]')) return;
    const keys = { Space: togglePlay, KeyK: togglePlay, ArrowLeft: () => skip(-1), KeyJ: () => skip(-1), ArrowRight: () => skip(1), KeyL: () => skip(1), PageUp: prevChapter, PageDown: nextChapter, KeyB: addBookmark };
    if (e.target.closest('button,a') && e.code === 'Space') return;
    if (keys[e.code]) { e.preventDefault(); keys[e.code](); }
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden) saveNow(); else render(); updateWakeLock(); });
  window.addEventListener('pagehide', saveNow);
  window.addEventListener('popstate', () => { state.navPushed = false; route(); });
  matchMedia('(prefers-color-scheme: light)').addEventListener?.('change', () => { if (state.settings.theme === 'system') applySettings(); });
  setInterval(timerTick, 1000);

  if ('mediaSession' in navigator) {
    const actions = {
      play: () => play(), pause: () => audio.pause(), stop: () => audio.pause(),
      seekbackward: d => jump(audio.currentTime - (d?.seekOffset || state.settings.skip)),
      seekforward: d => jump(audio.currentTime + (d?.seekOffset || state.settings.skip)),
      seekto: d => jump(d.seekTime),
      // Кнопки ⏮ ⏭ на руле: перемотка на шаг, чтобы случайное нажатие не уводило на целую главу.
      previoustrack: () => skip(-1), nexttrack: () => skip(1)
    };
    for (const [action, handler] of Object.entries(actions)) { try { navigator.mediaSession.setActionHandler(action, handler); } catch {} }
  }
}
function selectTab(name) {
  for (const tab of ['chapters', 'bookmarks']) {
    $(`tab-${tab}`).setAttribute('aria-selected', String(tab === name));
    $(`pane-${tab}`).hidden = tab !== name;
  }
}

async function init() {
  applySettings(); bind(); setupGestures(); renderTimer();
  try { await loadLibrary(); }
  catch { $('library-empty').hidden = false; $('library-empty').textContent = 'Не удалось загрузить библиотеку. Проверьте соединение и обновите страницу.'; }
  history.replaceState(location.hash.startsWith('#book=') ? { view: 'book', id: decodeURIComponent(location.hash.slice(6)) } : { view: 'library' }, '');
  route();
  setupServiceWorker();
}
init();
