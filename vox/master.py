"""Мастеринг: громкость каждой главы (двухпроходный loudnorm), M4B с оглавлением."""
from __future__ import annotations

from .post import LOUDNESS, ffmpeg_path as _ffmpeg, loudnorm_filter, measure, run as _run
from .util import HarnessError, read_json, sha_file, write_json, write_text, now_utc

TITLES = {"front": "", "note": "Примечание"}


def ffmpeg_path(config: dict) -> str:
    return _ffmpeg(config)


def master(project, allow_open: bool = False) -> dict:
    release = project.active_release()
    cfg = project.config["master"]
    ff = ffmpeg_path(project.config)
    sections = read_json(project.release_dir(release) / "sections.json")
    chapters_dir = project.chapters_dir(release)
    out = project.master_dir(release)
    out.mkdir(parents=True, exist_ok=True)
    rows, problems = [], []
    for section in sections:
        meta_path = chapters_dir / f"{section['id']}.json"
        if not meta_path.exists():
            problems.append(f"{section['id']}: глава не собрана")
            continue
        meta = read_json(meta_path)
        if sha_file(chapters_dir / meta["file"]) != meta["audio_sha256"]:
            problems.append(f"{section['id']}: файл главы изменён после сборки")
        if meta["missing_jobs"]:
            problems.append(f"{section['id']}: в главе нет {len(meta['missing_jobs'])} jobs")
        if meta["open_qc_issues"] and not allow_open:
            problems.append(f"{section['id']}: открытые подозрения QC ({len(meta['open_qc_issues'])})")
        rows.append((section, meta))
    if problems:
        raise HarnessError("Мастер не собран:\n  " + "\n  ".join(problems)
                           + "\nДля тестовой сборки с открытыми подозрениями: --allow-open")
    # preset = "custom" — взять lufs/true_peak/lra из [master] как есть
    target = LOUDNESS.get(cfg.get("preset")) or {k: cfg[k] for k in ("lufs", "true_peak", "lra")}
    gap = float(cfg["chapter_gap_s"])
    parts, chapters, cursor = [], [], 0.0
    for index, (section, meta) in enumerate(rows):
        source = chapters_dir / meta["file"]
        second = loudnorm_filter(ff, source, target)
        pad = f",apad=pad_dur={gap}" if index < len(rows) - 1 else ""
        part = out / f"{index:03}-{section['id']}.flac"
        _run(ff, ["-i", source, "-af", second + pad, "-ar", 24000, "-ac", 1, "-c:a", "flac", part])
        duration = meta["duration_s"] + (gap if pad else 0)
        title = section["title"] or TITLES.get(section["kind"], "") or section["id"]
        chapters.append({"title": title, "start": cursor, "end": cursor + duration})
        cursor += duration
        parts.append(part)
    concat = out / "concat.txt"
    write_text(concat, "".join(f"file '{p.name}'\n" for p in parts))
    book = project.config["book"]
    meta_lines = [";FFMETADATA1", f"title={book['title']}", f"artist={book['author']}", f"album={book['title']}"]
    for ch in chapters:
        meta_lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(ch['start'] * 1000)}",
                       f"END={int(ch['end'] * 1000)}", "title=" + ch["title"].replace("=", "\\=")]
    write_text(out / "chapters.ffmeta", "\n".join(meta_lines) + "\n")
    m4b = out / "book.m4b"
    _run(ff, ["-f", "concat", "-safe", "0", "-i", concat, "-i", out / "chapters.ffmeta", "-map_metadata", "1",
              "-map_chapters", "1", "-map", "0:a", "-c:a", "aac", "-b:a", cfg["bitrate"], "-ar", 24000, "-ac", 1,
              "-movflags", "+faststart", m4b])
    actual = measure(ff, m4b)
    warnings = []
    if abs(actual["lufs"] - target["lufs"]) > 1.0:
        warnings.append(f"громкость {actual['lufs']} LUFS вместо {target['lufs']}")
    if actual["true_peak"] > target["true_peak"] + 0.5:
        warnings.append(f"пик {actual['true_peak']} dBTP выше {target['true_peak']}")
    result = {"release": release, "file": m4b.name, "sha256": sha_file(m4b), "chapters": chapters,
              "loudness_target": target, "loudness_measured": actual, "loudness_warnings": warnings,
              "duration_s": round(cursor, 2), "status": "pending_listening" if not allow_open else "test_build_open_qc",
              "settings": cfg, "created_utc": now_utc()}
    write_json(out / "manifest.json", result)
    return result
