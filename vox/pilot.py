"""Пилот-калибровка: одна глава (или несколько коротких) до запуска всей книги.

  vox.py pilot plan --sections s003        пробный релиз (можно без утверждения) и цена
  vox.py pilot run --sections s003 --yes   синтез Flex + QC + отчёт (платно)
  vox.py pilot report                      пересобрать отчёт после правок и прослушивания

Отчёт pilot/CALIBRATION.md: фактический темп каждого голоса, технические и QC-находки,
что именно прослушать (по 2–3 фрагмента на голос, все места со style, тегами и словарём),
и куда вносить поправки. pilot/calibration.json хранит измеренный темп — `plan`
пересчитывает по нему длительность и цену всей книги.
"""
from __future__ import annotations

from collections import defaultdict

from . import qc as qc_mod
from . import synth
from .freeze import freeze, load_jobs
from .ledger import Ledger
from .util import HarnessError, read_json, write_json, write_text, now_utc

TARGET_CPS = (12.0, 17.0)  # комфортный темп русской аудиокниги, букв в секунду


def _letters(text: str) -> int:
    return sum(c.isalnum() for c in text)


def calibration(project) -> dict | None:
    path = project.root / "pilot" / "calibration.json"
    return read_json(path) if path.exists() else None


def prepare(project, sections: list[str]) -> dict:
    if not sections:
        raise HarnessError("Укажите раздел(ы) пилота: --sections s003 (глава с диалогами и рассказчиком)")
    result = freeze(project, allow_draft=True, include_proposed_pronunciation=True, sections_only=sections)
    write_json(project.root / "pilot" / "PILOT.json", {"sections": sections, "release": result["release"],
                                                       "created_utc": now_utc()})
    jobs = [j for j in load_jobs(project, result["release"]) if j["section"] in sections]
    return {"release": result["release"], "plan": synth.plan(project, jobs)}


def run(project, client, log=print, qc_client=None) -> dict:
    meta = read_json(project.root / "pilot" / "PILOT.json")
    if project.active_release() != meta["release"]:
        raise HarnessError("Активный релиз уже не пилотный: повторите pilot plan")
    jobs = [j for j in load_jobs(project) if j["section"] in meta["sections"]]
    pending = synth.enforce_budget(project, synth.plan(project, jobs)["pending"])
    stats = synth.run_flex(project, client, pending, log) if pending else {"ok": 0}
    qc_stats = qc_mod.run(project, meta["sections"], log=log, client=qc_client)
    report_path = report(project)
    return {"synth": stats, "qc": qc_stats, "report": str(report_path)}


