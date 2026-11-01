// Офлайн-загрузка книг частями с проверкой SHA-256. Те же имена кэшей использует sw.js.
const base = new URL('../', import.meta.url);
const scope = encodeURIComponent(base.pathname);
export const DATA_CACHE = `vox-data-${scope}`;
const audioPrefix = id => `vox-audio-${scope}-${id}-`;
export const audioCacheName = book => `${audioPrefix(book.id)}${book.version}`;
export const chunkURL = (book, index) => new URL(`_chunks/${book.id}/${book.version}/${index}`, base).href;
export const offlineSupported = () => Boolean(window.isSecureContext && 'serviceWorker' in navigator && 'caches' in window);

const hex = buffer => Array.from(new Uint8Array(buffer), v => v.toString(16).padStart(2, '0')).join('');
const aborted = signal => { if (signal.aborted) throw new DOMException('Загрузка остановлена', 'AbortError'); };

export async function downloadStatus(book) {
  if (!('caches' in window)) return { count: 0, bytes: 0, complete: false };
  if (!(await caches.has(audioCacheName(book)))) return { count: 0, bytes: 0, complete: false };
  const cache = await caches.open(audioCacheName(book));
  const keys = new Set((await cache.keys()).map(r => r.url));
  let count = 0, bytes = 0;
  book.audio.chunks.forEach((chunk, i) => { if (keys.has(chunkURL(book, i))) { count++; bytes += chunk.size; } });
  return { count, bytes, complete: count === book.audio.chunks.length };
}

// Описание книги кладём в кэш данных: service worker найдёт по нему части аудио без сети.
export async function rememberBook(book, raw) {
  const cache = await caches.open(DATA_CACHE);
  try { const lib = new URL('library.json', base).href; const r = await fetch(lib, { cache: 'no-cache' }); if (r.ok) await cache.put(lib, r); } catch {}
  await cache.put(new URL('book.json', book.baseUrl).href, new Response(JSON.stringify(raw), { headers: { 'Content-Type': 'application/json' } }));
  if (book.coverUrl) { try { if (!(await cache.match(book.coverUrl))) await cache.add(book.coverUrl); } catch {} }
  for (const image of Object.values(book.images)) { try { if (!(await cache.match(image.url))) await cache.add(image.url); } catch {} }
}

export async function downloadBook(book, raw, signal, onProgress) {
  await rememberBook(book, raw);
  await migrateLegacy(book);
  const cache = await caches.open(audioCacheName(book));
  const total = book.audio.chunks.length;
  let bytes = 0;
  for (let index = 0; index < total; index++) {
    aborted(signal);
    const chunk = book.audio.chunks[index];
    const key = chunkURL(book, index);
    const existing = await cache.match(key);
    if (!existing || existing.headers.get('X-SHA256') !== chunk.sha256) {
      const start = index * book.audio.chunkSize, end = start + chunk.size - 1;
      let buffer, attempt = 0;
      for (;;) {
        try {
          const response = await fetch(book.audioUrl, { headers: { Range: `bytes=${start}-${end}`, 'X-Download-Chunk': '1' }, cache: 'no-store', signal });
          if (response.status !== 206 || response.headers.get('Content-Range') !== `bytes ${start}-${end}/${book.audio.size}`) throw new Error('Сервер не поддерживает загрузку частями.');
          buffer = await response.arrayBuffer();
          if (buffer.byteLength !== chunk.size) throw new Error('Часть аудио пришла не полностью.');
          if (hex(await crypto.subtle.digest('SHA-256', buffer)) !== chunk.sha256) throw new Error('Проверка целостности не пройдена.');
          break;
        } catch (error) {
          if (error.name === 'AbortError' || ++attempt >= 3) throw error;
          await new Promise(r => setTimeout(r, 1500 * attempt));
        }
      }
      aborted(signal);
      await cache.put(key, new Response(buffer, { headers: { 'Content-Type': 'application/octet-stream', 'Content-Length': String(chunk.size), 'X-SHA256': chunk.sha256 } }));
    }
    bytes += chunk.size;
    onProgress({ bytes, count: index + 1, complete: index === total - 1 });
  }
  // Старые версии этой книги больше не нужны.
  for (const name of await caches.keys()) if (name.startsWith(audioPrefix(book.id)) && name !== audioCacheName(book)) await caches.delete(name);
}

export async function deleteDownload(book) {
  for (const name of await caches.keys()) if (name.startsWith(audioPrefix(book.id))) await caches.delete(name);
}

// Загрузка из прежнего плеера (кэш warbreaker-audio-*): переносим части, совпадающие по SHA-256,
// по одной, чтобы не занимать место дважды. Чужие части не трогаем.
export async function migrateLegacy(book) {
  const legacy = (await caches.keys()).filter(n => n.startsWith('warbreaker-audio-'));
  if (!legacy.length) return 0;
  const target = await caches.open(audioCacheName(book));
  let moved = 0;
  for (const name of legacy) {
    const cache = await caches.open(name);
    for (const request of await cache.keys()) {
      const index = Number(new URL(request.url).pathname.split('/').pop());
      const chunk = book.audio.chunks[index];
      const response = await cache.match(request);
      if (chunk && response && response.headers.get('X-SHA256') === chunk.sha256 && Number(book.audio.chunkSize) === 4194304) {
        await target.put(chunkURL(book, index), response); moved++;
        await cache.delete(request);
      }
    }
    if (!(await cache.keys()).length) await caches.delete(name);
  }
  return moved;
}

export async function storageEstimate() {
  try { return await navigator.storage.estimate(); } catch { return null; }
}
