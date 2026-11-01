"""Публикация книги в библиотеку плеера (vox publish)."""
import hashlib
import json
import os

import pytest

from vox import cli
from vox.publish import CHUNK_SIZE, normalize_chapters, parse_cue, publish, read_ffmeta, slug
from vox.util import HarnessError


def _chapters(n=3, length=10.0):
    return [{"title": f"Глава {i + 1}", "start": i * length, "end": (i + 1) * length} for i in range(n)]


def test_publish_writes_book_and_library(tmp_path):
    audio = tmp_path / "in.m4b"
    data = os.urandom(CHUNK_SIZE + 1234)
    audio.write_bytes(data)
    cover = tmp_path / "c.jpg"; cover.write_bytes(b"jpg")
    card = tmp_path / "m.png"; card.write_bytes(b"png")
    lib = tmp_path / "library"
    result = publish(lib, audio=audio, chapters=_chapters(), book_id="test-book", title="Тест", author="Автор",
                     cover=cover, images=[f"map={card}|Карта"], cues=["map@1", "map@-1"], version_label="master-v1",
                     log=lambda *_: None)
    book = json.loads((lib / "books" / "test-book" / "book.json").read_text(encoding="utf-8"))
    sha = hashlib.sha256(data).hexdigest()
    assert result["version"] == book["version"] == f"master-v1-{sha[:12]}"
    assert book["audio"]["url"] == f"book.m4b?v={sha[:12]}" and book["audio"]["size"] == len(data)
    assert [c["size"] for c in book["audio"]["chunks"]] == [CHUNK_SIZE, 1234]
    assert book["audio"]["chunks"][1]["sha256"] == hashlib.sha256(data[CHUNK_SIZE:]).hexdigest()
    assert (lib / "books" / "test-book" / "book.m4b").read_bytes() == data
    assert book["cover"] == "cover.jpg" and book["images"]["map"]["src"] == "map.png"
    assert book["cues"] == [{"image": "map", "start": 0, "end": 10.0}, {"image": "map", "start": 20.0, "end": 30.0}]
    library = json.loads((lib / "library.json").read_text(encoding="utf-8"))
    entry = library["books"][0]
    assert entry["path"] == "books/test-book/book.json" and entry["cover"] == "books/test-book/cover.jpg"
    assert entry["chapters"] == 3 and entry["duration"] == 30.0
    if os.name != "nt":  # nginx должен читать файлы
        assert oct((lib / "library.json").stat().st_mode)[-3:] == "644"
        assert oct((lib / "books" / "test-book" / "book.json").stat().st_mode)[-3:] == "644"

    # повторная публикация заменяет запись, дата добавления сохраняется
    publish(lib, audio=audio, chapters=_chapters(), book_id="test-book", title="Тест 2", author="Автор", log=lambda *_: None)
    library2 = json.loads((lib / "library.json").read_text(encoding="utf-8"))
    assert len(library2["books"]) == 1 and library2["books"][0]["title"] == "Тест 2"
    assert library2["books"][0]["added"] == entry["added"]


def test_chapters_validation():
    assert normalize_chapters([{"title": "", "start": 0.01, "end": 5}, {"title": "Б", "start": 5.02, "end": 9}]) == [
        {"id": "00", "title": "Раздел 1", "start": 0, "end": 5.0}, {"id": "01", "title": "Б", "start": 5.0, "end": 9.0}]
    with pytest.raises(HarnessError):
        normalize_chapters([{"title": "А", "start": 0, "end": 5}, {"title": "Б", "start": 7, "end": 9}])
    with pytest.raises(HarnessError):
        normalize_chapters([])


def test_ffmeta_and_cues(tmp_path):
    meta = tmp_path / "chapters.ffmeta"
    meta.write_text(";FFMETADATA1\ntitle=Книга\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1500\ntitle=Пролог\\=1\n"
                    "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1500\nEND=4000\ntitle=Глава 1\n", encoding="utf-8")
    chapters = normalize_chapters(read_ffmeta(meta))
    assert [c["title"] for c in chapters] == ["Пролог=1", "Глава 1"] and chapters[1]["end"] == 4.0
    assert parse_cue("map@1..2", chapters) == {"image": "map", "start": 0, "end": 4.0}
    assert parse_cue("map@0.5-1.5", chapters) == {"image": "map", "start": 0.5, "end": 1.5}
    with pytest.raises(HarnessError):
        parse_cue("map@9", chapters)
    assert slug("Сокрушитель Войн") == "sokrushitel-voyn"


def test_cli_publish_standalone(tmp_path, capsys):
    audio = tmp_path / "a.mp3"; audio.write_bytes(b"x" * 100)
    meta = tmp_path / "ch.json"; meta.write_text(json.dumps({"chapters": _chapters(2)}), encoding="utf-8")
    code = cli.main(["--book", str(tmp_path / "нет"), "publish", "--library", str(tmp_path / "lib"), "--m4b", str(audio),
                     "--chapters", str(meta), "--title", "Книга", "--id", "kniga"])
    assert code == 0
    book = json.loads((tmp_path / "lib" / "books" / "kniga" / "book.json").read_text(encoding="utf-8"))
    assert book["audio"]["type"] == "audio/mpeg" and book["audio"]["url"].startswith("book.mp3?v=")
    assert cli.main(["--book", str(tmp_path / "нет"), "publish", "--library", str(tmp_path / "lib")]) == 1
