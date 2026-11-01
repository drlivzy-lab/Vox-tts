import test from 'node:test';
import assert from 'node:assert/strict';
import { chapterAt, formatTime, formatDuration, plural, makeProgress, restoreProgress, illustrationAt, normalizeBook, formatRate } from '../public/app/model.mjs';

const raw = {
  id: 'b', version: 'v-1', title: 'Книга', author: 'А', duration: 300,
  audio: { url: 'book.m4b?v=1', size: 10, chunkSize: 4, chunks: [] },
  chapters: [{ title: 'Пролог', start: 0, end: 100 }, { title: 'Глава 1', start: 100, end: 250 }, { title: 'Эпилог', start: 250, end: 300 }],
  images: { cover: { src: 'cover.jpg', title: 'Обложка' }, map: { src: 'map.jpg', title: 'Карта' } },
  cues: [{ image: 'map', start: 0, end: 100 }]
};
const book = normalizeBook(raw, 'https://example.org/listen/books/b/book.json');

test('URL книги и картинок считаются от book.json', () => {
  assert.equal(book.audioUrl, 'https://example.org/listen/books/b/book.m4b?v=1');
  assert.equal(book.coverUrl, 'https://example.org/listen/books/b/cover.jpg');
  assert.equal(book.images.map.url, 'https://example.org/listen/books/b/map.jpg');
});

test('глава по времени и иллюстрации', () => {
  assert.equal(chapterAt(book.chapters, 0), 0);
  assert.equal(chapterAt(book.chapters, 99.99), 0);
  assert.equal(chapterAt(book.chapters, 100), 1);
  assert.equal(chapterAt(book.chapters, 299), 2);
  assert.equal(illustrationAt(book, 50), 'map');
  assert.equal(illustrationAt(book, 150), 'cover');
  assert.equal(illustrationAt({ ...book, images: {}, cues: [] }, 0), null);
});

test('форматирование по-русски', () => {
  assert.equal(formatTime(3725), '1:02:05');
  assert.equal(formatTime(65), '1:05');
  assert.equal(formatDuration(82377), '22 ч 53 мин');
  assert.equal(formatDuration(3600), '1 ч');
  assert.equal(formatRate(1.25), '1,25×');
  assert.deepEqual([1, 2, 5, 11, 21, 22, 61].map(n => plural(n, 'книга', 'книги', 'книг')), ['книга', 'книги', 'книг', 'книг', 'книга', 'книги', 'книга']);
});

test('позиция той же версии восстанавливается точно, скорость ограничена', () => {
  const p = makeProgress(book, 180, 1.5);
  assert.deepEqual(restoreProgress(JSON.stringify(p), book), { position: 180, rate: 1.5, migrated: false });
  assert.equal(restoreProgress(JSON.stringify({ ...p, rate: 9 }), book).rate, 3);
  assert.equal(restoreProgress('мусор', book), null);
  assert.equal(restoreProgress(JSON.stringify({ version: 'v-1' }), book), null);
});

test('прогресс переносится на новый мастер по главе и доле', () => {
  const old = makeProgress(book, 175, 1); // середина «Главы 1»
  const remastered = normalizeBook({ ...raw, version: 'v-2', chapters: [
    { title: 'Пролог', start: 0, end: 120 }, { title: 'Глава 1', start: 120, end: 420 }, { title: 'Эпилог', start: 420, end: 480 }] }, 'https://x/b/book.json');
  const r = restoreProgress(JSON.stringify(old), remastered);
  assert.equal(r.migrated, true);
  assert.equal(r.position, 120 + 0.5 * 300 - 3);
});

test('прогресс старого плеера (без главы) принимается только для той же версии', () => {
  assert.equal(restoreProgress(JSON.stringify({ version: 'v-1', position: 42, rate: 1.1 }), book).position, 42);
  assert.equal(restoreProgress(JSON.stringify({ version: 'old', position: 42 }), book), null);
});