def report(project):
    meta = read_json(project.root / "pilot" / "PILOT.json")
    release = meta["release"]
    jobs = [j for j in load_jobs(project, release) if j["section"] in meta["sections"]]
    ledger = Ledger(project)
    by_speaker = defaultdict(lambda: {"jobs": 0, "seconds": 0.0, "letters": 0, "voice": "", "issues": 0})
    listen = defaultdict(list)
    missing = 0
    for job in jobs:
        if job.get("group_ref"):
            continue
        if not ledger.has(job["request_hash"]):
            missing += 1
            continue
        tech = ledger.meta(job["request_hash"]).get("technical", {})
        row = by_speaker[job["speaker_id"]]
        row["voice"] = job["voice"]
        row["jobs"] += 1
        row["seconds"] += float(tech.get("duration_s") or 0)
        row["letters"] += _letters(job["text"])
        row["issues"] += bool(tech.get("issues"))
        reasons = []
        if job["style"]:
            reasons.append("подача: " + job["style"][:60])
        if "<" in job["text"]:
            reasons.append("теги")
        if job.get("pronunciation_applied"):
            reasons.append("словарь: " + ", ".join(str(a) for a in job["pronunciation_applied"])[:60])
        if job.get("effect"):
            reasons.append("эффект " + job["effect"].get("type", ""))
        if job.get("group_parts"):
            reasons.append(f"группа ремарок ({len(job['group_parts'])})")
        listen[job["speaker_id"]].append((job, reasons, float(tech.get("duration_s") or 0)))
    if not by_speaker:
        raise HarnessError("В пилоте ещё нет аудио: pilot run --yes")
    total_s = sum(r["seconds"] for r in by_speaker.values())
    total_letters = sum(r["letters"] for r in by_speaker.values())
    cps = total_letters / total_s if total_s else 0
    # chars/s по всем символам текста — именно так plan оценивает длительность
    chars = sum(len(j["text"]) for j in jobs if not j.get("group_ref") and ledger.has(j["request_hash"]))
    calib = {"release": release, "sections": meta["sections"], "measured_utc": now_utc(),
             "chars_per_second": round(chars / total_s, 2) if total_s else None,
             "letters_per_second": round(cps, 2), "audio_seconds": round(total_s, 1),
             "speakers": {k: {"voice": v["voice"], "jobs": v["jobs"], "seconds": round(v["seconds"], 1),
                              "letters_per_second": round(v["letters"] / v["seconds"], 2) if v["seconds"] else None}
                          for k, v in by_speaker.items()}}
    write_json(project.root / "pilot" / "calibration.json", calib)
    issues = qc_mod.open_issues(project, release, jobs)
    lines = [f"# Пилот-калибровка: {', '.join(meta['sections'])} (релиз {release})", "",
             f"Аудио: {total_s / 60:.1f} мин, средний темп {cps:.1f} букв/с "
             f"(комфортно {TARGET_CPS[0]:.0f}–{TARGET_CPS[1]:.0f}). Без аудио: {missing}. "
             f"Открытых подозрений QC: {len(issues)} (qc/{release}/REPORT.md).", "",
             "Слушайте на странице `python vox.py review --all` (кнопки принять/отклонить, затем `qc apply`).", "",
             "## Голоса", "", "| Роль | Голос | Запросов | Мин | Букв/с | Тех. замечаний | Вывод |", "|---|---|---|---|---|---|---|"]
    for role, r in sorted(by_speaker.items(), key=lambda kv: -kv[1]["seconds"]):
        rate = r["letters"] / r["seconds"] if r["seconds"] else 0
        verdict = ("слишком быстро — добавьте в карточку «unhurried»" if rate > TARGET_CPS[1] else
                   "медленно — проверьте паузы и затягивание" if rate < TARGET_CPS[0] else "темп в норме")
        lines.append(f"| {role} | {r['voice']} | {r['jobs']} | {r['seconds'] / 60:.1f} | {rate:.1f} | {r['issues']} | {verdict} |")
    lines += ["", "## Что прослушать", ""]
    for role, items in sorted(listen.items()):
        longest = sorted(items, key=lambda x: -x[2])[:2]
        flagged = [x for x in items if x[1]][:8]
        chosen = {x[0]["id"]: x for x in longest + flagged}
        lines.append(f"**{role}**")
        for job, reasons, seconds in chosen.values():
            lines.append(f"- `{job['id']}` ({seconds:.0f} с){': ' + '; '.join(reasons) if reasons else ''} — "
                         f"«{job['text'][:90].replace(chr(10), ' ')}…»")
        lines.append("")
    lines += ["## Вопросы к прослушиванию и куда вносить поправки", "",
              "| Вопрос | Если нет — что менять |", "|---|---|",
              "| Голоса героев различимы, рассказчик ровный? | `cast assign` другой голос; карточка роли (`cast.json`, card) |",
              "| Подача не переиграна, шёпот разборчив? | словарь подачи в `attribution/BOOK_NOTES.md`, затем `attribute prepare --force` для следующих глав |",
              "| Темп комфортный? | карточка голоса (unhurried / brisk); `chunking.max_words` меньше — ровнее темп |",
              "| Паузы между репликами и абзацами естественные? | `[pauses]` в book.toml (turn, paragraph, scene) — без пересинтеза, только `assemble` |",
              "| Ударения и имена верные? | `pron add` / `pron accept` и `pron trial` |",
              "| Ремарки «сказал он» не звучат оторванно? | `chunking.group_remarks = true` (группировка с нарезкой) |",
              "| Мысли звучат отлично от речи? | `[effects.thought]` wet/decay |",
              "| Склейки чистые? | `assemble`, затем `qc splices --asr` |", "",
              f"Темп пилота сохранён в pilot/calibration.json: `plan` для всей книги теперь считает длительность "
              f"по {calib['chars_per_second']} симв/с вместо 14.", ""]
    path = project.root / "pilot" / "CALIBRATION.md"
    write_text(path, "\n".join(lines))
    return path
