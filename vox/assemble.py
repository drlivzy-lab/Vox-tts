"""Монтаж глав: jobs в исходном порядке, паузы по карте, эффекты режимов.

Исходные WAV из кэша не меняются: каждая глава собирается заново и
сопровождается timeline.json (где какой job звучит) для проверки и ремонта.
"""
from __future__ import annotations

import numpy as np

from . import audio as audio_mod
from . import groups
from . import qc as qc_mod
from .freeze import load_jobs
from .ledger import Ledger
from .util import HarnessError, read_json, sha_file, write_json, now_utc


def gap_ms(project, job: dict, previous: dict | None) -> int:
    pauses = project.config["pauses"]
    if previous and previous.get("pause_after_ms") is not None:
        return int(previous["pause_after_ms"])
    kind = job["join_before"]
    if kind == "start":
        return 0
    return int(pauses.get(kind, pauses["paragraph"]))


def assemble(project, sections: list[str] | None = None, allow_missing: bool = False) -> list[dict]:
    release = project.active_release()
    jobs = load_jobs(project, release)
    by_id = {j["id"]: j for j in jobs}
    order = [s["id"] for s in read_json(project.release_dir(release) / "sections.json")]
    if sections:
        order = [s for s in order if s in sections]
    ledger = Ledger(project)
    pauses = project.config["pauses"]
    out_dir = project.chapters_dir(release)
    results = []
    for section_id in order:
        section_jobs = [j for j in jobs if j["section"] == section_id]
        missing = [j["id"] for j in section_jobs if not ledger.has(j["request_hash"])]
        if missing and not allow_missing:
            raise HarnessError(f"{section_id}: нет аудио для {len(missing)} jobs, например {missing[0]}")
        chunks, timeline, cursor, previous = [], [], 0, None
        rate = audio_mod.SR
        for job in section_jobs:
            if job["id"] in missing:
                previous = None
                continue
            if "group_parts" in job or groups.is_ref(job):
                primary = by_id[job.get("group_ref") or job["id"]]
                try:
                    samples = groups.part_samples(project, ledger, job, primary)
                except groups.CutError as exc:
                    raise HarnessError(f"{job['id']}: нарезка группы ремарок {primary['id']} не удалась ({exc}). "
                                       f"Перегенерируйте: qc reject {primary['id']}") from None
                rate = audio_mod.SR
            else:
                samples, rate = audio_mod.read_wav(ledger.audio_path(job["request_hash"]))
            if rate != audio_mod.SR:
                raise HarnessError(f"{job['id']}: частота {rate}")
            samples = audio_mod.trim(samples, rate, pauses["silence_db"], pauses["edge_keep_ms"])
            samples = audio_mod.apply_effect(samples, rate, job.get("effect"))
            gap = gap_ms(project, job, previous) * rate // 1000 if chunks else 0
            if gap:
                chunks.append(np.zeros(gap, dtype=np.float32))
                cursor += gap
            timeline.append({"job_id": job["id"], "speaker_id": job["speaker_id"],
                             "start_s": round(cursor / rate, 3), "end_s": round((cursor + len(samples)) / rate, 3),
                             "segments": [p["segment_id"] for p in job["pieces"]]})
            chunks.append(samples.astype(np.float32))
            cursor += len(samples)
            previous = job
        if not chunks:
            continue
        data = np.concatenate(chunks)
        peak = float(np.max(np.abs(data)))
        path = out_dir / f"{section_id}.wav"
        audio_mod.write_wav(path, data, rate)
        issues = qc_mod.open_issues(project, release, section_jobs)
        report = {"section": section_id, "release": release, "file": path.name, "audio_sha256": sha_file(path),
                  "duration_s": round(len(data) / rate, 2), "peak": round(peak, 4), "jobs": len(timeline),
                  "missing_jobs": missing, "open_qc_issues": [i["job_id"] for i in issues],
                  "status": "pending_listening", "assembled_utc": now_utc(), "timeline": timeline}
        write_json(out_dir / f"{section_id}.json", report)
        results.append({k: report[k] for k in ("section", "duration_s", "jobs", "missing_jobs", "open_qc_issues")})
    return results
