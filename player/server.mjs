// Локальный сервер плеера: приложение из public/, книги из папки библиотеки.
//   node server.mjs --library ../library [--lan] [--port 4173]
// На боевом сервере то же самое делает nginx: содержимое public/ и библиотеки в одной папке.
import http from 'node:http';
import { createReadStream } from 'node:fs';
import { stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { networkInterfaces } from 'node:os';

const here = path.dirname(fileURLToPath(import.meta.url));
const TYPES = {
  '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8', '.webmanifest': 'application/manifest+json',
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp', '.svg': 'image/svg+xml',
  '.m4b': 'audio/mp4', '.m4a': 'audio/mp4', '.mp3': 'audio/mpeg'
};
const AUDIO = new Set(['.m4b', '.m4a', '.mp3']);
const CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'";

export function parseRange(value, size) {
  if (!value) return { start: 0, end: size - 1, partial: false };
  const m = /^bytes=(\d*)-(\d*)$/.exec(value.trim());
  if (!m || (!m[1] && !m[2])) return null;
  let start, end;
  if (!m[1]) {
    const n = Number(m[2]);
    if (!Number.isSafeInteger(n) || n <= 0) return null;
    start = Math.max(0, size - n); end = size - 1;
  } else {
    start = Number(m[1]); end = m[2] ? Number(m[2]) : size - 1;
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || end < start || start >= size) return null;
    end = Math.min(end, size - 1);
  }
  return { start, end, partial: true };
}

function inside(root, relative) {
  const full = path.resolve(root, '.' + relative);
  return full === root || full.startsWith(root + path.sep) ? full : null;
}

export function createServer({ library = path.join(here, 'library'), app = path.join(here, 'public') } = {}) {
  const roots = { app: path.resolve(app), library: path.resolve(library) };
  return http.createServer(async (req, res) => {
    try {
      if (!['GET', 'HEAD'].includes(req.method)) { res.writeHead(405, { Allow: 'GET, HEAD' }); res.end(); return; }
      const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
      if (pathname.includes('\\') || pathname.includes('\0') || pathname.split('/').some(p => p === '..')) { res.writeHead(403); res.end(); return; }
      const fromLibrary = pathname === '/library.json' || pathname.startsWith('/books/');
      const filename = inside(fromLibrary ? roots.library : roots.app, pathname === '/' ? '/index.html' : pathname);
      if (!filename) { res.writeHead(403); res.end(); return; }
      const info = await stat(filename);
      if (!info.isFile()) { res.writeHead(404); res.end(); return; }
      const ext = path.extname(filename).toLowerCase();
      const headers = { 'X-Content-Type-Options': 'nosniff', 'Content-Type': TYPES[ext] || 'application/octet-stream', 'Referrer-Policy': 'no-referrer', 'X-Robots-Tag': 'noindex, nofollow' };
      let range = null;
      if (AUDIO.has(ext)) {
        range = parseRange(req.headers.range, info.size);
        if (!range) { res.writeHead(416, { 'Content-Range': `bytes */${info.size}` }); res.end(); return; }
        Object.assign(headers, { 'Accept-Ranges': 'bytes', 'Cache-Control': 'private, max-age=3600', 'Content-Length': String(range.end - range.start + 1) });
        if (range.partial) headers['Content-Range'] = `bytes ${range.start}-${range.end}/${info.size}`;
      } else {
        Object.assign(headers, { 'Cache-Control': 'no-cache', 'Content-Length': String(info.size), 'Content-Security-Policy': CSP });
      }
      res.writeHead(range?.partial ? 206 : 200, headers);
      if (req.method === 'HEAD') { res.end(); return; }
      const stream = createReadStream(filename, range ? { start: range.start, end: range.end } : {});
      stream.on('error', () => res.destroy()); res.on('close', () => stream.destroy()); stream.pipe(res);
    } catch (error) { if (!res.headersSent) res.writeHead(error.code === 'ENOENT' || error.code === 'ENOTDIR' ? 404 : 400); res.end(); }
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const arg = name => { const i = process.argv.indexOf(name); return i > 0 ? process.argv[i + 1] : undefined; };
  const library = path.resolve(arg('--library') || process.env.VOX_LIBRARY || path.join(here, 'library'));
  const port = Number(arg('--port') || process.env.PORT || 4173);
  const host = process.argv.includes('--lan') ? '0.0.0.0' : '127.0.0.1';
  createServer({ library }).listen(port, host, () => {
    console.log(`Плеер: http://localhost:${port}  (библиотека: ${library})`);
    if (host === '0.0.0.0') for (const list of Object.values(networkInterfaces())) for (const v of list || []) if (v.family === 'IPv4' && !v.internal) console.log(`В локальной сети: http://${v.address}:${port}`);
  });
}
