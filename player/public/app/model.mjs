// Чистые функции без DOM: их проверяют тесты Node (tests/model.test.mjs).

export const RATE_MIN = 0.5, RATE_MAX = 3, BOOST_MIN = 0.5, BOOST_MAX = 2;
export const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
export const round2 = value => Math.round(value * 100) / 100;

export function chapterAt(chapters, time) {
  for (let i = chapters.length - 1; i >= 0; i--) if (time >= chapters[i].start) return i;
  return 0;
}

export function formatTime(seconds) {
  const t = Math.max(0, Math.floor(Number(seconds) || 0));
  const s = String(t % 60).padStart(2, '0');
  const m = Math.floor(t / 60) % 60;
  const h = Math.floor(t / 3600);
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

export function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

export function formatDuration(seconds) {
  const total = Math.max(0, Math.round((Number(seconds) || 0) / 60));
  const h = Math.floor(total / 60), m = total % 60;
  if (!h) return `${m} мин`;
  return m ? `${h} ч ${m} мин` : `${h} ч`;
}

export const formatRate = rate => `${String(round2(rate)).replace('.', ',')}×`;
export const formatPercent = value => `${Math.round(value * 100)}%`;

export function formatBytes(bytes) {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1).replace('.', ',')} ГБ`;
  return `${Math.max(1, Math.round(bytes / 1e6))} МБ`;
}

// Прогресс хранится с главой и смещением внутри неё, чтобы пережить новый мастер
// с изменёнными таймингами: позиция переносится в ту же главу на ту же долю.
export function makeProgress(book, position, rate) {
  const index = chapterAt(book.chapters, position);
  const chapter = book.chapters[index];
  return {
    version: book.version, position: round2(position), rate: round2(rate),
    chapter: index, chapterTitle: chapter.title, offset: round2(position - chapter.start),
    fraction: Math.round((position - chapter.start) / Math.max(1, chapter.end - chapter.start) * 10000) / 10000,
    savedAt: new Date().toISOString()
  };
}

export function restoreProgress(raw, book) {
  let p;
  try { p = typeof raw === 'string' ? JSON.parse(raw) : raw; } catch { return null; }
  if (!p || !Number.isFinite(p.position)) return null;
  const rate = Number.isFinite(p.rate) ? clamp(p.rate, RATE_MIN, RATE_MAX) : 1;
  if (p.version === book.version) return { position: clamp(p.position, 0, book.duration), rate, migrated: false };
  // Другая версия мастера: ищем ту же главу по названию, затем по номеру.
  let index = -1;
  if (typeof p.chapterTitle === 'string') {
    const same = book.chapters.map((c, i) => c.title === p.chapterTitle ? i : -1).filter(i => i >= 0);
    if (same.length === 1) index = same[0];
    else if (same.length > 1 && same.includes(p.chapter)) index = p.chapter;
  }
  if (index < 0 && Number.isInteger(p.chapter) && p.chapter < book.chapters.length) index = p.chapter;
  if (index < 0) return null;
  const chapter = book.chapters[index];
  const length = chapter.end - chapter.start;
  const fraction = Number.isFinite(p.fraction) ? clamp(p.fraction, 0, 1) : null;
  const offset = fraction !== null ? fraction * length : clamp(Number(p.offset) || 0, 0, length);
  // Минус пара секунд, чтобы не начать посреди слова после переноса.
  return { position: clamp(chapter.start + offset - 3, chapter.start, chapter.end - 0.5), rate, migrated: true };
}

export function illustrationAt(book, position) {
  const cue = (book.cues || []).find(c => position >= c.start && position < c.end);
  if (cue && book.images && book.images[cue.image]) return cue.image;
  if (book.images && book.images.cover) return 'cover';
  return null;
}

export function bookProgress(entry, progress) {
  if (!progress || !entry.duration) return 0;
  return clamp(progress.position / entry.duration, 0, 1);
}

export function skipLabel(seconds) {
  return seconds >= 60 ? `${seconds / 60} мин` : `${seconds} с`;
}

// Нормализация описания книги: старый формат (schema 1) и новый (schema 2).
export function normalizeBook(raw, baseUrl) {
  if (!raw || !Array.isArray(raw.chapters) || !raw.chapters.length || !raw.audio) throw new Error('Неверное описание книги');
  const resolve = src => src ? new URL(src, baseUrl).href : '';
  const images = {};
  for (const [key, image] of Object.entries(raw.images || {})) images[key] = { ...image, url: resolve(image.src) };
  if (!images.cover && raw.cover) images.cover = { src: raw.cover, url: resolve(raw.cover), title: 'Обложка', alt: `Обложка книги «${raw.title}»` };
  const chapters = raw.chapters.map((c, i) => ({ id: c.id || String(i).padStart(2, '0'), title: c.title || `Раздел ${i + 1}`, start: Number(c.start), end: Number(c.end) }));
  const duration = Number(raw.duration) || chapters[chapters.length - 1].end;
  return {
    ...raw, chapters, images, duration, cues: raw.cues || [],
    baseUrl, audioUrl: resolve(raw.audio.url), coverUrl: images.cover ? images.cover.url : ''
  };
}
