"""Серверная часть deploy_site.py. Выполняется на сервере как `python3 -`, только стандартная библиотека.

deploy_site.py отправляет этот файл целиком и дописывает в конец вызов run(<аргументы>);
результат — одна строка JSON. Локально модуль импортируют тесты.

Раскладка на сервере (пути приходят в аргументах и проверяются здесь же):
    <landing>/html/listen/<секрет>/        опубликованный плеер (live)
    <landing>/.player-stage-<время>/       новая версия до переключения
    <landing>/.player-backup-<время>/      предыдущие версии (жёсткие ссылки: неизменённое аудио место не занимает)
    <landing>/.player-failed-<время>/      версия, не прошедшая проверку и снятая автоматически
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

AUDIO_EXT = (".m4b", ".m4a", ".mp3")
RANGE = 65536
SERVICE_DIR = re.compile(r"^\.player-(stage|backup|failed)-\d{8}-\d{6}(-[a-z0-9-]+)?$")


class RemoteError(Exception):
    pass


# ---------- файлы ----------

def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def walk_files(root):
    """Все обычные файлы под root (относительный путь → Path). Символические ссылки запрещены."""
    root = Path(root)
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                raise RemoteError(f"символическая ссылка в каталоге плеера: {p.relative_to(root).as_posix()}")
        for name in filenames:
            p = Path(dirpath) / name
            out[p.relative_to(root).as_posix()] = p
    return dict(sorted(out.items()))


def scan(root, hashes=True):
    """{путь: {bytes, ino, sha256}}; файл с несколькими ссылками хешируется один раз."""
    by_ino, out = {}, {}
    for rel, p in walk_files(root).items():
        st = p.stat()
        row = {"bytes": st.st_size, "ino": st.st_ino}
        if hashes:
            if st.st_ino not in by_ino:
                by_ino[st.st_ino] = file_sha(p)
            row["sha256"] = by_ino[st.st_ino]
        out[rel] = row
    return out


def fingerprint(root):
    """Дешёвый отпечаток каталога: состав, размеры, время изменения, inode."""
    h = hashlib.sha256()
    for rel, p in walk_files(root).items():
        st = p.stat()
        h.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\0{st.st_ino}\n".encode())
    return h.hexdigest()


def range_hashes(path, size):
    out = []
    with open(path, "rb") as f:
        for start in sorted({0, size // 2, max(size - RANGE, 0)}):
            end = min(start + RANGE, size) - 1
            if end < start:
                continue
            f.seek(start)
            out.append({"start": start, "end": end, "sha256": hashlib.sha256(f.read(end - start + 1)).hexdigest()})
    return out


def chmod_tree(root):
    root = Path(root)
    os.chmod(root, 0o755)
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            os.chmod(Path(dirpath) / d, 0o755)
        for f in filenames:
            os.chmod(Path(dirpath) / f, 0o644)


def exchange(a, b):
    """Атомарно поменять местами два каталога (renameat2 RENAME_EXCHANGE, Linux)."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.renameat2
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    if fn(-100, os.fsencode(str(a)), -100, os.fsencode(str(b)), 2) != 0:
        err = ctypes.get_errno()
        raise RemoteError(f"атомарный обмен {a} ↔ {b} не удался: {os.strerror(err)}")


# ---------- проверка путей ----------

def paths(a):
    landing = Path(a["landing"])
    html = landing / "html"
    prefix = a["prefix"]
    if not re.fullmatch(r"/listen/[A-Za-z0-9_-]{24,}/", prefix):
        raise RemoteError("prefix должен быть /listen/<секрет не короче 24 символов>/")
    live = html / prefix.strip("/")
    return landing, html, live


def service_dir(landing, name, kind=None):
    if not SERVICE_DIR.match(name) or (kind and not name.startswith(f".player-{kind}-")):
        raise RemoteError(f"недопустимое имя служебного каталога: {name}")
    return Path(landing) / name


def list_service(landing, live_inodes):
    rows = []
    for p in sorted(Path(landing).iterdir(), key=lambda x: x.name, reverse=True):
        if not (p.is_dir() and not p.is_symlink() and SERVICE_DIR.match(p.name)):
            continue
        files = scan(p, hashes=False)
        own, seen = 0, set()
        for row in files.values():
            if row["ino"] not in live_inodes and row["ino"] not in seen:
                own += row["bytes"]
            seen.add(row["ino"])
        books = []
        try:
            lib = json.loads((p / "library.json").read_text(encoding="utf-8"))
            books = [{"id": b.get("id"), "version": b.get("version")} for b in lib.get("books", [])]
        except (OSError, ValueError):
            pass
        rows.append({"name": p.name, "kind": p.name.split("-")[1], "files": len(files),
                     "bytes": sum(r["bytes"] for r in files.values()), "own_bytes": own, "books": books})
    return rows


