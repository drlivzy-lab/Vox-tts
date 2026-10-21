"""Проверка полученного аудио.

Распознавание выполняется БЕЗ подстановки ожидаемого текста, затем сравнивается
с тем, что было отправлено. Сигналы — кандидаты, а не доказанные дефекты:
окончательное решение принимает слушатель (`qc accept` / `qc reject`).
"""
from __future__ import annotations

import difflib
from pathlib import Path

import numpy as np

from . import asr as asr_mod
from . import audio as audio_mod
from . import groups
from . import pronounce
from .freeze import load_jobs
from .ledger import Ledger
from .util import HarnessError, read_json, words, write_json, write_text, now_utc


# ---------------- сравнение ----------------

def compare(expected_text: str, observed_words: list[dict], qc: dict) -> list[dict]:
    """Сравнить отправленный текст с дословной расшифровкой; вернуть кандидаты дефектов."""
    expected = words(expected_text)
    observed, timing = [], []
    for w in observed_words:
        for token in words(w["word"]):
            observed.append(token)
            timing.append(w)
    findings = []
    min_insert, min_repeat = int(qc["insert_min_words"]), int(qc["repeat_min_words"])

    def at(c: int, d: int) -> dict:
        if not timing or c >= len(timing):
            return {}
        return {"start_s": round(timing[c]["start"], 2), "end_s": round(timing[max(c, d - 1)]["end"], 2)}

    matcher = difflib.SequenceMatcher(None, expected, observed, autojunk=False)
    for tag, a, b, c, d in matcher.get_opcodes():
        extra, lost = (d - c) - (b - a), (b - a) - (d - c)
        if tag in ("insert", "replace") and extra >= min_insert:
            findings.append({"type": "лишние слова", "observed": " ".join(observed[c:d]),
                             "expected": " ".join(expected[a:b]), **at(c, d)})
        if tag in ("delete", "replace") and lost >= min_insert:
            findings.append({"type": "пропуск", "expected": " ".join(expected[a:b]),
                             "observed": " ".join(observed[c:d]), **at(c, d)})
    # Повтор фразы, которой в тексте нет дважды (зацикливание/перезапуск).
    seen = []
    for i in range(len(observed) - min_repeat + 1):
        for j in range(i + 1, min(len(observed) - min_repeat + 1, i + 80)):
            if observed[i:i + min_repeat] != observed[j:j + min_repeat]:
                continue
            length = min_repeat
            while j + length < len(observed) and i + length < j and observed[i + length] == observed[j + length]:
                length += 1
            phrase = observed[j:j + length]
            if any(i >= x and j >= y and i + length <= x + l for x, y, l in seen):
                continue
            occurrences = sum(expected[k:k + length] == phrase for k in range(len(expected) - length + 1))
            if occurrences < 2:
                seen.append((i, j, length))
                findings.append({"type": "повтор", "observed": " ".join(phrase), **at(j, j + length)})
    tail = expected[-3:]
    if len(expected) >= 6 and tail and " ".join(tail) not in " ".join(observed[-8:]):
        findings.append({"type": "обрыв конца?", "expected": " ".join(tail),
                         "observed": " ".join(observed[-6:])})
    return findings


def engines(project, client=None) -> list:
    cfg = project.config
    found = []
    for kind in (cfg["qc"]["asr"], cfg["qc"].get("second_asr", "none")):
        item = asr_mod.engine(kind, cfg, client)
        if item:
            found.append(item)
    return found


def check_audio(project, path: Path, text: str, lexicon, recognizers: list) -> dict:
    """Проверка одного файла: техника + каждый движок распознавания отдельно."""
    tech = audio_mod.technical(path.read_bytes(), text, project.config["qc"])
    findings = [{"type": "техника", "detail": issue, "source": "technical"} for issue in tech["issues"]]
    transcripts = {}
    expected = lexicon.to_readable(text)
    for rec in recognizers:
        result = rec.transcribe(path)
        transcripts[rec.name] = " ".join(w["word"].strip() for w in result["words"])
        findings += [{**f, "source": rec.name} for f in compare(expected, result["words"], project.config["qc"])]
        findings += [{"type": "аномалия: " + a["type"], "detail": a["detail"], "start_s": a["time_s"],
                      "end_s": a["time_s"], "source": rec.name} for a in result.get("anomalies", [])]
        result["tokens"] = [t for w in result["words"] for t in words(w["word"])]
        transcripts[rec.name + ":tokens"] = result["tokens"]
    return {"technical": tech, "findings": findings, "transcripts": transcripts}


