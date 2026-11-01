import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createServer, parseRange } from '../server.mjs';

test('разбор Range', () => {
  assert.deepEqual(parseRange('bytes=3-7', 10), { start: 3, end: 7, partial: true });
  assert.deepEqual(parseRange('bytes=7-', 10), { start: 7, end: 9, partial: true });
  assert.deepEqual(parseRange('bytes=-3', 10), { start: 7, end: 9, partial: true });
  assert.deepEqual(parseRange('bytes=0-999', 10), { start: 0, end: 9, partial: true });
  for (const r of ['bytes=10-', 'bytes=7-3', 'bytes=-0', 'bytes=', 'bytes=0-1,3-4', 'items=0-2']) assert.equal(parseRange(r, 10), null);
});

test('сервер отдаёт приложение, каталог и книги частями, ничего вне папок', async t => {
  const library = await mkdtemp(path.join(tmpdir(), 'vox-lib-'));
  await mkdir(path.join(library, 'books', 'x'), { recursive: true });
  const bytes = Buffer.from(Array.from({ length: 1000 }, (_, i) => i % 256));
  await writeFile(path.join(library, 'books', 'x', 'book.m4b'), bytes);
  await writeFile(path.join(library, 'library.json'), JSON.stringify({ books: [] }));
  await writeFile(path.join(library, 'secret.txt'), 'нельзя');
  const server = createServer({ library });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(() => server.close());
  const base = `http://127.0.0.1:${server.address().port}`;

  const index = await fetch(base + '/');
  assert.equal(index.status, 200);
  assert.match(index.headers.get('content-security-policy'), /default-src 'self'/);
  assert.equal(await index.text(), await readFile(new URL('../public/index.html', import.meta.url), 'utf8'));
  assert.equal((await fetch(base + '/app/main.mjs')).headers.get('content-type'), 'text/javascript; charset=utf-8');
  assert.equal((await fetch(base + '/library.json')).status, 200);

  const part = await fetch(base + '/books/x/book.m4b', { headers: { Range: 'bytes=100-199' } });
  assert.equal(part.status, 206);
  assert.equal(part.headers.get('content-range'), 'bytes 100-199/1000');
  assert.equal(part.headers.get('content-type'), 'audio/mp4');
  assert.deepEqual(Buffer.from(await part.arrayBuffer()), bytes.subarray(100, 200));
  assert.equal((await fetch(base + '/books/x/book.m4b', { headers: { Range: 'bytes=1000-' } })).status, 416);

  for (const p of ['/secret.txt', '/books/../secret.txt', '/%2e%2e/secret.txt', '/..%5csecret.txt', '/books/x/']) {
    assert.ok([403, 404].includes((await fetch(base + p)).status), p);
  }
  assert.equal((await fetch(base, { method: 'POST' })).status, 405);
});

test('service worker перечисляет существующие файлы оболочки', async () => {
  const sw = await readFile(new URL('../public/sw.js', import.meta.url), 'utf8');
  const files = JSON.parse(sw.match(/const FILES = (\[[^\]]+\])/)[1].replace(/'/g, '"'));
  for (const f of files) if (f !== './') await readFile(new URL(`../public/${f}`, import.meta.url));
  const html = await readFile(new URL('../public/index.html', import.meta.url), 'utf8');
  for (const m of html.matchAll(/(?:src|href)="(app\/[^"]+|icons\/[^"]+|manifest\.webmanifest)"/g)) assert.ok(files.includes(m[1]), `${m[1]} нет в FILES sw.js`);
  for (const m of (await readFile(new URL('../public/app/main.mjs', import.meta.url), 'utf8')).matchAll(/from '\.\/([^']+)'/g)) assert.ok(files.includes(`app/${m[1]}`), m[1]);
});

test('все id из main.mjs есть в index.html', async () => {
  const html = await readFile(new URL('../public/index.html', import.meta.url), 'utf8');
  const js = await readFile(new URL('../public/app/main.mjs', import.meta.url), 'utf8');
  const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]));
  for (const m of js.matchAll(/\$\('([^']+)'\)/g)) assert.ok(ids.has(m[1]), `#${m[1]}`);
});