# ---------- nginx ----------

def strip_conf(text):
    return " ".join(re.sub(r"#[^\n]*", "", text).split())


def find_block(text, marker, prefix):
    """Участок блока плеера: от строки-маркера до закрывающей скобки location ^~ <prefix>."""
    start = text.find(marker)
    if start < 0:
        return None
    start = text.rfind("\n", 0, start) + 1
    head = text.find(f"location ^~ {prefix}", start)
    if head < 0:
        return None
    depth, i = 0, text.index("{", head)
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = text.find("\n", i)
                return start, (len(text) if end < 0 else end + 1)
        i += 1
    return None


def insert_block(text, block):
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip() == "absolute_redirect off;":
            return "".join(lines[:i + 1] + ["\n", block] + lines[i + 1:])
    for i, line in enumerate(lines):
        if re.match(r"\s*location\s+/\s*\{", line):
            return "".join(lines[:i] + [block, "\n"] + lines[i:])
    raise RemoteError("в nginx.conf не найдено место для блока плеера (нет absolute_redirect off; и location /)")


def docker(*cmd, timeout=60):
    try:
        r = subprocess.run(["docker", *cmd], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None
    return r


def nginx_status(a):
    landing, _, _ = paths(a)
    conf = landing / "nginx.conf"
    text = conf.read_text(encoding="utf-8")
    where = find_block(text, a["marker"], a["prefix"])
    current = text[where[0]:where[1]] if where else None
    host_sha = file_sha(conf)
    running = docker("inspect", "-f", "{{.State.Running}}", a["container"])
    inside = docker("exec", a["container"], "sha256sum", "/etc/nginx/conf.d/default.conf")
    inside_sha = inside.stdout.split()[0] if inside is not None and inside.returncode == 0 and inside.stdout else None
    return {"conf_sha256": host_sha, "marker_present": a["marker"] in text,
            "block_present": current is not None,
            "block_matches": current is not None and strip_conf(current) == strip_conf(a["block"]),
            "container_running": running is not None and running.returncode == 0 and running.stdout.strip() == "true",
            "container_conf_sha256": inside_sha, "container_sees_same_file": inside_sha == host_sha}


def op_nginx_status(a):
    return {"nginx": nginx_status(a)}


def op_nginx_apply(a):
    landing, _, _ = paths(a)
    conf = landing / "nginx.conf"
    st = nginx_status(a)
    if st["block_matches"]:
        return {"changed": False, "nginx": st}
    if not st["container_sees_same_file"]:
        raise RemoteError("контейнер читает другой nginx.conf (файл заменили, а не правили на месте): "
                          f"нужен docker restart {a['container']}, затем повторить")
    old = conf.read_bytes()
    text = old.decode("utf-8")
    where = find_block(text, a["marker"], a["prefix"])
    if where:
        new = (text[:where[0]] + a["block"] + text[where[1]:]).encode("utf-8")
    elif st["marker_present"]:
        raise RemoteError("в nginx.conf есть маркер плеера, но не найден его location: исправьте вручную (на месте)")
    else:
        new = insert_block(text, a["block"]).encode("utf-8")
    backup = landing / f"nginx.conf.before-player-{a['stamp']}"
    if backup.exists():
        raise RemoteError(f"резервная копия уже существует: {backup}")
    backup.write_bytes(old)
    os.chmod(backup, 0o600)

    def write_in_place(data):
        # Файл смонтирован в контейнер отдельно: замена переименованием оставила бы контейнеру старый inode.
        with open(conf, "r+b") as f:
            f.seek(0)
            f.write(data)
            f.truncate()
            f.flush()
            os.fsync(f.fileno())

    write_in_place(new)
    test = docker("exec", a["container"], "nginx", "-t")
    if test is None or test.returncode != 0:
        write_in_place(old)
        raise RemoteError("nginx -t не прошёл, nginx.conf возвращён: " + ((test.stderr if test else "") or "")[-800:])
    reload = docker("exec", a["container"], "nginx", "-s", "reload")
    if reload is None or reload.returncode != 0:
        raise RemoteError("nginx -s reload не прошёл: " + ((reload.stderr if reload else "") or "")[-800:])
    return {"changed": True, "replaced": bool(where), "backup": backup.name,
            "sha_before": hashlib.sha256(old).hexdigest(), "nginx": nginx_status(a)}


# ---------- плеер ----------

def op_inspect(a):
    landing, html, live = paths(a)
    if not html.is_dir():
        raise RemoteError(f"нет каталога сайта {html}")
    out = {"live_exists": live.is_dir(), "free_bytes": shutil.disk_usage(landing).free,
           "same_fs": os.stat(landing).st_dev == os.stat(html).st_dev}
    files = scan(live) if live.is_dir() else {}
    out["files"] = {k: {"bytes": v["bytes"], "sha256": v["sha256"]} for k, v in files.items()}
    out["fingerprint"] = fingerprint(live) if live.is_dir() else None
    out["service_dirs"] = list_service(landing, {v["ino"] for v in files.values()})
    out["nginx"] = nginx_status(a)
    return out


def op_stage(a):
    landing, _, live = paths(a)
    stage = service_dir(landing, a["stage"], "stage")
    stage.mkdir(mode=0o755)
    for rel, src in a["links"]:
        dst = stage / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.link(live / src, dst)
    return {"stage": stage.name, "linked": len(a["links"])}


def op_activate(a):
    landing, html, live = paths(a)
    stage = service_dir(landing, a["stage"], "stage")
    backup = service_dir(landing, a["backup"], "backup")
    for alias, target in a["aliases"].items():
        dst = stage / alias
        if dst.exists():
            raise RemoteError(f"псевдоним {alias} совпадает с файлом сборки")
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.link(stage / target, dst)
    want = dict(a["expected"])
    for alias, target in a["aliases"].items():
        want[alias] = want[target]
    have = scan(stage)
    extra, missing = sorted(set(have) - set(want)), sorted(set(want) - set(have))
    bad = sorted(k for k in set(have) & set(want)
                 if have[k]["sha256"] != want[k]["sha256"] or have[k]["bytes"] != want[k]["bytes"])
    if extra or missing or bad:
        raise RemoteError(json.dumps({"stage_mismatch": {"extra": extra, "missing": missing, "bad": bad}},
                                     ensure_ascii=False))
    chmod_tree(stage)
    if backup.exists():
        raise RemoteError(f"{backup.name} уже существует: ничего не переключено")
    if live.is_dir():
        if fingerprint(live) != a["live_fingerprint"]:
            raise RemoteError("опубликованный плеер изменился после проверки (параллельная выкладка?): ничего не переключено")
        exchange(stage, live)
        try:
            os.rename(stage, backup)
        except OSError as e:
            raise RemoteError(f"SWITCHED: новая версия опубликована, но прежняя осталась в {stage.name}: {e}")
        previous = backup.name
    else:
        if a["live_fingerprint"] is not None:
            raise RemoteError("опубликованный плеер исчез после проверки: ничего не переключено")
        live.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        os.rename(stage, live)
        previous = None
    return {"verified_files": len(have), "backup": previous}


def op_swap_back(a):
    """Снять не прошедшую проверку версию: вернуть backup на место, новую версию отложить в failed."""
    landing, _, live = paths(a)
    failed = service_dir(landing, a["failed"], "failed")
    if a.get("backup"):
        backup = service_dir(landing, a["backup"], "backup")
        exchange(backup, live)
        os.rename(backup, failed)
    else:
        os.rename(live, failed)
    return {"restored": bool(a.get("backup")), "failed": failed.name}


def op_rollback(a):
    """Поменять местами опубликованную версию и резервную копию; снятая версия становится новой копией."""
    landing, _, live = paths(a)
    backup = service_dir(landing, a["backup"], "backup")
    target = service_dir(landing, a["new_backup"], "backup")
    if not (backup.is_dir() and live.is_dir()):
        raise RemoteError("нет резервной копии или опубликованного плеера")
    if not (backup / "index.html").is_file():
        raise RemoteError(f"{backup.name} не похожа на сборку плеера (нет index.html)")
    exchange(backup, live)
    os.rename(backup, target)
    return {"restored": a["backup"], "replaced_saved_as": target.name}


def op_manifest(a):
    _, _, live = paths(a)
    if not live.is_dir():
        raise RemoteError("плеер не опубликован")
    files = scan(live)
    for rel, row in files.items():
        if rel.lower().endswith(AUDIO_EXT):
            row["ranges"] = range_hashes(live / rel, row["bytes"])
        del row["ino"]
    dirs = sorted({p.as_posix() for rel in files for p in Path(rel).parents} - {"."})
    return {"files": files, "dirs": dirs}


def op_backups(a):
    landing, _, live = paths(a)
    inodes = {v["ino"] for v in scan(live, hashes=False).values()} if live.is_dir() else set()
    return {"service_dirs": list_service(landing, inodes)}


def op_prune(a):
    landing, _, live = paths(a)
    deleted = []
    for name in a["delete"]:
        p = service_dir(landing, name)
        if p.resolve() == live.resolve() or p.is_symlink():
            raise RemoteError(f"{name}: отказ")
        shutil.rmtree(p)
        deleted.append(name)
    return {"deleted": deleted, "free_bytes": shutil.disk_usage(landing).free}


OPS = {name[3:]: fn for name, fn in globals().items() if name.startswith("op_")}


def run(args):
    try:
        result = OPS[args["op"]](args)
        result["ok"] = True
    except (RemoteError, OSError, KeyError, ValueError) as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(result, ensure_ascii=False))
    return result
