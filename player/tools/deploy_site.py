"""Выкладка собранного плеера (папка build_site.py --out) на сервер по SSH.

Схема: сайт behappy-ptg.ru на Selectel — nginx в контейнере behappy-landing, каталог
/root/behappy-landing/html смонтирован внутрь, плеер лежит в html/listen/<секрет>/.
Подключение — системный ssh с алиасом из ~/.ssh/config (вход по ключу, без пароля).
На сервере нужен python3; ничего не устанавливается.

    py -X utf8 player/tools/deploy_site.py plan        что изменится (ничего не меняет)
    py -X utf8 player/tools/deploy_site.py deploy      выложить, проверить по HTTPS, при сбое откатить
    py -X utf8 player/tools/deploy_site.py verify      проверить опубликованный плеер по HTTPS
    py -X utf8 player/tools/deploy_site.py status      что опубликовано, резервные копии, nginx
    py -X utf8 player/tools/deploy_site.py rollback    вернуть предыдущую версию
    py -X utf8 player/tools/deploy_site.py nginx [--apply]   проверить / восстановить блок плеера в nginx.conf
    py -X utf8 player/tools/deploy_site.py prune --keep 2 [--yes]   удалить старые резервные копии

Выкладка: на сервере создаётся .player-stage-<время> рядом с сайтом; неизменённые файлы
(обычно аудио) берутся жёсткими ссылками из опубликованной версии, остальные загружаются
одним потоком tar; всё сверяется по SHA-256; каталог плеера меняется атомарно
(renameat2), прежний уходит в .player-backup-<время>. Затем HTTPS-проверка; если она
не прошла — прежняя версия возвращается автоматически.

Настройки — player/deploy.local.json (не в git: путь к плееру секретный), образец —
player/deploy.example.json. Журналы без секрета — каталог journal_dir из настроек.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shlex
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
REMOTE_SRC = TOOLS / "deploy_remote.py"
DEFAULT_CONFIG = ROOT / "player" / "deploy.local.json"
AUDIO_EXT = (".m4b", ".m4a", ".mp3")
MIME = {".html": "text/html", ".js": "javascript", ".mjs": "javascript", ".css": "text/css",
        ".json": "application/json", ".webmanifest": "application/manifest+json", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".svg": "image/svg+xml",
        ".m4b": "audio/mp4", ".m4a": "audio/mp4", ".mp3": "audio/mpeg"}
FREE_MARGIN = 300_000_000
PREFIX_RE = re.compile(r"/listen/[A-Za-z0-9_-]{24,}/")

# Блок плеера в nginx.conf. Сравнивается без комментариев и пробелов, поэтому строку-маркер можно дополнять.
NGINX_BLOCK = """\
    # audiobook-test:@TOKEN@ - audiobook player, managed by Vox-tts/player/tools/deploy_site.py
    location = @BARE@ {
        return 302 @PREFIX@;
    }
    location ^~ @PREFIX@ {
        root @ROOT@;
        index index.html;
        try_files $uri $uri/ =404;
        autoindex off;
        gzip on;
        gzip_vary on;
        gzip_proxied any;
        gzip_comp_level 6;
        gzip_min_length 256;
        gzip_types text/css application/javascript application/json application/manifest+json image/svg+xml;
        types {
            text/html html;
            text/css css;
            application/javascript js mjs;
            application/json json;
            application/manifest+json webmanifest;
            image/jpeg jpg jpeg;
            image/png png;
            image/webp webp;
            image/svg+xml svg;
            audio/mp4 m4b m4a;
            audio/mpeg mp3;
        }
        add_header X-Robots-Tag "noindex, nofollow, noarchive, nosnippet" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Cache-Control "private, no-cache" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self'; connect-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'" always;
    }
