"""Группировка коротких ремарок рассказчика и нарезка аудио группы.

Ремарка «— сказал Вар» отдельным запросом звучит оторванно и стоит целого запроса.
При chunking.group_remarks = true подряд идущие короткие ремарки одной сцены
синтезируются одним запросом (части через перевод строки), а затем аудио режется
по паузам между частями. Первая ремарка группы — основной job (несёт полный текст
и запрос), остальные — ссылки (group_ref) с тем же хешем: синтез и оплата один раз.

Нарезка: ищутся паузы, ближайшие к ожидаемым границам (по доле букв), динамическим
программированием; каждая часть проверяется по скорости речи. Если нарезать не
удалось, сборка останавливается, а qc run показывает находку «нарезка группы» —
тогда qc reject (перегенерировать) или выключите группировку для книги.
С распознаванием (qc.asr) каждая часть отдельно сверяется со своим текстом.
"""
from __future__ import annotations

import numpy as np

from . import audio as audio_mod
from .util import HarnessError, read_json, sha_file, write_json

NARRATION_JOINS = ("turn",)
BETWEEN_JOINS = ("turn", "continuation", "paragraph")


class CutError(HarnessError):
    pass


def is_ref(job: dict) -> bool:
    return bool(job.get("group_ref"))


def spoken_text(job: dict) -> str:
    """Текст, который звучит в месте этого job (у группы — только своя часть)."""
    return job.get("part_text") or job["text"]


def group_remarks(jobs: list[dict], narrator: str, max_words: int, max_members: int) -> list[list[int]]:
    """Индексы jobs, которые синтезируются вместе. Возвращает только группы из 2+ ремарок."""
    def remark(job: dict) -> bool:
        return (job["speaker_id"] == narrator and job["mode"] == "narration"
                and job["join_before"] in NARRATION_JOINS and job.get("pause_after_ms") is None
                and len(job["text"].split()) <= max_words)

    groups, current = [], []
    for i, job in enumerate(jobs):
        if remark(job):
            if current and len(current) < max_members:
                between = jobs[current[-1] + 1:i]
                if all(b["speaker_id"] != narrator and b["join_before"] in BETWEEN_JOINS for b in between):
                    current.append(i)
                    continue
            if len(current) > 1:
                groups.append(current)
            current = [i]
        elif job["speaker_id"] == narrator or job["join_before"] not in BETWEEN_JOINS:
            if len(current) > 1:
                groups.append(current)
            current = []
    if len(current) > 1:
        groups.append(current)
    return groups


def _letters(text: str) -> int:
    return max(1, sum(c.isalnum() for c in text))


def find_cuts(samples: np.ndarray, rate: int, parts: list[str], qc: dict,
              silence_db: float = -45.0, min_gap_s: float = 0.12) -> list[tuple[int, int]]:
    """Границы частей (в отсчётах). CutError, если пауз не хватает или части неправдоподобны."""
    if len(parts) == 1:
        return [(0, len(samples))]
    duration = len(samples) / rate
    voiced = audio_mod.silences(samples, rate, silence_db, min_gap_s)
    edges = [s for s in voiced if s[0] > 0.05 and s[1] < duration - 0.05]
    if len(edges) < len(parts) - 1:
        raise CutError(f"пауз между частями {len(edges)}, нужно {len(parts) - 1}")
    db = audio_mod.frame_db(samples, rate)
    loud = np.flatnonzero(db >= silence_db)
    start, end = loud[0] / 100, (loud[-1] + 1) / 100
    total = sum(_letters(p) for p in parts)
    expected, acc = [], 0
    for p in parts[:-1]:
        acc += _letters(p)
        expected.append(start + (end - start) * acc / total)
    mids = [(a + b) / 2 for a, b in edges]
    k, n = len(expected), len(mids)
    # DP: выбрать k пауз по возрастанию с минимальной суммой квадратов отклонений;
    # длинная пауза чуть предпочтительнее короткой (бонус за длительность).
    inf = float("inf")
    cost = [[inf] * n for _ in range(k)]
    back = [[-1] * n for _ in range(k)]
    for j in range(n):
        cost[0][j] = (mids[j] - expected[0]) ** 2 - 0.05 * (edges[j][1] - edges[j][0])
    for i in range(1, k):
        for j in range(i, n):
            best = min(range(i - 1, j), key=lambda m: cost[i - 1][m], default=None)
            if best is not None and cost[i - 1][best] < inf:
                cost[i][j] = cost[i - 1][best] + (mids[j] - expected[i]) ** 2 - 0.05 * (edges[j][1] - edges[j][0])
                back[i][j] = best
    j = min(range(n), key=lambda m: cost[k - 1][m])
    if cost[k - 1][j] == inf:
        raise CutError("не удалось сопоставить паузы с частями")
    chosen = [j]
    for i in range(k - 1, 0, -1):
        j = back[i][j]
        chosen.append(j)
    chosen.reverse()
    points = [0] + [int(mids[c] * rate) for c in chosen] + [len(samples)]
    spans = list(zip(points, points[1:]))
    lo, hi = float(qc["min_chars_per_s"]) * 0.5, float(qc["max_chars_per_s"]) * 1.6
    for (a, b), text in zip(spans, parts):
        seconds = max(1e-3, len(audio_mod.trim(samples[a:b], rate, silence_db, 0)) / rate)
        cps = _letters(text) / seconds
        if _letters(text) >= 8 and not lo <= cps <= hi:
            raise CutError(f"часть «{text[:30]}…» получилась {seconds:.2f} с ({cps:.1f} симв/с)")
    return spans


def cuts_for(project, ledger, job: dict) -> list[tuple[int, int]]:
    """Нарезка аудио основного job группы, с кэшем рядом с WAV (по хешу аудио)."""
    path = ledger.audio_path(job["request_hash"])
    cache = path.with_suffix(".cuts.json")
    digest = sha_file(path)
    if cache.exists():
        data = read_json(cache)
        if data.get("audio_sha256") == digest and data.get("parts") == job["group_parts"]:
            return [tuple(s) for s in data["spans"]]
    samples, rate = audio_mod.read_wav(path)
    spans = find_cuts(samples, rate, job["group_parts"], project.config["qc"])
    write_json(cache, {"audio_sha256": digest, "parts": job["group_parts"], "spans": spans})
    return spans


def part_samples(project, ledger, job: dict, primary: dict) -> np.ndarray:
    """Аудио части job (основного или ссылки) из записи основного job."""
    samples, rate = audio_mod.read_wav(ledger.audio_path(primary["request_hash"]))
    a, b = cuts_for(project, ledger, primary)[job.get("group_part", 0)]
    return samples[a:b]
