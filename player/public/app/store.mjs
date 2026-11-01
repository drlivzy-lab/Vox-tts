// Всё, что плеер помнит на устройстве (localStorage). Ключи книг: vox:progress:<id>, vox:bookmarks:<id>.
import { restoreProgress, makeProgress } from './model.mjs';

export const DEFAULTS = Object.freeze({
  theme: 'dark', scale: 1, skip: 30, boost: 1, leveling: false, car: false,
  autoImage: true, wakeLock: true, autoplay: false
});

const read = key => { try { const v = localStorage.getItem(key); return v === null ? null : JSON.parse(v); } catch { return null; } };
const write = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch { return false; } };

export function loadSettings() {
  const saved = read('vox:settings') || {};
  const s = { ...DEFAULTS, ...saved };
  if (!['dark', 'light', 'system'].includes(s.theme)) s.theme = 'dark';
  if (![1, 1.2, 1.4].includes(s.scale)) s.scale = 1;
  if (![10, 15, 30, 60].includes(s.skip)) s.skip = 30;
  s.boost = Math.min(2, Math.max(0.5, Number(s.boost) || 1));
  return s;
}
export const saveSettings = settings => write('vox:settings', settings);

export function loadProgress(book) {
  const own = read(`vox:progress:${book.id}`);
  if (own) return restoreProgress(own, book);
  // Старый плеер «Сокрушителя Войн» хранил позицию под listening:<id>.
  try { const legacy = localStorage.getItem(`listening:${book.id}`); if (legacy) return restoreProgress(legacy, book); } catch {}
  return null;
}
export const rawProgress = id => read(`vox:progress:${id}`);
export const saveProgress = (book, position, rate) => write(`vox:progress:${book.id}`, makeProgress(book, position, rate));

export const loadBookmarks = id => (read(`vox:bookmarks:${id}`) || []).filter(b => b && Number.isFinite(b.position));
export const saveBookmarks = (id, list) => write(`vox:bookmarks:${id}`, list);

export const lastBook = () => read('vox:last');
export const setLastBook = id => write('vox:last', id);

export function exportAll() {
  const data = { app: 'vox-player', exported: new Date().toISOString(), items: {} };
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key.startsWith('vox:') || key.startsWith('listening:')) data.items[key] = localStorage.getItem(key);
  }
  return data;
}

// Импорт не затирает более свежий прогресс на этом устройстве.
export function importAll(data) {
  if (!data || data.app !== 'vox-player' || typeof data.items !== 'object') throw new Error('Это не файл прогресса плеера');
  let count = 0;
  for (const [key, value] of Object.entries(data.items)) {
    if (!(key.startsWith('vox:') || key.startsWith('listening:')) || typeof value !== 'string') continue;
    if (key.startsWith('vox:progress:')) {
      const mine = read(key), theirs = JSON.parse(value);
      if (mine && mine.savedAt && theirs.savedAt && mine.savedAt > theirs.savedAt) continue;
    }
    localStorage.setItem(key, value); count++;
  }
  return count;
}
