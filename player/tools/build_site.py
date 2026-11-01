"""Собрать папку для выкладки на сервер: плеер (public/) + библиотека (library.json, books/).

    python -X utf8 player/tools/build_site.py --library <папка библиотеки> --out <папка сайта>

Аудио по возможности кладётся жёсткой ссылкой (место на диске не удваивается).
Папку --out целиком загружают в каталог плеера на сервере (например, в /listen/<секрет>/).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

PLAYER = Path(__file__).resolve().parents[1]


def place(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if dst.stat().st_size == src.stat().st_size and dst.stat().st_mtime >= src.stat().st_mtime:
            return
        dst.unlink()
    if src.suffix.lower() in {".m4b", ".m4a", ".mp3"}:
        try:
            os.link(src, dst)
            return
        except OSError:
            pass
    shutil.copy2(src, dst)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    library, out = Path(args.library).resolve(), Path(args.out).resolve()
    catalog = json.loads((library / "library.json").read_text(encoding="utf-8"))
    count = 0
    for src in (PLAYER / "public").rglob("*"):
        if src.is_file():
            place(src, out / src.relative_to(PLAYER / "public")); count += 1
    place(library / "library.json", out / "library.json"); count += 1
    for book in catalog["books"]:
        folder = library / "books" / book["id"]
        for src in folder.iterdir():
            if src.is_file() and not src.name.endswith(".tmp"):
                place(src, out / "books" / book["id"] / src.name); count += 1
    for path in out.rglob("*"):
        try:
            os.chmod(path, 0o755 if path.is_dir() else 0o644)
        except OSError:
            pass
    print(f"Готово: {out} (файлов: {count}, книг: {len(catalog['books'])})")


if __name__ == "__main__":
    main()