def run(project, sections: list[str] | None = None, force: bool = False, log=print, client=None) -> dict:
    release = project.active_release()
    jobs = load_jobs(project, release)
    if sections:
        jobs = [j for j in jobs if j["section"] in sections]
    ledger = Ledger(project)
    lexicon = pronounce.Lexicon(pronounce.load(project), include_proposed=True)
    recognizers = engines(project, client)
    names = ",".join(r.name for r in recognizers) or "none"
    folder = project.qc_dir(release) / "jobs"
    stats = {"checked": 0, "suspect": 0, "missing_audio": 0, "asr": names}
    previous = None
    for job in jobs:
        if groups.is_ref(job):  # часть группы ремарок: проверяется вместе с основным job
            continue
        out = folder / f"{job['id']}.json"
        if not ledger.has(job["request_hash"]):
            stats["missing_audio"] += 1
            previous = None
            continue
        meta = ledger.meta(job["request_hash"])
        if out.exists() and not force:
            result = read_json(out)
            if result.get("audio_sha256") == meta["audio_sha256"] and result.get("asr") == names:
                stats["checked"] += 1
                stats["suspect"] += result["status"] == "suspect"
                previous = (job, result)
                continue
        checked = check_audio(project, ledger.audio_path(job["request_hash"]), job["text"], lexicon, recognizers)
        findings = checked["findings"]
        if job.get("group_parts"):
            findings += check_group(project, ledger, job, lexicon, recognizers)
        # Повтор на стыке: конец предыдущего запроса прозвучал снова в начале этого.
        if previous and previous[0]["section"] == job["section"]:
            for rec in recognizers:
                tail = previous[1].get("transcripts", {}).get(rec.name + ":tokens", [])[-6:]
                head = checked["transcripts"].get(rec.name + ":tokens", [])[:6]
                expected_head = words(lexicon.to_readable(job["text"]))[:4]
                if len(tail) >= 4 and head[:4] == tail[-4:] and expected_head != tail[-4:]:
                    findings.append({"type": "повтор на стыке", "observed": " ".join(head[:4]), "source": rec.name})
        result = {"job_id": job["id"], "request_hash": job["request_hash"], "audio_sha256": meta["audio_sha256"],
                  "asr": names, "technical": checked["technical"], "findings": findings,
                  "transcripts": checked["transcripts"],
                  "status": "suspect" if findings else ("ok" if recognizers else "technical_ok"),
                  "checked_utc": now_utc()}
        write_json(out, result)
        previous = (job, result)
        stats["checked"] += 1
        stats["suspect"] += bool(findings)
        if findings:
            log(f"⚠ {job['id']}: " + "; ".join(sorted({f['type'] for f in findings})))
    write_report(project, release, jobs)
    return stats


def check_group(project, ledger, job: dict, lexicon, recognizers: list) -> list[dict]:
    """Группа ремарок: нарезается ли запись на части и звучит ли в каждой своя ремарка."""
    import tempfile
    try:
        spans = groups.cuts_for(project, ledger, job)
    except groups.CutError as exc:
        return [{"type": "нарезка группы", "detail": str(exc), "source": "groups"}]
    if not recognizers:
        return []
    samples, rate = audio_mod.read_wav(ledger.audio_path(job["request_hash"]))
    findings = []
    with tempfile.TemporaryDirectory() as tmp:
        for k, ((a, b), text) in enumerate(zip(spans, job["group_parts"])):
            path = Path(tmp) / f"{job['id']}-part{k}.wav"
            audio_mod.write_wav(path, samples[a:b], rate)
            for rec in recognizers:
                heard = words(" ".join(w["word"] for w in rec.transcribe(path)["words"]))
                want = words(lexicon.to_readable(text))
                if heard and want and difflib.SequenceMatcher(None, want, heard).ratio() < 0.6:
                    findings.append({"type": f"часть {k + 1} группы не совпала", "expected": text,
                                     "observed": " ".join(heard), "start_s": round(a / rate, 2),
                                     "end_s": round(b / rate, 2), "source": rec.name})
    return findings


def decisions_path(project, release: str) -> Path:
    return project.qc_dir(release) / "decisions.json"


def decide(project, job_id: str, verdict: str, note: str) -> None:
    release = project.active_release()
    path = decisions_path(project, release)
    data = read_json(path) if path.exists() else {}
    jobs = {j["id"]: j for j in load_jobs(project, release)}
    if job_id not in jobs:
        raise HarnessError(f"Нет job {job_id}")
    job_id = jobs[job_id].get("group_ref") or job_id  # решение по части группы — решение по группе
    ledger = Ledger(project)
    if verdict == "reject":
        ledger.reject(jobs[job_id], note or "отклонено при прослушивании")
    elif not ledger.has(jobs[job_id]["request_hash"]):
        raise HarnessError("Нет аудио для решения")
    data[job_id] = {"verdict": verdict, "note": note, "request_hash": jobs[job_id]["request_hash"],
                    "audio_sha256": None if verdict == "reject" else ledger.meta(jobs[job_id]["request_hash"])["audio_sha256"],
                    "utc": now_utc()}
    write_json(path, data)
    write_report(project, release, list(jobs.values()))


def open_issues(project, release: str, jobs: list[dict]) -> list[dict]:
    decisions = read_json(decisions_path(project, release)) if decisions_path(project, release).exists() else {}
    issues = []
    for job in jobs:
        path = project.qc_dir(release) / "jobs" / f"{job['id']}.json"
        if not path.exists():
            continue
        result = read_json(path)
        decision = decisions.get(job["id"])
        if result["status"] == "suspect" and not (decision and decision["verdict"] == "accept"
                                                  and decision["audio_sha256"] == result["audio_sha256"]):
            issues.append(result)
    return issues


def write_report(project, release: str, jobs: list[dict]) -> None:
    issues = open_issues(project, release, jobs)
    lines = [f"# Проверка аудио, релиз {release}", "",
             f"Открытых подозрений: {len(issues)}. Прослушайте каждое и отметьте: "
             "`python book.py qc accept <job> --note ...` (ложная тревога) или "
             "`python book.py qc reject <job> --note ...` (перегенерировать).", "",
             "| Job | Говорящий | Сигналы | Время | Текст |", "|---|---|---|---|---|"]
    by_id = {j["id"]: j for j in jobs}
    for result in issues:
        job = by_id[result["job_id"]]
        signals = "<br>".join(f"{f['type']}: {f.get('observed') or f.get('detail') or f.get('expected', '')}"[:120]
                              for f in result["findings"])
        times = ", ".join(f"{f['start_s']}–{f['end_s']}" for f in result["findings"] if "start_s" in f)
        text = job["text"][:140].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {job['id']} | {job['speaker_id']} | {signals.replace('|', '/')} | {times} | {text} |")
    write_text(project.qc_dir(release) / "REPORT.md", "\n".join(lines) + "\n")
