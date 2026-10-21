"""Заморозка релиза: проверенный сценарий -> точные запросы TTS (jobs).

Релиз неизменяем. Каждый job несёт канонический запрос (модель, голос, style,
текст для синтеза) и его хеш. Хеш — ключ кэша аудио: при новом релизе
неизменённые реплики не синтезируются повторно и не оплачиваются.
"""
from __future__ import annotations

import re

from . import cast as cast_mod
from . import groups
from . import pronounce
from .gemini import core_request
from .script import SILENT_MODES, check_all, load_script, load_sections
from .util import (HarnessError, read_json, read_jsonl, sha_file, sha_value, write_json,
                   write_jsonl, now_utc)

SENTENCE = re.compile(r"(?<=[.!?…])\s+(?=[«\"—–(]?[A-ZА-ЯЁ0-9])")


def estimate_tokens(text: str, style: str) -> int:
    # Грубая верхняя оценка для кириллицы: ~2.5 символа на токен.
    return int(len(text) / 2.5 + len(style) / 3.5) + 60


def split_long(text: str, max_words: int) -> list[str]:
    """Разбить слишком длинный блок по границам предложений, не разрывая предложение."""
    sentences = SENTENCE.split(text)
    parts, current = [], []
    for sentence in sentences:
        if current and len(" ".join(current + [sentence]).split()) > max_words:
            parts.append(" ".join(current))
            current = []
        current.append(sentence)
    if current:
        parts.append(" ".join(current))
    return parts


def _join_kind(prev: dict | None, seg: dict) -> str:
    if prev is None:
        return "start"
    if seg.get("mode") == "heading" or prev.get("mode") == "heading":
        return "heading"
    if seg.get("scene_id") != prev.get("scene_id"):
        return "scene"
    if (seg.get("source_ids") or [None])[0] != (prev.get("source_ids") or [None])[-1]:
        return "paragraph"
    if seg.get("speaker_id") != prev.get("speaker_id"):
        return "turn"
    return "continuation"


def build_jobs(project, section: dict, segments: list[dict], cast: dict, lexicon) -> list[dict]:
    cfg = project.config
    model = cfg["tts"]["model"]
    max_words = int(cfg["chunking"]["max_words"])
    token_cap = int(cfg["chunking"]["max_input_tokens"] * cfg["chunking"]["token_safety"])
    effects = cfg.get("effects", {})
    pieces, prev = [], None
    for seg in segments:
        if (seg.get("mode") or "narration") in SILENT_MODES:
            continue
        tts_text, applied = lexicon.apply(seg["text"].strip())
        style = cast_mod.effective_style(project, cast, seg["speaker_id"], seg.get("style") or "")
        voice = cast_mod.voice_for(cast, seg["speaker_id"])
        join = _join_kind(prev, seg)
        parts = split_long(tts_text, max_words) if len(tts_text.split()) > max_words else [tts_text]
        for k, part in enumerate(parts):
            if estimate_tokens(part, style) > token_cap:
                raise HarnessError(f"{seg['id']}: предложение длиннее лимита запроса; разбейте блок")
            pieces.append({"segment_id": seg["id"], "part": k, "text": part, "speaker_id": seg["speaker_id"],
                           "mode": seg.get("mode") or "narration", "style": style, "voice": voice,
                           "join": join if k == 0 else "continuation", "applied": applied if k == 0 else [],
                           "pause_after_ms": seg.get("pause_after_ms") if k == len(parts) - 1 else None})
        prev = seg
    jobs = []
    for piece in pieces:
        last = jobs[-1] if jobs else None
        mergeable = (last is not None and last["speaker_id"] == piece["speaker_id"]
                     and last["style"] == piece["style"] and last["mode"] == piece["mode"]
                     and last["pause_after_ms"] is None
                     and piece["join"] in ("continuation", "paragraph")
                     and (piece["join"] == "continuation" or cfg["chunking"]["merge_across_paragraphs"])
                     and len((last["text"] + " " + piece["text"]).split()) <= max_words
                     and estimate_tokens(last["text"] + piece["text"], piece["style"]) <= token_cap)
        if mergeable:
            sep = "\n" if piece["join"] == "paragraph" else " "
            last["text"] += sep + piece["text"]
            last["pieces"].append({"segment_id": piece["segment_id"], "part": piece["part"]})
            last["pronunciation_applied"] += piece["applied"]
            last["pause_after_ms"] = piece["pause_after_ms"]
            continue
        jobs.append({"section": section["id"], "speaker_id": piece["speaker_id"], "mode": piece["mode"],
                     "voice": piece["voice"], "style": piece["style"], "text": piece["text"],
                     "join_before": piece["join"], "pause_after_ms": piece["pause_after_ms"],
                     "effect": effects.get(piece["mode"]) if piece["speaker_id"] != cfg["tts"]["narrator_role"] else None,
                     "pieces": [{"segment_id": piece["segment_id"], "part": piece["part"]}],
                     "pronunciation_applied": list(piece["applied"])})
    for n, job in enumerate(jobs, 1):
        job["id"] = f"{section['id']}-j{n:04}"
        job["core"] = core_request(model, job["voice"], job["style"], job["text"])
        job["request_hash"] = sha_value(job["core"])
        job["est_tokens"] = estimate_tokens(job["text"], job["style"])
    if cfg["chunking"].get("group_remarks"):
        _apply_groups(cfg, jobs, model, token_cap)
    return jobs