"""


class DeployError(Exception):
    pass


class RemoteFailure(DeployError):
    """Серверная часть выполнилась и сообщила об ошибке (в отличие от обрыва связи)."""


# ---------- настройки и журнал ----------

def load_config(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise DeployError(f"нет файла настроек {path} (образец: player/deploy.example.json)")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for key in ("ssh_host", "public_host", "prefix"):
        if not cfg.get(key):
            raise DeployError(f"{path.name}: не задано {key}")
    if not PREFIX_RE.fullmatch(cfg["prefix"]):
        raise DeployError("prefix должен иметь вид /listen/<секрет не короче 24 символов>/")
    cfg.setdefault("ssh", "ssh")
    cfg.setdefault("landing_dir", "/root/behappy-landing")
    cfg.setdefault("container", "behappy-landing")
    cfg.setdefault("container_root", "/usr/share/nginx/html")
    cfg.setdefault("aliases", {})
    cfg.setdefault("site", "../site")
    cfg.setdefault("journal_dir", "player/.deploy")
    cfg["token"] = cfg["prefix"].split("/")[2]
    for alias, target in cfg["aliases"].items():
        for p in (alias, target):
            if p.startswith("/") or ".." in Path(p).parts:
                raise DeployError(f"aliases: недопустимый путь {p}")
    return cfg


def resolve(cfg: dict, key: str) -> Path:
    p = Path(cfg[key])
    return p if p.is_absolute() else (ROOT / p).resolve()


def redact(value, token: str):
    if isinstance(value, str):
        return value.replace(token, "<secret>")
    if isinstance(value, list):
        return [redact(v, token) for v in value]
    if isinstance(value, dict):
        return {redact(k, token): redact(v, token) for k, v in value.items()}
    return value


class Journal:
    def __init__(self, cfg: dict, kind: str, stamp: str):
        self.cfg, self.data = cfg, {"kind": kind, "stamp_utc": stamp, "host": cfg["public_host"],
                                    "ssh_host": cfg["ssh_host"], "url": f"https://{cfg['public_host']}<secret>"}
        self.path = resolve(cfg, "journal_dir") / f"{kind}-{stamp}.json"

    def __call__(self, **fields):
        self.data.update(fields)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(redact(self.data, self.cfg["token"]), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def utc_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def say(cfg: dict, *parts) -> None:
    print(redact(" ".join(str(p) for p in parts), cfg["token"]), flush=True)


# ---------- локальная сборка ----------

def hash_file(path: Path, chunk_size: int | None = None) -> tuple[str, list[dict]]:
    whole, chunks = hashlib.sha256(), []
    step = chunk_size or (1 << 20)
    with path.open("rb") as f:
        for block in iter(lambda: f.read(step), b""):
            whole.update(block)
            if chunk_size:
                chunks.append({"size": len(block), "sha256": hashlib.sha256(block).hexdigest()})
    return whole.hexdigest(), chunks


def scan_site(site: Path, aliases: dict) -> dict:
    """Опись сборки и проверка её целостности: книги, аудио, части офлайн-загрузки, иллюстрации."""
    site = Path(site)
    if not (site / "index.html").is_file() or not (site / "sw.js").is_file():
        raise DeployError(f"{site} не похожа на сборку плеера (нет index.html или sw.js): выполните build_site.py")
    paths = {}
    for p in sorted(site.rglob("*")):
        rel = p.relative_to(site).as_posix()
        if p.is_symlink():
            raise DeployError(f"в сборке символическая ссылка: {rel}")
        if p.is_file():
            if p.name.endswith(".tmp") or any(part.startswith(".") for part in Path(rel).parts):
                raise DeployError(f"в сборке служебный файл {rel}: пересоберите build_site.py")
            paths[rel] = p
    library = json.loads((site / "library.json").read_text(encoding="utf-8"))
    chunked, books, problems = {}, [], []
    for entry in library.get("books", []):
        rel = entry.get("path", "")
        if rel not in paths:
            problems.append(f"library.json → {entry.get('id')}: нет {rel}")
            continue
        book = json.loads(paths[rel].read_text(encoding="utf-8"))
        folder = Path(rel).parent.as_posix()
        audio = f"{folder}/{book['audio']['url'].split('?')[0]}"
        if audio not in paths:
            problems.append(f"{rel}: нет аудио {audio}")
        else:
            chunked[audio] = (book, int(book["audio"].get("chunkSize") or 0))
        for key, image in (book.get("images") or {}).items():
            if f"{folder}/{image['src']}" not in paths:
                problems.append(f"{rel}: нет иллюстрации {image['src']}")
        if entry.get("version") != book.get("version"):
            problems.append(f"{entry.get('id')}: версия в library.json {entry.get('version')} ≠ {book.get('version')}")
        books.append({"id": book.get("id"), "version": book.get("version"), "title": book.get("title")})
    files = {}
    for rel, p in paths.items():
        book, chunk_size = chunked.get(rel, (None, None))
        sha, chunks = hash_file(p, chunk_size or None)
        files[rel] = {"bytes": p.stat().st_size, "sha256": sha}
        if book:
            a = book["audio"]
            if a.get("sha256") != sha or a.get("size") != files[rel]["bytes"]:
                problems.append(f"{rel}: SHA-256 или размер не совпадает с book.json (пересоберите сайт)")
            elif chunk_size and [c["sha256"] for c in a.get("chunks", [])] != [c["sha256"] for c in chunks]:
                problems.append(f"{rel}: хеши частей офлайн-загрузки не совпадают с book.json")
    for alias, target in aliases.items():
        if target not in files:
            problems.append(f"псевдоним {alias}: в сборке нет {target}")
        if alias in files:
            problems.append(f"псевдоним {alias} совпадает с файлом сборки")
    if not books:
        problems.append("в library.json нет книг")
    if problems:
        raise DeployError("сборка не прошла проверку:\n  " + "\n  ".join(problems))
    return {"files": files, "books": books}


def make_plan(local: dict, remote: dict, aliases: dict) -> dict:
    """Какие файлы взять жёсткой ссылкой с сервера, какие загрузить, какие исчезнут."""
    by_sha = {}
    for rel, row in remote.items():
        by_sha.setdefault(row["sha256"], rel)
    links, upload = [], []
    for rel, row in local.items():
        if remote.get(rel, {}).get("sha256") == row["sha256"]:
            links.append([rel, rel])
        elif row["sha256"] in by_sha:
            links.append([rel, by_sha[row["sha256"]]])
        else:
            upload.append(rel)
    wanted = dict(local)
    for alias, target in aliases.items():
        wanted[alias] = local[target]
    removed = sorted(set(remote) - set(wanted))
    changed = sorted(rel for rel in wanted if remote.get(rel, {}).get("sha256") != wanted[rel]["sha256"])
    return {"links": links, "upload": upload, "upload_bytes": sum(local[r]["bytes"] for r in upload),
            "removed": removed, "changed": changed, "identical": not removed and not changed}


# ---------- сервер ----------

class Server:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = {"landing": cfg["landing_dir"], "prefix": cfg["prefix"], "container": cfg["container"],
                     "marker": f"audiobook-test:{cfg['token']}", "block": nginx_block(cfg)}

    def ssh(self, *remote: str) -> list[str]:
        return [self.cfg["ssh"], "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=8", self.cfg["ssh_host"], *remote]

    def call(self, op: str, timeout: int = 1800, **args) -> dict:
        payload = dict(self.base, op=op, **args)
        source = REMOTE_SRC.read_text(encoding="utf-8")
        source += f"\nrun(json.loads({json.dumps(payload, ensure_ascii=False)!r}))\n"
        try:
            r = subprocess.run(self.ssh("python3", "-"), input=source.encode("utf-8"), capture_output=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise DeployError(f"{op}: нет ответа сервера за {timeout} с — состояние неизвестно, проверьте status") from e
        lines = [line for line in r.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
        try:
            result = json.loads(lines[-1])
        except (IndexError, ValueError):
            raise DeployError(f"{op}: ssh вернул {r.returncode}: "
                              + redact(r.stderr.decode("utf-8", "replace")[-1500:], self.cfg["token"]))
        if not result.get("ok"):
            raise RemoteFailure(f"{op}: {redact(result.get('error', '?'), self.cfg['token'])}")
        return result

    def upload(self, stage: str, site: Path, rels: list[str], sizes: dict) -> None:
        """Один поток tar через ssh в каталог stage; tar -k не перезапишет уже связанные файлы."""
        if not rels:
            return
        target = f"{self.cfg['landing_dir'].rstrip('/')}/{stage}"
        cmd = self.ssh(f"tar -x -k --no-same-owner -f - -C {shlex.quote(target)}")
        total, sent, started, last = sum(sizes[r] for r in rels), 0, time.monotonic(), 0.0

        class Progress:
            def __init__(self, stream, rel):
                self.stream, self.rel = stream, rel

            def read(self, n=-1):
                nonlocal sent, last
                block = self.stream.read(n)
                sent += len(block)
                now = time.monotonic()
                if now - last > 10 or sent == total:
                    last = now
                    speed = sent / max(now - started, 0.001) / 1e6
                    print(f"  загрузка: {sent / 1e6:.1f} из {total / 1e6:.1f} МБ ({speed:.1f} МБ/с) — {self.rel}",
                          flush=True)
                return block

        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=err, stderr=err)
            try:
                try:
                    with tarfile.open(fileobj=proc.stdin, mode="w|", format=tarfile.PAX_FORMAT,
                                      bufsize=1 << 20) as tar:
                        for rel in rels:
                            path = site / rel
                            info = tarfile.TarInfo(rel)
                            info.size, info.mode, info.mtime = sizes[rel], 0o644, int(path.stat().st_mtime)
                            info.uid = info.gid = 0
                            info.uname = info.gname = "root"
                            with path.open("rb") as f:
                                tar.addfile(info, Progress(f, rel))
                    proc.stdin.close()
                except BrokenPipeError:
                    pass  # удалённый tar завершился раньше: причину покажет его код и вывод
                code = proc.wait()
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            err.seek(0)
            message = err.read().decode("utf-8", "replace")[-1500:]
        if code != 0:
            raise DeployError(f"загрузка не удалась (tar/ssh код {code}): {redact(message, self.cfg['token'])}")


def nginx_block(cfg: dict) -> str:
    prefix = cfg["prefix"]
    return (NGINX_BLOCK.replace("@TOKEN@", cfg["token"]).replace("@BARE@", prefix.rstrip("/"))
            .replace("@PREFIX@", prefix).replace("@ROOT@", cfg["container_root"]))


# ---------- HTTPS ----------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def fetch(url: str, headers: dict | None = None, method: str = "GET"):
    req = urllib.request.Request(url, headers={"User-Agent": "vox-deploy-check", **(headers or {})}, method=method)
    try:
        with _OPENER.open(req, timeout=60) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def https_checks(cfg: dict, manifest: dict, home_before: str | None = None) -> tuple[list[str], list[str]]:
    """Опубликованное по HTTPS сверяется с тем, что лежит на сервере (хеши файлов и диапазонов аудио)."""
    host = f"https://{cfg['public_host']}"
    base = host + cfg["prefix"]
    passed, failed = [], []

    def ok(name, cond, detail=""):
        (passed if cond else failed).append(name if cond else f"{name}: {detail}")

    files = manifest["files"]
    for rel, row in files.items():
        ext = Path(rel).suffix.lower()
        if ext in AUDIO_EXT:
            continue
        s, h, body = fetch(base + rel)
        ct = h.get("content-type", "")
        ok(f"файл {rel}", s == 200 and sha(body) == row["sha256"] and MIME.get(ext, "") in ct, f"{s} {ct}")
        ok(f"заголовки {rel}", "noindex" in h.get("x-robots-tag", "") and h.get("referrer-policy") == "no-referrer"
           and h.get("x-content-type-options") == "nosniff" and "no-cache" in h.get("cache-control", ""),
           json.dumps({k: h.get(k) for k in ("x-robots-tag", "referrer-policy", "cache-control")}))

    s, h, body = fetch(base)
    ok("корень плеера", s == 200 and sha(body) == files.get("index.html", {}).get("sha256")
       and "default-src 'self'" in h.get("content-security-policy", ""), str(s))
    s, h, _ = fetch(base.rstrip("/"))
    ok("переадресация без слеша", s in (301, 302) and h.get("location", "").endswith(cfg["prefix"]),
       f"{s} {h.get('location')}")

    audio = [rel for rel in files if Path(rel).suffix.lower() in AUDIO_EXT]
    for rel in audio:
        row = files[rel]
        n = row["bytes"]
        s, h, _ = fetch(base + rel, method="HEAD")
        ok(f"HEAD {rel}", s == 200 and int(h.get("content-length", -1)) == n and h.get("accept-ranges") == "bytes"
           and MIME[Path(rel).suffix.lower()] in h.get("content-type", ""),
           f"{s} {h.get('content-length')} {h.get('content-type')}")
        for rng in row.get("ranges", []):
            a, b = rng["start"], rng["end"]
            s, h, body = fetch(base + rel, {"Range": f"bytes={a}-{b}"})
            ok(f"Range {rel} {a}", s == 206 and sha(body) == rng["sha256"]
               and h.get("content-range") == f"bytes {a}-{b}/{n}", f"{s} {h.get('content-range')}")
    if audio:
        n = files[audio[0]]["bytes"]
        s, _, _ = fetch(base + audio[0], {"Range": f"bytes={n + 10}-{n + 20}"})
        ok("416 за концом файла", s == 416, str(s))

    for d in manifest["dirs"]:
        s, _, body = fetch(base + d + "/")
        ok(f"нет списка файлов {d}/", s in (403, 404) and b"Index of" not in body, str(s))
    s, _, _ = fetch(base + f"missing-{uuid.uuid4().hex[:8]}.json")
    ok("несуществующий файл → 404", s == 404, str(s))

    scripts = [rel for rel, row in files.items() if rel.endswith((".mjs", ".js")) and row["bytes"] > 1024]
    if scripts:
        s, h, body = fetch(base + scripts[0], {"Accept-Encoding": "gzip"})
        try:
            same = h.get("content-encoding") == "gzip" and sha(gzip.decompress(body)) == files[scripts[0]]["sha256"]
        except OSError:
            same = False
        ok(f"gzip {scripts[0]}", s == 200 and same, f"{s} {h.get('content-encoding')}")

    s, _, body = fetch(base + "library.json")
    try:
        library = json.loads(body)
        for entry in library["books"]:
            book_rel = entry["path"]
            s2, _, raw = fetch(base + book_rel)
            book = json.loads(raw)
            folder = Path(book_rel).parent.as_posix()
            audio_rel = f"{folder}/{book['audio']['url'].split('?')[0]}"
            ok(f"книга {entry['id']}", s2 == 200 and audio_rel in files
               and files[audio_rel]["sha256"] == book["audio"]["sha256"]
               and files[audio_rel]["bytes"] == book["audio"]["size"], f"{s2} {audio_rel}")
    except (ValueError, KeyError, TypeError) as e:
        ok("library.json", False, f"{s} {e}")

    s, _, body = fetch(host + "/")
    home = sha(body)
    ok("главная страница сайта", s == 200 and home != files.get("index.html", {}).get("sha256")
       and (home_before is None or home == home_before), f"{s} {home[:12]}")
    s, _, body = fetch(host + "/robots.txt")
    ok("robots.txt без ссылки на плеер", s == 404 or (s == 200 and b"listen" not in body), str(s))
    return passed, failed


def check_manifest(manifest: dict, expected: dict) -> list[str]:
    have = {k: v["sha256"] for k, v in manifest["files"].items()}
    want = {k: v["sha256"] for k, v in expected.items()}
    return sorted(k for k in set(have) | set(want) if have.get(k) != want.get(k))


def expected_files(local_files: dict, aliases: dict) -> dict:
    out = dict(local_files)
    for alias, target in aliases.items():
        out[alias] = local_files[target]
    return out


# ---------- команды ----------

def describe_nginx(st: dict) -> list[str]:
    out = []
    if not st["block_present"]:
        out.append("в nginx.conf нет блока плеера → py -X utf8 player/tools/deploy_site.py nginx --apply")
    elif not st["block_matches"]:
        out.append("блок плеера в nginx.conf отличается от шаблона → nginx --apply обновит его")
    if not st["container_running"]:
        out.append("контейнер сайта не запущен")
    elif not st["container_sees_same_file"]:
        out.append("контейнер читает не тот nginx.conf (файл заменили переименованием): нужен docker restart")
    return out


def service_summary(rows: list[dict]) -> list[str]:
    out = []
    for r in rows:
        books = ", ".join(f"{b['id']} {b['version']}" for b in r["books"]) or "—"
        out.append(f"  {r['name']}: {r['files']} файлов, собственных данных {r['own_bytes'] / 1e6:.1f} МБ; {books}")
    return out


def cmd_plan(cfg: dict, args) -> int:
    site = Path(args.site) if args.site else resolve(cfg, "site")
    say(cfg, f"Проверяю сборку {site}…")
    local = scan_site(site, cfg["aliases"])
    server = Server(cfg)
    info = server.call("inspect")
    plan = make_plan(local["files"], info["files"], cfg["aliases"])
    say(cfg, "Книги сборки: " + "; ".join(f"{b['id']} {b['version']}" for b in local["books"]))
    say(cfg, f"На сервере: {'плеер опубликован' if info['live_exists'] else 'плеера нет (первая выкладка)'}, "
             f"файлов {len(info['files'])}, свободно {info['free_bytes'] / 1e9:.1f} ГБ")
    if plan["identical"]:
        say(cfg, "Сервер уже содержит эту сборку: выкладывать нечего (deploy --force переключит её заново).")
    else:
        say(cfg, f"Изменится файлов: {len(plan['changed'])}; загрузить {len(plan['upload'])} "
                 f"({plan['upload_bytes'] / 1e6:.1f} МБ), взять с сервера ссылкой {len(plan['links'])}, "
                 f"исчезнет {len(plan['removed'])}")
        for rel in plan["changed"]:
            say(cfg, f"  ~ {rel}{'  (загрузка)' if rel in plan['upload'] else ''}")
        for rel in plan["removed"]:
            say(cfg, f"  − {rel}")
    for line in describe_nginx(info["nginx"]):
        say(cfg, "nginx: " + line)
    if not info["same_fs"]:
        say(cfg, "ОШИБКА: каталог сайта и служебные каталоги на разных файловых системах")
    if info["free_bytes"] < plan["upload_bytes"] + FREE_MARGIN:
        say(cfg, "ОШИБКА: на сервере не хватит места")
    if info["service_dirs"]:
        say(cfg, "Служебные каталоги на сервере:")
        for line in service_summary(info["service_dirs"]):
            say(cfg, line)
    return 0


def cmd_deploy(cfg: dict, args) -> int:
    site = Path(args.site) if args.site else resolve(cfg, "site")
    stamp = utc_stamp()
    journal = Journal(cfg, "deploy", stamp)
    say(cfg, f"Проверяю сборку {site}…")
    local = scan_site(site, cfg["aliases"])
    server = Server(cfg)
    home_before = sha(fetch(f"https://{cfg['public_host']}/")[2])
    info = server.call("inspect")
    problems = [line for line in describe_nginx(info["nginx"]) if "отличается от шаблона" not in line]
    if problems:
        raise DeployError("выкладка не начата: " + "; ".join(problems))
    if not info["nginx"]["block_matches"]:
        say(cfg, "Внимание: блок плеера в nginx.conf отличается от шаблона (nginx --apply обновит); HTTPS-проверка покажет, мешает ли это.")
    if not info["same_fs"]:
        raise DeployError("выкладка не начата: сайт и служебные каталоги на разных файловых системах")
    plan = make_plan(local["files"], info["files"], cfg["aliases"])
    if info["free_bytes"] < plan["upload_bytes"] + FREE_MARGIN:
        raise DeployError(f"выкладка не начата: свободно {info['free_bytes'] / 1e9:.1f} ГБ, "
                          f"нужно {(plan['upload_bytes'] + FREE_MARGIN) / 1e9:.1f} ГБ")
    if plan["identical"] and not args.force:
        say(cfg, "Сервер уже содержит эту сборку — выкладывать нечего. Проверка: verify; переключить заново: deploy --force.")
        return 0
    stage, backup, failed = f".player-stage-{stamp}", f".player-backup-{stamp}", f".player-failed-{stamp}"
    expected = expected_files(local["files"], cfg["aliases"])
    journal(status="planned", site=str(site), books=local["books"], files=expected,
            plan={k: plan[k] for k in ("changed", "upload", "upload_bytes", "removed")},
            first_install=not info["live_exists"], home_before=home_before, stage=stage)
    say(cfg, f"Выкладка {stamp}: загрузить {len(plan['upload'])} файлов ({plan['upload_bytes'] / 1e6:.1f} МБ), "
             f"взять с сервера {len(plan['links'])}")
    def abort(error: BaseException):
        try:
            server.call("prune", delete=[stage])
            removed = True
        except BaseException:
            removed = False
        journal(status="aborted_before_switch", error=str(error)[:800], stage_removed=removed)
        return DeployError(f"выкладка прервана до переключения, опубликованный плеер не тронут: {error}")

    try:
        server.call("stage", stage=stage, links=plan["links"])
        server.upload(stage, site, plan["upload"], {r: local["files"][r]["bytes"] for r in plan["upload"]})
        journal(status="uploaded")
    except BaseException as e:
        raise abort(e) from e
    try:
        act = server.call("activate", stage=stage, backup=backup, expected=local["files"], aliases=cfg["aliases"],
                          live_fingerprint=info["fingerprint"])
    except RemoteFailure as e:
        if "SWITCHED:" not in str(e):  # сервер отказался до переключения
            raise abort(e) from e
        journal(status="switched_with_error", error=str(e)[:800])
        raise DeployError(f"{e}. Выполните status и verify") from e
    except BaseException as e:  # обрыв связи: сервер мог успеть переключиться
        journal(status="unknown_after_activate_call", error=str(e)[:800])
        raise DeployError(f"исход переключения неизвестен: {e}. Выполните status и verify, ничего не повторяя вслепую") from e
    journal(status="activated_pending_https_check", activation=act)
    say(cfg, f"Переключено: сверено {act['verified_files']} файлов; прежняя версия — {act['backup'] or 'не было'}")
    try:
        manifest = server.call("manifest")
        diff = check_manifest(manifest, expected)
        if diff:
            raise DeployError("на сервере не то содержимое: " + ", ".join(diff[:10]))
        passed, bad = https_checks(cfg, manifest, home_before)
        if bad:
            raise DeployError("HTTPS-проверка не прошла:\n  " + "\n  ".join(bad[:20]))
    except BaseException as e:
        try:
            result = server.call("swap_back", backup=act["backup"], failed=failed)
        except BaseException as e2:
            journal(status="check_failed_and_swap_back_failed", error=str(e)[:2000], swap_back_error=str(e2)[:800])
            raise DeployError(f"проверка не прошла ({e}), и откат не удался ({e2}): выполните status, "
                              f"затем rollback --backup {act['backup']}") from e
        journal(status="rolled_back_after_failed_check", error=str(e)[:2000], swap_back=result)
        say(cfg, f"Проверка не прошла — новая версия снята в {failed}, "
                 f"{'прежняя возвращена' if act['backup'] else 'плеер снова отсутствует'}.")
        if act["backup"]:
            restored = server.call("manifest")
            passed2, bad2 = https_checks(cfg, restored, home_before)
            journal(restored_checks={"passed": len(passed2), "failed": bad2})
            say(cfg, f"Прежняя версия: HTTPS-проверок {len(passed2)} прошло, {len(bad2)} нет")
        raise
    journal(status="deployed_verified", https_checks=len(passed), backup=act["backup"])
    say(cfg, f"Готово: опубликовано и проверено ({len(passed)} HTTPS-проверок), главная страница сайта не изменилась.")
    say(cfg, f"Журнал: {journal.path}")
    return 0


def cmd_verify(cfg: dict, args) -> int:
    manifest = Server(cfg).call("manifest")
    if args.site:
        local = scan_site(Path(args.site), cfg["aliases"])
        diff = check_manifest(manifest, expected_files(local["files"], cfg["aliases"]))
        say(cfg, "Сервер совпадает со сборкой." if not diff else "Отличается от сборки: " + ", ".join(diff))
    passed, failed = https_checks(cfg, manifest)
    say(cfg, f"HTTPS-проверок: {len(passed)} прошло, {len(failed)} не прошло")
    for line in failed:
        say(cfg, "  ✗ " + line)
    return 1 if failed else 0


def cmd_status(cfg: dict, args) -> int:
    info = Server(cfg).call("inspect")
    files = info["files"]
    unique = {r["sha256"]: r["bytes"] for r in files.values()}  # псевдоним — та же жёсткая ссылка
    say(cfg, f"Сервер {cfg['ssh_host']}: плеер {'опубликован' if info['live_exists'] else 'НЕ опубликован'}, "
             f"файлов {len(files)}, {sum(unique.values()) / 1e9:.2f} ГБ; "
             f"свободно {info['free_bytes'] / 1e9:.1f} ГБ")
    st = info["nginx"]
    say(cfg, "nginx: блок плеера " + ("совпадает с шаблоном" if st["block_matches"] else
                                      "есть, но отличается" if st["block_present"] else "ОТСУТСТВУЕТ")
        + ("; контейнер видит этот же файл" if st["container_sees_same_file"] else "; контейнер видит ДРУГОЙ файл"))
    for line in describe_nginx(st):
        say(cfg, "  " + line)
    say(cfg, "Служебные каталоги:" if info["service_dirs"] else "Резервных копий нет.")
    for line in service_summary(info["service_dirs"]):
        say(cfg, line)
    return 0


def cmd_rollback(cfg: dict, args) -> int:
    server = Server(cfg)
    rows = [r for r in server.call("backups")["service_dirs"] if r["kind"] == "backup"]
    if not rows:
        raise DeployError("резервных копий нет")
    name = args.backup or rows[0]["name"]
    if name not in {r["name"] for r in rows}:
        raise DeployError(f"нет резервной копии {name}")
    stamp = utc_stamp()
    journal = Journal(cfg, "rollback", stamp)
    home_before = sha(fetch(f"https://{cfg['public_host']}/")[2])
    result = server.call("rollback", backup=name, new_backup=f".player-backup-{stamp}-replaced")
    journal(status="switched", result=result)
    passed, failed = https_checks(cfg, server.call("manifest"), home_before)
    journal(status="rolled_back_verified" if not failed else "rolled_back_check_failed",
            https_checks=len(passed), failed=failed)
    say(cfg, f"Возвращена {name}; снятая версия сохранена как {result['replaced_saved_as']} "
             f"(повторный rollback вернёт её). HTTPS-проверок {len(passed)} прошло, {len(failed)} нет.")
    for line in failed:
        say(cfg, "  ✗ " + line)
    return 1 if failed else 0


def cmd_nginx(cfg: dict, args) -> int:
    server = Server(cfg)
    if not args.apply:
        st = server.call("nginx_status")["nginx"]
        issues = describe_nginx(st)
        say(cfg, "nginx: всё в порядке" if not issues else "nginx: " + "; ".join(issues))
        return 1 if issues else 0
    stamp = utc_stamp()
    journal = Journal(cfg, "nginx", stamp)
    result = server.call("nginx_apply", stamp=stamp)
    journal(status="changed" if result["changed"] else "unchanged", result=result)
    if not result["changed"]:
        say(cfg, "Блок плеера уже совпадает с шаблоном — nginx.conf не менялся.")
        return 0
    say(cfg, f"Блок плеера {'обновлён' if result['replaced'] else 'добавлен'} в nginx.conf на месте, "
             f"nginx -t прошёл, nginx перезагружен; прежний файл — {result['backup']}")
    try:
        manifest = server.call("manifest")
    except RemoteFailure:
        say(cfg, "Плеер на сервере ещё не выложен — выполните deploy.")
        return 0
    passed, failed = https_checks(cfg, manifest)
    journal(https_checks=len(passed), failed=failed)
    say(cfg, f"HTTPS-проверок: {len(passed)} прошло, {len(failed)} не прошло")
    for line in failed:
        say(cfg, "  ✗ " + line)
    return 1 if failed else 0


def cmd_prune(cfg: dict, args) -> int:
    server = Server(cfg)
    rows = server.call("backups")["service_dirs"]
    backups = [r for r in rows if r["kind"] == "backup"]
    doomed = [r for r in backups[args.keep:]] + [r for r in rows if r["kind"] in ("failed", "stage")]
    if not doomed:
        say(cfg, f"Удалять нечего: резервных копий {len(backups)}, оставить {args.keep}.")
        return 0
    say(cfg, "Будут удалены безвозвратно:" if args.yes else "Будут удалены (запустите с --yes):")
    for line in service_summary(doomed):
        say(cfg, line)
    if not args.yes:
        return 0
    result = server.call("prune", delete=[r["name"] for r in doomed])
    Journal(cfg, "prune", utc_stamp())(status="deleted", deleted=result["deleted"])
    say(cfg, f"Удалено: {len(result['deleted'])}; свободно {result['free_bytes'] / 1e9:.1f} ГБ")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="файл настроек (по умолчанию player/deploy.local.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="что изменится; ничего не меняет")
    p.add_argument("--site")
    p = sub.add_parser("deploy", help="выложить сборку")
    p.add_argument("--site")
    p.add_argument("--force", action="store_true", help="переключить даже идентичную сборку")
    p = sub.add_parser("verify", help="HTTPS-проверка опубликованного плеера")
    p.add_argument("--site", help="заодно сверить сервер с этой сборкой")
    sub.add_parser("status", help="состояние сервера")
    p = sub.add_parser("rollback", help="вернуть предыдущую версию")
    p.add_argument("--backup", help="имя резервной копии (по умолчанию последняя)")
    p = sub.add_parser("nginx", help="блок плеера в nginx.conf")
    p.add_argument("--apply", action="store_true", help="записать/обновить блок (на месте, с копией и nginx -t)")
    p = sub.add_parser("prune", help="удалить старые резервные копии")
    p.add_argument("--keep", type=int, default=2)
    p.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(Path(args.config))
    try:
        return {"plan": cmd_plan, "deploy": cmd_deploy, "verify": cmd_verify, "status": cmd_status,
                "rollback": cmd_rollback, "nginx": cmd_nginx, "prune": cmd_prune}[args.cmd](cfg, args)
    except DeployError as e:
        say(cfg, f"Ошибка: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
