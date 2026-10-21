"""`vox next`: что делать дальше. Смотрит на файлы проекта и называет ближайший шаг
с готовой командой — агенту не нужно держать конвейер в голове."""
from __future__ import annotations

from . import qc as qc_mod
from . import script as script_mod
from . import synth
from .util import read_json, sha_file


def _stale_release(project) -> bool:
    if not project.active_path.exists():
        return True
    manifest = read_json(project.release_dir() / "manifest.json")
    if manifest.get("status") != "frozen":  # пилотный/черновой релиз — книгу нужно заморозить целиком
        return True
    sections = {s["id"] for s in script_mod.load_sections(project)["sections"] if s["audio"]}
    if set(manifest["inputs"]["scripts"]) != sections:
        return True
    for sid, digest in manifest["inputs"]["scripts"].items():
        path = project.script_dir / f"{sid}.md"
        if not path.exists() or sha_file(path) != digest:
            return True
    if project.cast_path.exists() and sha_file(project.cast_path) != manifest["inputs"]["cast"]:
        return True
    lex = sha_file(project.lexicon_path) if project.lexicon_path.exists() else None
    return lex != manifest["inputs"]["lexicon"]


def steps(project) -> list[dict]:
    out = []
    add = lambda command, why: out.append({"command": command, "why": why})
    from . import cast as cast_mod, library
    if project.cast_path.exists():
        voices = {p.get("voice") for p in cast_mod.load(project)["profiles"].values()}
        for entry in library.expiring(30):
            if entry["voice"] in voices:
                add(f"python vox.py voices list --expiring 30",
                    f"голос {entry['name']} истекает {entry['expires_utc'][:10]}: дозапишите книгу или пересоздайте голос")
    if project.config["project"]["kind"] == "clips":
        add("python vox.py clips plan clips/<файл>.md",
            "проект коротких текстов: пишите clips/*.md (docs/SCRIPT_GUIDE.md), затем clips render --yes")
        return out
    if not project.sections_path.exists():
        add("python vox.py ingest", "исходник ещё не разобран на абзацы")
        return out
    sections = [s for s in script_mod.load_sections(project)["sections"] if s["audio"]]
    missing = [s["id"] for s in sections if not script_mod.script_path(project, s["id"]).exists()]
    if missing:
        add("python vox.py draft", f"нет сценариев для {len(missing)} разделов")
        return out
    reports = script_mod.check_all(project)
    unknown = sum(r["unknown"] for r in reports)
    packets = project.attribution_dir / "packets"
    responses = project.attribution_dir / "responses"
    applied = project.attribution_dir / "applied"
    pending_packets = sorted(p.stem for p in packets.glob("*.md")) if packets.exists() else []
    unanswered = [n for n in pending_packets if not (responses / f"{n}.json").exists()]
    unapplied = [n for n in pending_packets if (responses / f"{n}.json").exists() and not (applied / f"{n}.json").exists()]
    if unapplied:
        add("python vox.py attribute import", f"есть {len(unapplied)} ответов ИИ, ещё не применённых")
    if unanswered:
        add(f"ответить на пакет attribution/packets/{unanswered[0]}.md -> attribution/responses/{unanswered[0]}.json",
            f"пакетов без ответа: {len(unanswered)}")
    nothing_reviewed = not any(r["approved"] for r in reports) and not applied.exists()
    if not pending_packets and (unknown or nothing_reviewed):
        add("python vox.py attribute prepare", "сценарии ещё не размечены ИИ (говорящие, режимы, подача)")
    if out:
        return out
    review = project.attribution_dir / "REVIEW.md"
    if review.exists() and any(s.get("confidence") in ("medium", "low")
                               for r in sections for s in script_mod.load_script(project, r["id"])):
        add("проверить attribution/REVIEW.md", "есть блоки с неуверенной атрибуцией")
    from . import cast as cast_mod
    scripts = {s["id"]: script_mod.load_script(project, s["id"]) for s in sections}
    cast_report = cast_mod.check(project, scripts)
    if cast_report["errors"] or cast_report["conflicts"]:
        add("python vox.py cast check", "каст не готов: " + "; ".join((cast_report["errors"] + cast_report["conflicts"])[:3]))
        return out
    if not (project.root / "pronunciation" / "candidates.md").exists():
        add("python vox.py pron scan", "словарь произношения ещё не собирался")
        return out
    errors = [e for r in reports for e in r["errors"]]
    if errors:
        add("python vox.py check", f"в сценарии {len(errors)} ошибок, например: {errors[0]}")
        return out
    unapproved = [r["section"] for r in reports if r["approved"] < r["segments"]]
    if unapproved:
        add(f"python vox.py approve {' '.join(unapproved[:5])}",
            f"не утверждено разделов: {len(unapproved)} (после вычитки; для пробы — freeze --allow-draft)")
        return out
    if _stale_release(project):
        add("python vox.py freeze", "сценарий, каст или словарь изменились после последней заморозки")
        return out
    from .freeze import load_jobs
    jobs = load_jobs(project)
    batches = synth.batches(project)
    submitted = [b["name"] for b in batches if b["status"] == "submitted"]
    if submitted:
        add("python vox.py batch status --all", f"в работе пакетов Batch: {len(submitted)}")
        add("python vox.py batch collect --all", "забрать готовые")
        return out
    plan = synth.plan(project, jobs)
    if plan["pending"] and len(plan["pending"]) > 40 and not (project.root / "pilot" / "calibration.json").exists():
        add("python vox.py pilot plan --sections <глава с диалогами>",
            "перед синтезом всей книги — пилот на одной главе: темп, голоса, подача, паузы, точная цена")
    if plan["pending"]:
        add("python vox.py plan", f"к синтезу {len(plan['pending'])} запросов: проверьте план и стоимость")
        add("python vox.py synth --yes", "платно; нужно разрешение пользователя")
        return out
    if plan["blocked"]:
        add("python vox.py ledger show", f"заблокировано запросов: {len(plan['blocked'])}")
    release = project.active_release()
    checked = project.qc_dir(release) / "jobs"
    own = [j for j in jobs if not j.get("group_ref")]
    if not checked.exists() or len(list(checked.glob("*.json"))) < len(own):
        add("python vox.py qc run", "не всё аудио проверено")
        return out
    issues = qc_mod.open_issues(project, release, jobs)
    if issues:
        add(f"прослушать qc/{release}/REPORT.md; затем qc accept|reject <job>",
            f"открытых подозрений: {len(issues)}")
        return out
    chapters = project.chapters_dir(release)
    if not chapters.exists() or len(list(chapters.glob("*.json"))) < len({j['section'] for j in jobs}):
        add("python vox.py assemble", "главы не собраны")
        return out
    if not (project.qc_dir(release) / "SPLICES.md").exists():
        add("python vox.py qc splices", "склейки глав не проверены")
        return out
    if not (project.master_dir(release) / "manifest.json").exists():
        add("python vox.py master", "мастер не собран")
        return out
    add("прослушивание и художественная приёмка", "технически всё готово")
    return out