def _apply_groups(cfg: dict, jobs: list[dict], model: str, token_cap: int) -> None:
    """Короткие ремарки рассказчика одной сцены — одним запросом (см. vox/groups.py)."""
    found = groups.group_remarks(jobs, cfg["tts"]["narrator_role"], int(cfg["chunking"]["remark_max_words"]),
                                 int(cfg["chunking"]["group_max_remarks"]))
    for members in found:
        primary = jobs[members[0]]
        parts = [jobs[i]["text"] for i in members]
        text = "\n".join(parts)
        if estimate_tokens(text, primary["style"]) > token_cap:
            continue
        primary.update(part_text=parts[0], text=text, group_parts=parts, group_part=0,
                       core=core_request(model, primary["voice"], primary["style"], text))
        primary["request_hash"] = sha_value(primary["core"])
        primary["est_tokens"] = estimate_tokens(text, primary["style"])
        for k, i in enumerate(members[1:], 1):
            jobs[i].update(group_ref=primary["id"], group_part=k, core=primary["core"],
                           request_hash=primary["request_hash"], est_tokens=0)


def freeze(project, allow_draft: bool = False, include_proposed_pronunciation: bool = False,
           sections_only: list[str] | None = None) -> dict:
    reports = check_all(project, require_approved=not allow_draft)
    errors = [f"{r['section']}: {e}" for r in reports for e in r["errors"]
              if not sections_only or r["section"] in sections_only]
    if errors:
        raise HarnessError("Сценарий не готов к заморозке:\n  " + "\n  ".join(errors[:40])
                           + (f"\n  … ещё {len(errors) - 40}" if len(errors) > 40 else ""))
    sections = [s for s in load_sections(project)["sections"]
                if s["audio"] and (not sections_only or s["id"] in sections_only)]
    scripts = {s["id"]: load_script(project, s["id"]) for s in sections}
    cast_report = cast_mod.check(project, scripts)
    if cast_report["errors"]:
        raise HarnessError("Каст не готов:\n  " + "\n  ".join(cast_report["errors"]))
    if cast_report["conflicts"]:
        raise HarnessError("В одной сцене разные роли звучат одним голосом:\n  "
                           + "\n  ".join(cast_report["conflicts"][:20]))
    cast = cast_mod.load(project)
    lexicon = pronounce.Lexicon(pronounce.load(project), include_proposed_pronunciation)
    jobs = []
    for section in sections:
        jobs += build_jobs(project, section, scripts[section["id"]], cast, lexicon)
    inputs = {
        "scripts": {s["id"]: sha_file(project.script_dir / f"{s['id']}.md") for s in sections},
        "cast": sha_file(project.cast_path),
        "lexicon": sha_file(project.lexicon_path) if project.lexicon_path.exists() else None,
        "settings": sha_value({k: project.config[k] for k in ("tts", "chunking", "pauses", "effects")}),
        "allow_draft": allow_draft, "proposed_pronunciation": include_proposed_pronunciation,
    }
    fingerprint = sha_value({"inputs": inputs, "jobs": [j["request_hash"] for j in jobs]})
    project.releases_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(p.name for p in project.releases_dir.iterdir() if p.is_dir() and p.name.startswith("r"))
    for name in existing:
        manifest = read_json(project.releases_dir / name / "manifest.json")
        if manifest.get("fingerprint") == fingerprint:
            project.set_active(name, sha_file(project.releases_dir / name / "manifest.json"))
            return {"release": name, "reused": True, "jobs": len(jobs)}
    name = f"r{len(existing) + 1:03}"
    folder = project.releases_dir / name
    write_jsonl(folder / "jobs.jsonl", jobs)
    write_json(folder / "segments.json", scripts)
    write_json(folder / "sections.json", [{"id": s["id"], "title": s["title"], "kind": s["kind"]}
                                          for s in sections])
    manifest = {"release": name, "created_utc": now_utc(), "fingerprint": fingerprint, "inputs": inputs,
                "files": {f: sha_file(folder / f) for f in ("jobs.jsonl", "segments.json", "sections.json")},
                "counts": {"sections": len(sections), "jobs": len(jobs),
                           "segments": sum(len(v) for v in scripts.values()),
                           "chars": sum(len(j["text"]) for j in jobs if not groups.is_ref(j))},
                "status": "frozen" if not allow_draft else "frozen_draft_for_trial"}
    write_json(folder / "manifest.json", manifest)
    project.set_active(name, sha_file(folder / "manifest.json"))
    return {"release": name, "reused": False, "jobs": len(jobs), "chars": manifest["counts"]["chars"]}


def load_jobs(project, release: str | None = None) -> list[dict]:
    release = release or project.active_release()
    folder = project.release_dir(release)
    manifest = read_json(folder / "manifest.json")
    for name, digest in manifest["files"].items():
        if sha_file(folder / name) != digest:
            raise HarnessError(f"Файл релиза {release}/{name} изменён после заморозки")
    return read_jsonl(folder / "jobs.jsonl")
