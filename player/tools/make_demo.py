"""Демо-библиотека для проверки плеера без настоящих книг: синтетический звук, нарисованные обложки.

    python -X utf8 player/tools/make_demo.py [папка]      (по умолчанию player/library)

Нужен ffmpeg; Pillow необязателен (без него книги будут без обложек).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from vox.publish import publish  # noqa: E402

BOOKS = [
    {"id": "demo-tales", "title": "Сказки дальней дороги", "author": "Демо-автор", "color": (190, 120, 30),
     "chapters": ["Пролог", "Глава 1. Перекрёсток", "Глава 2. Ночная трасса", "Глава 3. Маяк", "Эпилог"], "seconds": 75},
    {"id": "demo-long", "title": "Очень длинное название тестовой книги, чтобы проверить переносы",
     "author": "Иван Тестов", "color": (40, 110, 160), "chapters": [f"Глава {i}" for i in range(1, 31)], "seconds": 20},
    {"id": "demo-nocover", "title": "Книга без обложки", "author": "", "color": None,
     "chapters": ["Часть первая", "Часть вторая"], "seconds": 40},
]


def make_audio(path: Path, chapters: int, seconds: int) -> None:
    # Разные тоны по главам, чтобы на слух было понятно, где граница.
    parts = []
    for i in range(chapters):
        parts += ["-f", "lavfi", "-t", str(seconds), "-i", f"sine=frequency={220 + 40 * (i % 8)}:sample_rate=24000"]
    concat = "".join(f"[{i}:a]" for i in range(chapters)) + f"concat=n={chapters}:v=0:a=1,volume=0.2[a]"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *parts, "-filter_complex", concat, "-map", "[a]",
                    "-c:a", "aac", "-b:a", "32k", "-ac", "1", "-movflags", "+faststart", str(path)], check=True)


def make_cover(path: Path, title: str, author: str, color) -> bool:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    img = Image.new("RGB", (600, 900), color)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 54)
        small = ImageFont.truetype("DejaVuSans.ttf", 36)
    except OSError:
        font = small = ImageFont.load_default()
    words, lines, line = title.split(), [], ""
    for word in words:
        if len(line + " " + word) > 16:
            lines.append(line.strip()); line = word
        else:
            line += " " + word
    lines.append(line.strip())
    y = 120
    for text in lines[:6]:
        draw.text((50, y), text, fill="white", font=font); y += 70
    draw.text((50, 800), author, fill="white", font=small)
    img.save(path, quality=85)
    return True


def main() -> None:
    library = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "player" / "library"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for book in BOOKS:
            audio = tmp / f"{book['id']}.m4b"
            make_audio(audio, len(book["chapters"]), book["seconds"])
            n, sec = len(book["chapters"]), book["seconds"]
            chapters = [{"title": t, "start": i * sec, "end": (i + 1) * sec} for i, t in enumerate(book["chapters"])]
            cover = tmp / f"{book['id']}.jpg"
            has_cover = book["color"] and make_cover(cover, book["title"], book["author"], book["color"])
            images, cues = [], []
            if has_cover and book["id"] == "demo-tales":
                card = tmp / "map.jpg"
                make_cover(card, "Карта дороги", "иллюстрация", (30, 90, 60))
                images, cues = [f"map={card}|Карта дороги"], ["map@1"]
            publish(library, audio=audio, chapters=chapters, book_id=book["id"], title=book["title"],
                    author=book["author"], cover=cover if has_cover else None, images=images, cues=cues,
                    version_label="demo", log=lambda *_: None)
            print(f"{book['title']}: {n} глав")
    print(f"Демо-библиотека: {library}\nЗапуск: node player/server.mjs --library \"{library}\"")


if __name__ == "__main__":
    main()
