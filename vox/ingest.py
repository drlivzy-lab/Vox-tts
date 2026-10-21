"""Извлечение книги (FB2, EPUB, TXT, MD) в абзацы со стабильными ID.

Исходный файл не меняется; его SHA-256 записывается в sections.json.
Каждый абзац получает ID p000001…, раздел — s001…; правки текста
делаются потом в сценарии, а не здесь.
"""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

from .util import HarnessError, sha_file, write_json, write_jsonl, now_utc

HEADING_RE = re.compile(r"^(пролог|эпилог|глава|часть|книга|интерлюдия|приложение|"
                        r"prologue|epilogue|chapter|part|interlude)\b", re.I)
SCENE_RE = re.compile(r"^\s*([*•·~]\s*){3,}\s*$|^\s*(\*\s*){1,3}\*\s*$|^\s*#\s*$")


def clean(text: str) -> str:
    text = text.replace(" ", " ").replace("­", "").replace("﻿", "")
    return re.sub(r"\s+", " ", text).strip()


class Builder:
    def __init__(self) -> None:
        self.sections: list[dict] = []
        self.paragraphs: list[dict] = []
        self.pending_headings: list[str] = []

    def start(self, title: str = "", kind: str | None = None, audio: bool = True) -> None:
        title = clean(title)
        if kind is None:
            kind = "chapter" if HEADING_RE.match(title) else ("front" if not self.sections else "chapter")
        self.sections.append({"id": f"s{len(self.sections) + 1:03}", "title": title, "kind": kind,
                              "audio": audio, "paragraphs": []})
        for heading in self.pending_headings:
            self.add(heading, "heading")
        self.pending_headings = []

    def add(self, text: str, kind: str = "text", italic: list[str] | None = None) -> None:
        text = clean(text)
        if kind != "scene_break" and not text:
            return
        if kind == "text" and SCENE_RE.match(text):
            kind, text = "scene_break", ""
        if not self.sections:
            self.start("", "front")
        section = self.sections[-1]
        if kind == "scene_break":
            last = self.paragraphs[-1] if self.paragraphs and section["paragraphs"] else None
            if not section["paragraphs"] or (last and last["kind"] in ("scene_break", "heading")):
                return
        pid = f"p{len(self.paragraphs) + 1:06}"
        row = {"id": pid, "section": section["id"], "kind": kind, "text": text}
        italic = [clean(i) for i in (italic or []) if len(clean(i)) >= 2]
        if italic:
            row["italic"] = italic  # курсив: часто мысли, письма, иностранные слова
        self.paragraphs.append(row)
        section["paragraphs"].append(pid)

    def finish(self) -> tuple[list[dict], list[dict]]:
        for section in self.sections:
            while section["paragraphs"]:
                last = next(p for p in self.paragraphs if p["id"] == section["paragraphs"][-1])
                if last["kind"] != "scene_break":
                    break
                section["paragraphs"].pop()
                self.paragraphs.remove(last)
        keep = [s for s in self.sections if s["paragraphs"]]
        return keep, self.paragraphs


# ---------------- FB2 ----------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(node: ET.Element) -> str:
    def walk(item: ET.Element) -> str:
        if _local(item.tag) == "a" and "note" in (item.get("type") or ""):
            return ""  # номер сноски не читаем как часть текста
        inner = (item.text or "") + "".join(walk(child) + (child.tail or "") for child in item)
        return inner
    return clean(walk(node))


def _italics(node: ET.Element) -> list[str]:
    return [_text(e) for e in node.iter() if _local(e.tag) == "emphasis"]


def _fb2_block(builder: Builder, node: ET.Element) -> None:
    name = _local(node.tag)
    if name == "p":
        builder.add(_text(node), "text", _italics(node))
    elif name == "subtitle":
        builder.add(_text(node), "subtitle")
    elif name == "empty-line":
        builder.add("", "scene_break")
    elif name in ("epigraph", "cite"):
        for child in node:
            kind = _local(child.tag)
            if kind in ("p", "text-author", "subtitle"):
                builder.add(_text(child), "epigraph")
            elif kind in ("poem", "cite"):
                _fb2_block(builder, child)
    elif name == "poem":
        for stanza in node:
            if _local(stanza.tag) == "stanza":
                lines = [_text(v) for v in stanza if _local(v.tag) == "v"]
                builder.add(" / ".join(l for l in lines if l), "poem")
            elif _local(stanza.tag) in ("title", "text-author"):
                builder.add(_text(stanza), "poem")
    elif name == "table":
        builder.add(_text(node), "table")


def _fb2_section(builder: Builder, node: ET.Element, audio: bool, note: bool) -> None:
    title_node = next((c for c in node if _local(c.tag) == "title"), None)
    title = " ".join(_text(p) for p in title_node if _local(p.tag) == "p") if title_node is not None else ""
    children = [c for c in node if _local(c.tag) not in ("title", "image")]
    has_content = any(_local(c.tag) not in ("section",) for c in children)
    if has_content:
        builder.start(title, "note" if note else None, audio and not note)
        if title:
            builder.add(title, "heading")
        for child in children:
            if _local(child.tag) == "section":
                _fb2_section(builder, child, audio, note)
            else:
                _fb2_block(builder, child)
    else:
        if title:
            builder.pending_headings.append(title)
        for child in children:
            _fb2_section(builder, child, audio, note)


