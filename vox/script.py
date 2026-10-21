"""Режиссёрский сценарий: MD-файлы script/<раздел>.md.

Формат совместим со сценариями «Сокрушителя Войн»:

    ### s004-0012

    ```yaml
    source_ids: [p000491]
    scene_id: scene-014
    speaker_id: SIRI
    mode: spoken
    style: "quietly insistent"
    review_status: draft
    ```

    ```text
    Ты хочешь уйти <short pause> именно СЕЙЧАС?
    ```

Произносится только блок text; служебные поля в API как текст не уходят.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from .dialogue import guess_speaker, split_paragraph
from .util import HarnessError, letters, read_json, read_jsonl, write_text, check_id

MODES = {"narration", "spoken", "thought", "written", "heading", "epigraph", "mouthed", "skip"}
SILENT_MODES = {"mouthed", "skip"}
FIELDS = ["source_ids", "scene_id", "speaker_id", "mode", "style", "review_status",
          "pause_after_ms", "edit", "confidence", "note"]
# Теги Gemini 3.8 Flash TTS из таблицы Google (английские ASCII-скобки, без закрывающих).
ALLOWED_TAGS = {
    "breath", "heavy breath", "exhales", "pant", "gasp", "yawn", "laugh", "laughter",
    "chuckle", "chuckles", "giggle", "snicker", "cackle", "sigh", "sighs", "phew", "pff",
    "cry", "sob", "whimper", "groan", "moan", "scream", "shriek", "shout", "cheer", "argh",
    "grr", "growl", "grunt", "hiss", "tsk", "snort", "cough", "sneeze", "throat-clearing",
    "whispers", "whispering", "short pause", "long pause",
}
BLOCK = re.compile(r"(?m)^### ([A-Za-z0-9][A-Za-z0-9_.-]*)\s*\n\s*```yaml\s*\n(.*?)\n```\s*\n\s*"
                   r"```text\s*\n(.*?)\n```(?=\s|$)", re.S)


def _parse_value(value: str):
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        return [part.strip().strip("\"'") for part in value[1:-1].split(",") if part.strip()]
    if value.startswith('"') and value.endswith('"'):
        return json.loads(value)
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if value in ("", "null", "~"):
        return None
    return value


def parse_md(raw: str, name: str = "") -> list[dict]:
    segments = []
    for match in BLOCK.finditer(raw):
        meta = {}
        for line in match.group(2).splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, sep, value = line.partition(":")
            if not sep or not re.fullmatch(r"[a-z_]+", key.strip()):
                raise HarnessError(f"{name}: некорректная строка YAML в {match.group(1)}: {line}")
            meta[key.strip()] = _parse_value(value)
        segments.append({"id": match.group(1), **meta, "text": match.group(3)})
    declared = re.findall(r"(?m)^### ([A-Za-z0-9][A-Za-z0-9_.-]*)\s*$", raw)
    if len(declared) != len(segments):
        raise HarnessError(f"{name}: не каждый блок ### содержит пару ```yaml / ```text")
    return segments


def _format_value(value) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(value) + "]"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def render_md(title: str, segments: list[dict]) -> str:
    out = [f"# {title or 'Раздел'} — сценарий", "",
           "Произносятся только блоки `text`. Поля yaml — режиссура и учёт.", ""]
    for seg in segments:
        out += [f"### {seg['id']}", "", "```yaml"]
        for key in FIELDS:
            if seg.get(key) not in (None, ""):
                out.append(f"{key}: {_format_value(seg[key])}")
            elif key in ("style",):
                out.append(f'{key}: ""')
        out += ["```", "", "```text", seg["text"], "```", ""]
    return "\n".join(out)


def load_sections(project) -> dict:
    if not project.sections_path.exists():
        raise HarnessError("Сначала выполните ingest")
    return read_json(project.sections_path)


def load_paragraphs(project) -> dict[str, dict]:
    return {p["id"]: p for p in read_jsonl(project.paragraphs_path)}


def script_path(project, section_id: str) -> Path:
    return project.script_dir / f"{section_id}.md"


def load_script(project, section_id: str) -> list[dict]:
    path = script_path(project, section_id)
    if not path.exists():
        raise HarnessError(f"Нет сценария {path.name}: выполните draft")
    return parse_md(path.read_text(encoding="utf-8"), path.name)


def save_script(project, section: dict, segments: list[dict]) -> None:
    write_text(script_path(project, section["id"]), render_md(section["title"], segments))


def alias_map(project) -> dict[str, str]:
    if not project.cast_path.exists():
        return {}
    cast = read_json(project.cast_path)
    result = {}
    for role_id, role in cast.get("roles", {}).items():
        for alias in [role.get("name", ""), *role.get("aliases", [])]:
            if alias:
                result[alias] = role_id
    return result


def draft(project, force: bool = False, only: list[str] | None = None) -> dict:
    """Черновой сценарий из абзацев: речь/ремарки разделены, говорящий угадан, если можно."""
    sections = load_sections(project)["sections"]
    paragraphs = load_paragraphs(project)
    narrator = project.config["tts"]["narrator_role"]
    aliases = alias_map(project)
    stats = Counter()
    for section in sections:
        if not section["audio"] or (only and section["id"] not in only):
            continue
        path = script_path(project, section["id"])
        if path.exists() and not force:
            stats["kept"] += 1
            continue
        segments, scene = [], 1
        for pid in section["paragraphs"]:
            para = paragraphs[pid]
            if para["kind"] == "scene_break":
                scene += 1
                continue
            base = {"source_ids": [pid], "scene_id": f"{section['id']}-sc{scene:02}",
                    "review_status": "draft", "style": ""}
            if para["kind"] in ("heading", "subtitle"):
                pieces = [{"kind": "heading", "text": para["text"]}]
            elif para["kind"] == "epigraph":
                pieces = [{"kind": "epigraph", "text": para["text"]}]
            elif para["kind"] in ("poem", "table"):
                pieces = [{"kind": "narration", "text": para["text"]}]
            else:
                pieces = split_paragraph(para["text"])
            remarks = " ".join(p["text"] for p in pieces if p["kind"] == "narration")
            guessed = guess_speaker(remarks, aliases) if aliases else None
            for piece in pieces:
                speech = piece["kind"] == "speech"
                italic = [i for i in para.get("italic", []) if i in piece["text"]]
                segments.append({"id": f"{section['id']}-{len(segments) + 1:04}", **base,
                                 "speaker_id": (guessed or "UNKNOWN") if speech else narrator,
                                 "mode": "spoken" if speech else piece["kind"],
                                 "note": ("курсив: " + " | ".join(italic))[:300] if italic else None,
                                 "text": piece["text"]})
                stats["segments"] += 1
                stats["unknown"] += bool(speech and not guessed)
        save_script(project, section, segments)
        stats["written"] += 1
    return dict(stats)


def check_section(project, section: dict, paragraphs: dict, roles: set[str] | None,
                  require_approved: bool) -> dict:
    segments = load_script(project, section["id"])
    errors, warnings = [], []
    ids = Counter(s["id"] for s in segments)
    for seg_id, count in ids.items():
        if count > 1:
            errors.append(f"повторный id {seg_id}")
    by_para: dict[str, list[dict]] = {}
    order = []
    for seg in segments:
        sid = seg["id"]
        try:
            check_id(sid, "id блока")
        except HarnessError as exc:
            errors.append(str(exc))
        mode = seg.get("mode") or "narration"
        if mode not in MODES:
            errors.append(f"{sid}: неизвестный mode {mode}")
        speaker = seg.get("speaker_id")
        if mode not in SILENT_MODES:
            if not speaker or speaker == "UNKNOWN":
                errors.append(f"{sid}: не назначен говорящий")
            elif roles is not None and speaker not in roles:
                errors.append(f"{sid}: роли {speaker} нет в cast.json")
        text = seg.get("text", "")
        if not text.strip():
            errors.append(f"{sid}: пустой текст")
        if "```" in text or re.search(r"(?m)^#{1,6} ", text) or re.search(r"\b(speaker_id|style|mode):", text):
            errors.append(f"{sid}: в произносимый текст попала служебная разметка")
        for tag in re.findall(r"<([^<>]{1,40})>", text):
            if tag.strip().lower() not in ALLOWED_TAGS:
                errors.append(f"{sid}: неизвестный тег <{tag}> прозвучит вслух")
        if re.search(r"\[[a-z ]+\]", text):
            warnings.append(f"{sid}: квадратные теги [..] из старых моделей прочитаются вслух")
        if require_approved and seg.get("review_status") != "approved" and mode not in SILENT_MODES:
            errors.append(f"{sid}: блок не утверждён (review_status: approved)")
        sources = seg.get("source_ids") or []
        if not sources:
            errors.append(f"{sid}: нет source_ids")
        for pid in sources:
            if pid not in by_para:
                order.append(pid)
            by_para.setdefault(pid, []).append(seg)
    expected = [pid for pid in section["paragraphs"] if paragraphs[pid]["kind"] != "scene_break"]
    missing = [pid for pid in expected if pid not in by_para]
    extra = [pid for pid in by_para if pid not in expected]
    if missing:
        errors.append(f"абзацы без сценария: {', '.join(missing[:10])}{' …' if len(missing) > 10 else ''}")
    if extra:
        errors.append(f"блоки ссылаются на чужие абзацы: {', '.join(extra[:10])}")
    if [p for p in order if p in expected] != [p for p in expected if p in by_para]:
        errors.append("порядок абзацев в сценарии нарушен")
    edits = []
    for pid, segs in by_para.items():
        if pid not in paragraphs or any(len(s.get("source_ids") or []) > 1 for s in segs):
            continue  # блок на несколько абзацев сверяется вручную
        if letters(" ".join(s["text"] for s in segs)) != letters(paragraphs[pid]["text"]):
            reasons = [s.get("edit") for s in segs if s.get("edit")]
            if reasons:
                edits.append({"paragraph": pid, "reason": "; ".join(reasons)})
            else:
                errors.append(f"{pid}: текст сценария не совпадает с исходным абзацем; "
                              "укажите причину в поле edit у блока")
    return {"section": section["id"], "segments": len(segments), "errors": errors,
            "warnings": warnings, "edits": edits,
            "unknown": sum(1 for s in segments if s.get("speaker_id") == "UNKNOWN"),
            "approved": sum(1 for s in segments if s.get("review_status") == "approved")}


def check_all(project, require_approved: bool = False) -> list[dict]:
    sections = load_sections(project)["sections"]
    paragraphs = load_paragraphs(project)
    roles = set(read_json(project.cast_path)["roles"]) if project.cast_path.exists() else None
    reports = []
    for section in sections:
        if section["audio"]:
            reports.append(check_section(project, section, paragraphs, roles, require_approved))
    return reports


def approve(project, section_ids: list[str]) -> int:
    """Пометить блоки разделов утверждёнными (после вычитки)."""
    count = 0
    sections = {s["id"]: s for s in load_sections(project)["sections"]}
    for sid in section_ids:
        if sid not in sections:
            raise HarnessError(f"Нет раздела {sid}")
        segments = load_script(project, sid)
        for seg in segments:
            if seg.get("speaker_id") == "UNKNOWN":
                raise HarnessError(f"{seg['id']}: сначала назначьте говорящего")
            if seg.get("review_status") != "approved":
                seg["review_status"] = "approved"
                count += 1
        save_script(project, sections[sid], segments)
    return count
