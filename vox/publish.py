"""Публикация готовой книги в библиотеку плеера (player/).

Библиотека — папка с library.json и books/<id>/ (book.json, аудио, обложка, иллюстрации).
Плеер читает library.json при каждом открытии, поэтому новая книга появляется
без пересборки плеера: достаточно выложить обновлённую папку на сервер.

    python vox.py --book projects/<книга> publish --library ../library [--cover cover.jpg]
    python vox.py publish --library ../library --m4b book.m4b --chapters chapters.ffmeta \
        --id warbreaker-ru --title "Сокрушитель Войн" --author "Брендон Сандерсон"
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path

from .util import HarnessError, now_utc, read_json, write_json

CHUNK_SIZE = 4 * 1024 * 1024
SCHEMA = 2
AUDIO_TYPES = {".m4b": "audio/mp4", ".m4a": "audio/mp4", ".mp3": "audio/mpeg"}
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def slug(text: str) -> str:
    table = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
        "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
        "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya"})
    value = re.sub(r"[^a-z0-9]+", "-", text.lower().translate(table)).strip("-")
    return value[:64] or "book"


def read_ffmeta(path: Path) -> list[dict]:
    """Главы из ffmetadata (как пишет vox master и старый конвейер)."""
    chapters = []
    for block in Path(path).read_text(encoding="utf-8").split("[CHAPTER]")[1:]:
        fields = {}
        for line in block.strip().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key.strip()] = value
        num, den = (int(x) for x in fields.get("TIMEBASE", "1/1000").split("/"))
        title = re.sub(r"\\(.)", r"\1", fields.get("title", "")).strip()
        chapters.append({"title": title, "start": int(fields["START"]) * num / den,
                         "end": int(fields["END"]) * num / den})
    return chapters


def load_chapters(path: Path) -> list[dict]:
    path = Path(path)
    if path.suffix == ".json":
        data = read_json(path)
        rows = data["chapters"] if isinstance(data, dict) else data
        return [{"title": r["title"], "start": float(r["start"]), "end": float(r["end"])} for r in rows]
    return read_ffmeta(path)


def normalize_chapters(chapters: list[dict]) -> list[dict]:
    if not chapters:
        raise HarnessError("В книге нет глав")
    out = []
    for index, ch in enumerate(chapters):
        start, end = round(float(ch["start"]), 3), round(float(ch["end"]), 3)
        if end <= start:
            raise HarnessError(f"Глава {index + 1}: конец раньше начала")
        if out and abs(out[-1]["end"] - start) > 0.05:
            raise HarnessError(f"Глава {index + 1}: разрыв или наложение с предыдущей ({out[-1]['end']} → {start})")
        if out:
            start = out[-1]["end"]
        out.append({"id": f"{index:02}", "title": ch["title"] or f"Раздел {index + 1}", "start": start, "end": end})
    if out[0]["start"] > 0.05:
        raise HarnessError("Первая глава начинается не с нуля")
    out[0]["start"] = 0
    return out


def chunk_hashes(path: Path, chunk_size: int = CHUNK_SIZE) -> tuple[str, int, list[dict]]:
    whole, chunks, size = hashlib.sha256(), [], 0
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(chunk_size), b""):
            whole.update(block)
            size += len(block)
            chunks.append({"size": len(block), "sha256": hashlib.sha256(block).hexdigest()})
    return whole.hexdigest(), size, chunks


def _place(src: Path, dst: Path, link: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size == src.stat().st_size and dst.resolve() == src.resolve():
        return
    tmp = dst.with_name(dst.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    if link:
        try:
            os.link(src, tmp)
        except OSError:
            shutil.copy2(src, tmp)
    else:
        shutil.copy2(src, tmp)
    os.replace(tmp, dst)
    _readable(dst)


def _readable(path: Path) -> None:
    """Атомарная запись создаёт файл с правами 0600; веб-серверу (nginx) нужно чтение для всех."""
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


def parse_image(spec: str) -> tuple[str, Path, str]:
    """key=путь[|Подпись] → (key, path, title)."""
    if "=" not in spec:
        raise HarnessError(f"--image {spec}: нужен формат ключ=путь|Подпись")
    key, rest = spec.split("=", 1)
    path, _, title = rest.partition("|")
    return key.strip(), Path(path.strip()), title.strip() or key.strip()


def parse_cue(spec: str, chapters: list[dict]) -> dict:
    """key@глава[-глава] (номер с 1, отрицательный — с конца) или key@начало-конец в секундах (с точкой)."""
    if "@" not in spec:
        raise HarnessError(f"--cue {spec}: нужен формат ключ@глава или ключ@сек-сек")
    key, where = spec.split("@", 1)
    if re.fullmatch(r"\d+(\.\d+)?-\d+(\.\d+)?", where) and "." in where:
        start, end = (float(x) for x in where.split("-"))
        return {"image": key, "start": start, "end": end}
    parts = re.fullmatch(r"(-?\d+)(?:\.\.(-?\d+))?", where)
    if not parts:
        raise HarnessError(f"--cue {spec}: не понял диапазон")

    def chapter(n: int) -> dict:
        index = n - 1 if n > 0 else len(chapters) + n
        if not 0 <= index < len(chapters):
            raise HarnessError(f"--cue {spec}: нет главы {n}")
        return chapters[index]
    first = chapter(int(parts[1]))
    last = chapter(int(parts[2])) if parts[2] else first
    return {"image": key, "start": first["start"], "end": last["end"]}


def publish(library: Path, *, audio: Path, chapters: list[dict], book_id: str, title: str, author: str,
            cover: Path | None = None, images: list[str] = (), cues: list[str] = (), description: str = "",
            narrator: str = "", version_label: str = "", status: str = "", link: bool = False,
            log=print) -> dict:
    library, audio = Path(library), Path(audio)
    if not ID_RE.match(book_id):
        raise HarnessError(f"id книги «{book_id}»: только латиница, цифры, - и _")
    if not audio.exists():
        raise HarnessError(f"Нет аудиофайла {audio}")
    ext = audio.suffix.lower()
    if ext not in AUDIO_TYPES:
        raise HarnessError(f"Формат {ext} не поддерживается плеером (нужен .m4b, .m4a или .mp3)")
    chapters = normalize_chapters(chapters)
    folder = library / "books" / book_id
    log(f"Считаю хеши {audio.name}…")
    sha, size, chunks = chunk_hashes(audio)
    audio_name = "book" + ext
    _place(audio, folder / audio_name, link)

    image_rows = {}
    if cover:
        cover = Path(cover)
        if cover.suffix.lower() not in IMAGE_TYPES:
            raise HarnessError(f"Обложка {cover.name}: нужен jpg, png или webp")
        _place(cover, folder / ("cover" + cover.suffix.lower()), False)
        image_rows["cover"] = {"src": "cover" + cover.suffix.lower(), "title": "Обложка",
                               "alt": f"Обложка книги «{title}»"}
    for spec in images:
        key, path, caption = parse_image(spec)
        if not re.fullmatch(r"[a-z0-9_-]+", key) or key == "cover":
            raise HarnessError(f"--image {spec}: ключ латиницей и не cover")
        if path.suffix.lower() not in IMAGE_TYPES:
            raise HarnessError(f"--image {spec}: нужен jpg, png или webp")
        _place(path, folder / (key + path.suffix.lower()), False)
        image_rows[key] = {"src": key + path.suffix.lower(), "title": caption, "alt": caption}
    cue_rows = [parse_cue(spec, chapters) for spec in cues]
    for cue in cue_rows:
        if cue["image"] not in image_rows:
            raise HarnessError(f"--cue: нет иллюстрации {cue['image']}")

    duration = chapters[-1]["end"]
    version = f"{version_label or 'v'}-{sha[:12]}"
    book = {
        "schema": SCHEMA, "id": book_id, "version": version, "title": title, "author": author,
        "narrator": narrator, "description": description, "duration": duration,
        "status": status, "published": now_utc(),
        "audio": {"url": f"{audio_name}?v={sha[:12]}", "type": AUDIO_TYPES[ext], "size": size, "sha256": sha,
                  "chunkSize": CHUNK_SIZE, "chunks": chunks},
        "cover": image_rows.get("cover", {}).get("src", ""),
        "chapters": chapters, "images": image_rows, "cues": cue_rows,
    }
    write_json(folder / "book.json", book)
    _readable(folder / "book.json")

    lib_path = library / "library.json"
    lib = read_json(lib_path) if lib_path.exists() else {"schema": SCHEMA, "title": "Между строк", "books": []}
    previous = next((b for b in lib["books"] if b["id"] == book_id), None)
    entry = {"id": book_id, "title": title, "author": author, "narrator": narrator,
             "path": f"books/{book_id}/book.json", "version": version,
             "cover": f"books/{book_id}/{book['cover']}" if book["cover"] else "",
             "duration": duration, "chapters": len(chapters), "size": size,
             "added": previous["added"] if previous else now_utc(), "updated": now_utc()}
    lib["books"] = [entry] + [b for b in lib["books"] if b["id"] != book_id]
    lib["books"].sort(key=lambda b: b.get("added", ""), reverse=True)
    lib["updated"] = now_utc()
    write_json(lib_path, lib)
    _readable(lib_path)
    log(f"Книга «{title}» опубликована: {folder} (глав: {len(chapters)}, {size / 1e6:.0f} МБ, версия {version})")
    return {"id": book_id, "version": version, "folder": str(folder), "chapters": len(chapters), "size": size,
            "updated": bool(previous)}


def publish_project(project, library: Path, **options) -> dict:
    """Публикация активного мастера проекта (после `vox master`)."""
    release = project.active_release()
    folder = project.master_dir(release)
    manifest_path = folder / "manifest.json"
    if not manifest_path.exists():
        raise HarnessError("Мастер ещё не собран: выполните master")
    manifest = read_json(manifest_path)
    book_cfg = project.config["book"]
    title = options.pop("title", None) or book_cfg.get("title") or project.root.name
    return publish(library, audio=folder / manifest["file"], chapters=manifest["chapters"],
                   book_id=options.pop("book_id", None) or slug(title), title=title,
                   author=options.pop("author", None) or book_cfg.get("author", ""),
                   version_label=options.pop("version_label", None) or release,
                   status=manifest.get("status", ""), **options)