def read_fb2(path: Path) -> tuple[list[dict], list[dict], dict]:
    tree = ET.parse(path)
    root = tree.getroot()
    builder = Builder()
    meta = {}
    title_info = root.find(".//{*}title-info")
    if title_info is not None:
        meta["title"] = clean(title_info.findtext("{*}book-title") or "")
        author = title_info.find("{*}author")
        if author is not None:
            meta["author"] = clean(" ".join(author.findtext("{*}" + k) or "" for k in
                                            ("first-name", "middle-name", "last-name")))
    for body in root.findall("{*}body"):
        note = body.get("name") in ("notes", "comments")
        for child in body:
            if _local(child.tag) == "section":
                _fb2_section(builder, child, True, note)
    sections, paragraphs = builder.finish()
    return sections, paragraphs, meta


# ---------------- EPUB ----------------

class _Html(HTMLParser):
    BLOCK = {"p": "text", "h1": "heading", "h2": "heading", "h3": "heading", "h4": "subtitle",
             "h5": "subtitle", "h6": "subtitle", "blockquote": "epigraph", "li": "text", "div": None}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self.stack: list[str] = []
        self.buffer: list[str] = []
        self.skip = 0
        self.italic_depth = 0
        self.italic: list[str] = []
        self.current_italic: list[str] = []

    def _flush(self, kind: str) -> None:
        text = clean("".join(self.buffer))
        if text:
            self.blocks.append((kind, text, self.italic))
        self.buffer = []
        self.italic = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "sup"):
            self.skip += 1
        if tag in ("i", "em"):
            self.italic_depth += 1
            self.current_italic = []
        if tag in self.BLOCK and self.BLOCK[tag]:
            self._flush("text")
            self.stack.append(tag)
        elif tag == "br":
            self.buffer.append(" ")
        elif tag == "hr":
            self._flush("text")
            self.blocks.append(("scene_break", "", []))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "sup"):
            self.skip = max(0, self.skip - 1)
        if tag in ("i", "em") and self.italic_depth:
            self.italic_depth -= 1
            self.italic.append("".join(self.current_italic))
        if self.stack and tag == self.stack[-1]:
            self.stack.pop()
            self._flush(self.BLOCK[tag])

    def handle_data(self, data):
        if not self.skip:
            self.buffer.append(data)
            if self.italic_depth:
                self.current_italic.append(data)


def read_epub(path: Path) -> tuple[list[dict], list[dict], dict]:
    with zipfile.ZipFile(path) as archive:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        opf_path = container.find(".//{*}rootfile").get("full-path")
        opf = ET.fromstring(archive.read(opf_path))
        base = PurePosixPath(opf_path).parent
        manifest = {item.get("id"): item.get("href") for item in opf.findall(".//{*}manifest/{*}item")}
        spine = [ref.get("idref") for ref in opf.findall(".//{*}spine/{*}itemref")]
        meta = {"title": clean(opf.findtext(".//{http://purl.org/dc/elements/1.1/}title") or ""),
                "author": clean(opf.findtext(".//{http://purl.org/dc/elements/1.1/}creator") or "")}
        builder = Builder()
        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            parser = _Html()
            parser.feed(archive.read(str(base / href)).decode("utf-8", errors="replace"))
            parser._flush("text")
            if not any(k != "scene_break" for k, _, _ in parser.blocks):
                continue
            heading = next((t for k, t, _ in parser.blocks if k == "heading"), "")
            builder.start(heading)
            for kind, text, italic in parser.blocks:
                builder.add(text, kind, italic)
    sections, paragraphs = builder.finish()
    return sections, paragraphs, meta


# ---------------- TXT / MD ----------------

def read_plain(path: Path) -> tuple[list[dict], list[dict], dict]:
    raw = path.read_text(encoding="utf-8-sig")
    builder = Builder()
    markdown = path.suffix.lower() == ".md"
    if markdown:
        raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
        raw = re.sub(r"\\([\\`*_{}\[\]()#+\-.!])", r"\1", raw)
    for block in re.split(r"\n\s*\n", raw):
        block = block.strip()
        if not block or block.startswith("<!--"):
            continue
        match = re.match(r"^(#{1,6})\s+(.*)$", block) if markdown else None
        if match:
            builder.start(match.group(2))
            builder.add(match.group(2), "heading")
            continue
        first = clean(block.splitlines()[0])
        if not markdown and HEADING_RE.match(first) and len(first) < 80 and len(block.splitlines()) == 1:
            builder.start(first)
            builder.add(first, "heading")
            continue
        builder.add(block)
    sections, paragraphs = builder.finish()
    return sections, paragraphs, {}


READERS = {".fb2": read_fb2, ".epub": read_epub, ".txt": read_plain, ".md": read_plain}


def ingest(project) -> dict:
    source = project.config["book"]["source"]
    if not source:
        raise HarnessError("Укажите book.source в book.toml")
    path = (project.root / source).resolve()
    if not path.is_file():
        raise HarnessError(f"Исходник не найден: {path}")
    reader = READERS.get(path.suffix.lower())
    if not reader:
        raise HarnessError(f"Формат {path.suffix} не поддерживается: {', '.join(READERS)}")
    sections, paragraphs, meta = reader(path)
    if not paragraphs:
        raise HarnessError("В исходнике не найдено ни одного абзаца")
    if project.paragraphs_path.exists():
        raise HarnessError("text/ уже создан. Повторное извлечение сменит ID абзацев; "
                           "удалите text/ вручную, если это действительно нужно")
    write_jsonl(project.paragraphs_path, paragraphs)
    summary = {"source": source, "source_sha256": sha_file(path), "extracted_utc": now_utc(),
               "metadata": meta, "sections": sections}
    write_json(project.sections_path, summary)
    return {"sections": len(sections), "paragraphs": len(paragraphs),
            "characters": sum(len(p["text"]) for p in paragraphs), **meta}
