// Service worker плеера: оболочка офлайн, описания книг, аудио из скачанных частей.
// Обычный (не модульный) скрипт: модульные service worker поддерживаются не везде.
// Новая версия НЕ перезагружает открытые страницы: плеер показывает «Обновить», когда удобно.
'use strict';
const VERSION = '2.0.0';
const base = new URL('./', self.location.href);
const scope = encodeURIComponent(base.pathname);
const SHELL_PREFIX = `vox-shell-${scope}-`;
const SHELL = SHELL_PREFIX + VERSION;
const DATA = `vox-data-${scope}`;
const audioCacheName = book => `vox-audio-${scope}-${book.id}-${book.version}`;
const chunkURL = (book, index) => new URL(`_chunks/${book.id}/${book.version}/${index}`, base).href;
const FILES = ['./', 'index.html', 'manifest.webmanifest', 'app/app.css', 'app/main.mjs', 'app/model.mjs', 'app/store.mjs',
  'app/booster.mjs', 'app/offline.mjs', 'icons/icon.svg', 'icons/icon-192.png', 'icons/icon-512.png'];
const AUDIO_RE = /^books\/([a-z0-9][a-z0-9_-]*)\/book\.(m4b|m4a|mp3)$/;

self.addEventListener('install', event => event.waitUntil((async () => {
  const cache = await caches.open(SHELL);
  await cache.addAll(FILES.map(f => new Request(new URL(f, base), { cache: 'reload' })));
  // Переход со старого плеера «Сокрушителя Войн»: включаемся сразу, его страница не умеет предлагать обновление.
  if ((await caches.keys()).some(k => k.startsWith('warbreaker-shell-'))) await self.skipWaiting();
})()));

self.addEventListener('message', event => { if (event.data === 'skip-waiting') self.skipWaiting(); });

self.addEventListener('activate', event => event.waitUntil((async () => {
  for (const key of await caches.keys()) {
    if ((key.startsWith(SHELL_PREFIX) && key !== SHELL) || key.startsWith('warbreaker-shell-')) await caches.delete(key);
  }
  await self.clients.claim();
})()));

function parseRange(value, size) {
  if (!value) return { start: 0, end: size - 1, partial: false };
  const m = /^bytes=(\d*)-(\d*)$/.exec(value.trim());
  if (!m || (!m[1] && !m[2])) return null;
  let start, end;
  if (!m[1]) { const n = Number(m[2]); if (!(n > 0)) return null; start = Math.max(0, size - n); end = size - 1; }
  else { start = Number(m[1]); end = m[2] ? Number(m[2]) : size - 1; if (end < start || start >= size) return null; end = Math.min(end, size - 1); }
  return { start, end, partial: true };
}

async function serveAudio(request, id) {
  const data = await caches.open(DATA);
  const described = await data.match(new URL(`books/${id}/book.json`, base).href);
  if (!described) return fetch(request);
  const book = await described.json();
  if (!(await caches.has(audioCacheName(book)))) return fetch(request);
  const size = book.audio.size, chunkSize = book.audio.chunkSize;
  const range = parseRange(request.headers.get('Range'), size);
  if (!range) return new Response(null, { status: 416, headers: { 'Content-Range': `bytes */${size}` } });
  const cache = await caches.open(audioCacheName(book));
  const first = Math.floor(range.start / chunkSize), last = Math.floor(range.end / chunkSize);
  for (let i = first; i <= last; i++) {
    if (!(await cache.match(chunkURL(book, i)))) {
      try { return await fetch(request); } catch { return new Response('Книга скачана не полностью', { status: 503 }); }
    }
  }
  let index = first;
  const stream = new ReadableStream({
    async pull(controller) {
      try {
        if (index > last) { controller.close(); return; }
        const part = await cache.match(chunkURL(book, index));
        if (!part) throw new Error('Часть книги удалена');
        const bytes = new Uint8Array(await part.arrayBuffer());
        const offset = index * chunkSize;
        controller.enqueue(bytes.subarray(Math.max(0, range.start - offset), Math.min(bytes.length, range.end - offset + 1)));
        index++;
      } catch (error) { controller.error(error); }
    }
  });
  const headers = { 'Content-Type': book.audio.type || 'audio/mp4', 'Accept-Ranges': 'bytes', 'Content-Length': String(range.end - range.start + 1) };
  if (range.partial) headers['Content-Range'] = `bytes ${range.start}-${range.end}/${size}`;
  return new Response(request.method === 'HEAD' ? null : stream, { status: range.partial ? 206 : 200, headers });
}

// Каталог и описания: сначала сеть (чтобы появлялись новые книги), без сети — последняя копия.
async function networkFirst(request) {
  const data = await caches.open(DATA);
  try {
    const response = await fetch(request, { cache: 'no-cache' });
    if (response.ok) await data.put(request.url.split('?')[0], response.clone());
    return response;
  } catch (error) {
    const cached = await data.match(request.url.split('?')[0]);
    if (cached) return cached;
    throw error;
  }
}

// Обложки и иллюстрации: из кэша, иначе из сети с сохранением.
async function cacheFirst(request) {
  const data = await caches.open(DATA);
  const cached = await data.match(request.url);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) await data.put(request.url, response.clone());
  return response;
}

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || !['GET', 'HEAD'].includes(request.method)) return;
  if (request.headers.has('X-Download-Chunk') || !url.pathname.startsWith(base.pathname)) return;
  const path = url.pathname.slice(base.pathname.length);
  const audio = AUDIO_RE.exec(path);
  if (audio) { event.respondWith(serveAudio(request, audio[1]).catch(() => new Response('Нет соединения', { status: 503 }))); return; }
  if (path === 'library.json' || /^books\/[^/]+\/book\.json$/.test(path)) { event.respondWith(networkFirst(request)); return; }
  if (/^books\/[^/]+\/[^/]+\.(jpe?g|png|webp)$/.test(path)) { event.respondWith(cacheFirst(request)); return; }
  event.respondWith((async () => {
    const shell = await caches.open(SHELL);
    if (request.mode === 'navigate' && (path === '' || path === 'index.html')) {
      const page = await shell.match(new URL('index.html', base).href);
      if (page) return page;
    }
    if (path === '') return (await shell.match(new URL('./', base).href)) || fetch(request);
    return (await shell.match(request, { ignoreSearch: true })) || fetch(request);
  })());
});
