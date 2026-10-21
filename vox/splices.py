"""Проверка склейки собранных глав: паузы, наложения, щелчки, скачки громкости,
повторы через стык (распознавание окна вокруг каждой склейки)."""
from __future__ import annotations

import tempfile
from collections import Counter
from pathlib import Path

import numpy as np

from . import audio as audio_mod
from . import pronounce
from .assemble import gap_ms
from .groups import spoken_text
from .freeze import load_jobs
from .util import read_json, words, write_json, write_text, now_utc


def _speech_db(samples: np.ndarray, rate: int) -> float:
    db = audio_mod.frame_db(samples, rate)
    voiced = db[db > -50]
    return float(np.median(voiced)) if len(voiced) else -120.0


def check_section(project, release: str, section_id: str, recognizers: list, lexicon) -> dict:
    folder = project.chapters_dir(release)
    meta = read_json(folder / f"{section_id}.json")
    samples, rate = audio_mod.read_wav(folder / meta["file"])
    jobs = {j["id"]: j for j in load_jobs(project, release) if j["section"] == section_id}
    qc = project.config["qc"]
    window = float(qc.get("splice_window_s", 3.0))
    jump = float(qc.get("splice_level_jump_db", 6.0))
    findings, levels = [], {}
    timeline = meta["timeline"]
    for item in timeline:
        a, b = int(item["start_s"] * rate), int(item["end_s"] * rate)
        levels[item["job_id"]] = _speech_db(samples[a:b], rate)
    median = float(np.median(list(levels.values()))) if levels else 0.0
    for item in timeline:
        if abs(levels[item["job_id"]] - median) > jump:
            findings.append({"type": "громкость запроса отличается от главы", "job": item["job_id"],
                             "at_s": item["start_s"], "detail": f"{levels[item['job_id']] - median:+.1f} dB"})
    for prev, nxt in zip(timeline, timeline[1:]):
        at = nxt["start_s"]
        gap = round(nxt["start_s"] - prev["end_s"], 3)
        expected = gap_ms(project, jobs[nxt["job_id"]], jobs[prev["job_id"]]) / 1000
        if gap < -0.001:
            findings.append({"type": "наложение", "at_s": at, "detail": f"{gap:.3f} с"})
        elif abs(gap - expected) > 0.01:
            findings.append({"type": "пауза не по карте", "at_s": at, "detail": f"{gap:.3f} вместо {expected:.3f} с"})
        p_end, n_start = int(prev["end_s"] * rate), int(nxt["start_s"] * rate)
        for point in (p_end, n_start):
            seg = samples[max(0, point - rate // 100): point + rate // 100]
            if len(seg) > 1 and float(np.max(np.abs(np.diff(seg)))) > 0.3:
                findings.append({"type": "щелчок", "at_s": round(point / rate, 3)})
        tail = samples[max(int(prev["start_s"] * rate), p_end - rate): p_end]
        head = samples[n_start: min(int(nxt["end_s"] * rate), n_start + rate)]
        diff = _speech_db(head, rate) - _speech_db(tail, rate)
        limit = jump if prev["speaker_id"] == nxt["speaker_id"] else jump + 4
        if abs(diff) > limit:
            findings.append({"type": "скачок громкости на стыке", "at_s": at, "detail": f"{diff:+.1f} dB"})
        if recognizers:
            lo = max(0, int((prev["end_s"] - window) * rate))
            hi = min(len(samples), int((nxt["start_s"] + window) * rate))
            expected_tokens = words(lexicon.to_readable(spoken_text(jobs[prev["job_id"]]) + " " + spoken_text(jobs[nxt["job_id"]])))
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "splice.wav"
                audio_mod.write_wav(path, samples[lo:hi], rate)
                for rec in recognizers:
                    observed = [t for w in rec.transcribe(path)["words"] for t in words(w["word"])]
                    grams = Counter(tuple(observed[i:i + 4]) for i in range(len(observed) - 3))
                    for gram, count in grams.items():
                        allowed = sum(tuple(expected_tokens[i:i + 4]) == gram for i in range(len(expected_tokens) - 3))
                        if count > max(1, allowed):
                            findings.append({"type": "повтор через стык", "at_s": at, "detail": " ".join(gram),
                                             "source": rec.name})
    result = {"section": section_id, "release": release, "audio_sha256": meta["audio_sha256"],
              "joins": max(0, len(timeline) - 1), "findings": findings, "checked_utc": now_utc()}
    write_json(project.qc_dir(release) / "splices" / f"{section_id}.json", result)
    return result


def run(project, sections: list[str] | None = None, recognizers: list | None = None) -> dict:
    release = project.active_release()
    lexicon = pronounce.Lexicon(pronounce.load(project), include_proposed=True)
    order = [s["id"] for s in read_json(project.release_dir(release) / "sections.json")]
    results = []
    for section_id in order:
        if sections and section_id not in sections:
            continue
        if not (project.chapters_dir(release) / f"{section_id}.json").exists():
            continue
        results.append(check_section(project, release, section_id, recognizers or [], lexicon))
    lines = [f"# Проверка склеек, релиз {release}", "",
             "Время — от начала главы. Прослушайте каждое место ±5 с.", "",
             "| Глава | Время, с | Что | Подробно |", "|---|---|---|---|"]
    for r in results:
        for f in r["findings"]:
            lines.append(f"| {r['section']} | {f.get('at_s', '')} | {f['type']} | {f.get('detail', '')} |")
    write_text(project.qc_dir(release) / "SPLICES.md", "\n".join(lines) + "\n")
    return {"sections": len(results), "joins": sum(r["joins"] for r in results),
            "findings": sum(len(r["findings"]) for r in results)}
