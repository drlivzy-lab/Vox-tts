"""Командная строка харнесса: python vox.py --book projects/<папка> <команда>."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import attribute, cast as cast_mod, pronounce, qc as qc_mod, script as script_mod, synth
from .config import EXAMPLE_TOML, api_key
from .project import Project
from .util import HarnessError, read_json, write_text


def _client(project):
    from .gemini import GeminiClient
    return GeminiClient(api_key(project.config, project.root), int(project.config["tts"]["request_timeout_s"]))


def _optional_client(project):
    """Клиент для облачной проверки (qc.asr = gemini); без ключа — None."""
    kinds = (project.config["qc"]["asr"], project.config["qc"].get("second_asr"))
    return _client(project) if "gemini" in kinds else None


def _need_yes(args, what: str) -> None:
    if not getattr(args, "yes", False):
        raise HarnessError(f"{what} — платная операция. Проверьте план и повторите с --yes")


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value, str) else value)


def _scripts(project) -> dict:
    return {s["id"]: script_mod.load_script(project, s["id"])
            for s in script_mod.load_sections(project)["sections"] if s["audio"]}


# ---------------- команды ----------------

def cmd_init(args):
    root = Path(args.dir).resolve()
    if (root / "book.toml").exists():
        raise HarnessError(f"{root} уже содержит book.toml")
    root.mkdir(parents=True, exist_ok=True)
    source = ""
    if args.source:
        src = Path(args.source)
        (root / "source").mkdir(exist_ok=True)
        shutil.copy2(src, root / "source" / src.name)
        source = f"source/{src.name}"
    kind = args.kind
    write_text(root / "book.toml", EXAMPLE_TOML.format(title=args.title or "", source=source, kind=kind,
                                                       mode="flex" if kind == "clips" else "batch"))
    if kind == "clips":
        (root / "clips").mkdir(exist_ok=True)
        example = Path(__file__).resolve().parents[1] / "docs" / "examples" / "clips_example.md"
        if example.exists():
            shutil.copy2(example, root / "clips" / "example.md")
    print(f"Проект создан: {root}\nДальше: python vox.py --book \"{root}\" next")


def cmd_ingest(args, project):
    from .ingest import ingest
    result = ingest(project)
    _print(result)
    if not project.config["book"]["title"] and result.get("title"):
        path = project.root / "book.toml"
        raw = path.read_text(encoding="utf-8")
        raw = raw.replace('title = ""', 'title = ' + json.dumps(result["title"], ensure_ascii=False), 1)
        raw = raw.replace('author = ""', 'author = ' + json.dumps(result.get("author", ""), ensure_ascii=False), 1)
        path.write_text(raw, encoding="utf-8")
    print("Проверьте text/sections.json: поле audio=false исключает раздел из озвучки.")


def cmd_draft(args, project):
    _print(script_mod.draft(project, args.force, args.sections))


def cmd_check(args, project):
    reports = script_mod.check_all(project, args.approved)
    bad = 0
    for r in reports:
        state = "OK" if not r["errors"] else f"{len(r['errors'])} ошибок"
        print(f"{r['section']}: {r['segments']} блоков, утверждено {r['approved']}, "
              f"без говорящего {r['unknown']} — {state}")
        for e in r["errors"][:args.limit]:
            print("   ✗", e)
        for w in r["warnings"][:args.limit]:
            print("   ⚠", w)
        bad += bool(r["errors"])
    cast_report = cast_mod.check(project, _scripts(project)) if project.cast_path.exists() else None
    if cast_report:
        for e in cast_report["errors"]:
            print("   ✗ каст:", e)
        for c in cast_report["conflicts"][:args.limit]:
            print("   ✗ голос:", c)
    return 1 if bad else 0


def cmd_approve(args, project):
    print(f"Утверждено блоков: {script_mod.approve(project, args.sections)}")


def cmd_attribute(args, project):
    if args.action == "prepare":
        paths = attribute.prepare(project, args.sections, args.force)
        print(f"Пакетов: {len(paths)} в attribution/packets. Ответы: attribution/responses/<имя>.json")
    elif args.action == "claim":
        taken = attribute.claim(project, args.worker or "agent", args.count)
        print("\n".join(f"attribution/packets/{n}.md -> attribution/responses/{n}.json" for n in taken)
              or "Свободных пакетов нет.")
    elif args.action == "unclaim":
        print(f"Снято захватов: {attribute.unclaim(project, args.names or [])}")
    elif args.action == "status":
        rows = attribute.status(project)
        counts = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        print(", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "Пакетов нет.")
        for r in rows:
            if r["state"] in ("claimed", "error"):
                print(f"  {r['packet']}: {r['state']}" + (f" ({r.get('worker')})" if r.get("worker") else "")
                      + ("; " + "; ".join(r.get("errors", [])) if r.get("errors") else ""))
    elif args.action == "roles":
        print(attribute.roles_report(project))
    elif args.action == "import":
        _print(attribute.import_responses(project, args.names))
    elif args.action == "run":
        _need_yes(args, "Атрибуция текстовой моделью")
        _print(attribute.run_gemini(project, _client(project), args.names, args.limit))


def cmd_cast(args, project):
    if args.action == "show":
        _print(cast_mod.load(project))
    elif args.action == "check":
        _print(cast_mod.check(project, _scripts(project)))
    elif args.action == "add":
        cast_mod.add_role(project, args.role, args.name, args.gender, args.alias or [], args.profile, args.description or "")
    elif args.action == "assign":
        cast_mod.assign(project, args.profile, args.voice, args.force)
    elif args.action == "accept":
        cast_mod.set_status(project, args.profile, "accepted")
    elif args.action == "design":
        _need_yes(args, "Создание голоса")
        print(cast_mod.design(project, _client(project), args.profile, args.force))
    elif args.action == "audition":
        _need_yes(args, "Пробы голоса")
        for path in cast_mod.audition(project, _client(project), args.profile):
            print(path)
    elif args.action == "import":
        _print(cast_mod.import_voice_plan(project, Path(args.path)))
    elif args.action == "voices":
        for voice in _client(project).list_voices(args.language):
            print(json.dumps({k: voice.get(k) for k in ("id", "display_name", "gender", "pitch", "description")},
                             ensure_ascii=False))


def cmd_pron(args, project):
    if args.action == "scan":
        _print(pronounce.scan(project, _scripts(project)))
        print("Список: pronunciation/candidates.md")
    elif args.action == "add":
        pronounce.add_entry(project, args.form, args.say, args.context, args.note or "")
    elif args.action in ("accept", "reject"):
        print(pronounce.set_status(project, args.form, args.action + "ed"))
    elif args.action == "trial":
        _need_yes(args, "Проба произношения")
        from .gemini import core_request
        from .util import write_bytes
        cast = cast_mod.load(project)
        voice = cast_mod.voice_for(cast, args.role or project.config["tts"]["narrator_role"])
        lex = pronounce.Lexicon(pronounce.load(project), include_proposed=True)
        text, applied = lex.apply(args.text)
        audio = _client(project).synthesize(core_request(project.config["tts"]["model"], voice, "", text),
                                            project.config["tts"]["service_tier"])
        path = project.root / "pronunciation" / "trials" / f"{args.name}.wav"
        write_bytes(path, audio)
        print(f"{path}\nОтправлено: {text}\nПодстановки: {applied}")


def cmd_freeze(args, project):
    from .freeze import freeze
    _print(freeze(project, args.allow_draft, args.proposed_pron, args.sections))


def _jobs(args, project):
    from .freeze import load_jobs
    return synth.select(load_jobs(project), args.sections, args.jobs)


def _show_plan(p):
    print(f"Запросов в выборке: {p['total']}; уже в кэше: {p['cached']}; к синтезу: {len(p['pending'])}; "
          f"заблокировано: {len(p['blocked'])}")
    print(f"Символов: {p['chars']:,}; ≈ {p['est_audio_min']} мин аудио; ≈ {p['est_input_tokens']:,} вх. / "
          f"{p['est_output_tokens']:,} вых. токенов")
    if p["est_cost_usd_batch_or_flex"] is not None:
        print(f"Оценка: ≈ ${p['est_cost_usd_batch_or_flex']} (Batch/Flex), ≈ ${p['est_cost_usd_standard']} (Standard)")
    else:
        print("Цена не оценена: заполните [pricing] в book.toml")
    for b in p["blocked"][:10]:
        print(f"   ⛔ {b['job']}: {b['reason']}")


def cmd_plan(args, project):
    _show_plan(synth.plan(project, _jobs(args, project)))


def cmd_synth(args, project):
    p = synth.plan(project, _jobs(args, project))
    _show_plan(p)
    allowed = synth.enforce_budget(project, p["pending"])
    if len(allowed) < len(p["pending"]):
        print(f"Лимит запуска: будет обработано {len(allowed)} из {len(p['pending'])} (см. [limits])")
    if not allowed:
        print("Нечего синтезировать.")
        return
    _need_yes(args, "Синтез")
    mode = args.mode or project.config["tts"]["mode"]
    client = _client(project)
    if mode == "flex":
        _print(synth.run_flex(project, client, allowed))
    else:
        label = args.label or ("-".join(args.sections) if args.sections and len(args.sections) <= 3 else "set")
        for name in synth.batch_prepare(project, allowed, label):
            _print(synth.batch_submit(project, client, name))
        print("Пакеты отправлены. Проверка: python book.py batch status --all; "
              "получение: python book.py batch collect --all")


def cmd_batch(args, project):
    names = args.names or []
    rows = synth.batches(project)
    if args.all:
        wanted = {"status": ("submitted",), "collect": ("submitted",)}.get(args.action, ())
        names = [r["name"] for r in rows if not wanted or r["status"] in wanted]
    if args.action == "list":
        for r in rows:
            print(f"{r['name']}: {r['status']}, запросов {r['requests']}" +
                  (f", {r.get('state')}" if r.get("state") else ""))
        return
    client = _client(project)
    for name in names:
        if args.action == "status":
            _print(synth.batch_status(project, client, name))
        elif args.action == "collect":
            try:
                _print(synth.batch_collect(project, client, name))
            except HarnessError as exc:
                print(f"{name}: {exc}")


def cmd_ledger(args, project):
    from .ledger import Ledger
    from .freeze import load_jobs
    ledger = Ledger(project)
    if args.action == "show":
        state = ledger.state()
        print(f"Запросов в журнале: {len(state)}; попыток: {sum(s['attempts'] for s in state.values())}; "
              f"неизвестный исход: {sum(1 for s in state.values() if s['unknown'])}; "
              f"в ожидании: {sum(1 for s in state.values() if s['open'])}")
    elif args.action == "resolve":
        jobs = {j["id"]: j for j in load_jobs(project)}
        if args.job not in jobs:
            raise HarnessError(f"Нет job {args.job}")
        ledger.record("resolve_unknown", jobs[args.job]["request_hash"], job_id=args.job,
                      note=args.note or "проверено вручную: запрос не выполнен")
        print("Отмечено. Повторная попытка будет учтена в лимите попыток.")


def cmd_qc(args, project):
    if args.action == "run":
        _print(qc_mod.run(project, args.sections, args.force, client=_optional_client(project)))
        print(f"Отчёт: qc/{project.active_release()}/REPORT.md")
    elif args.action == "splices":
        from . import splices
        recognizers = qc_mod.engines(project, _optional_client(project)) if args.asr else []
        _print(splices.run(project, args.sections, recognizers))
        print(f"Отчёт: qc/{project.active_release()}/SPLICES.md")
    elif args.action == "apply":
        from . import review
        if not args.job:
            raise HarnessError("Укажите файл решений: qc apply decisions-r001.json")
        _print(review.apply(project, Path(args.job)))
    elif args.action in ("accept", "reject"):
        qc_mod.decide(project, args.job, args.action, args.note or "")
        if args.action == "reject":
            print("Аудио перенесено в rejected/; следующий synth перегенерирует этот запрос.")


def cmd_assemble(args, project):
    from .assemble import assemble
    _print(assemble(project, args.sections, args.allow_missing))


def cmd_master(args, project):
    from .master import master
    _print(master(project, args.allow_open))


def cmd_review(args, project):
    from . import review
    path = review.build(project, args.sections, args.all)
    print(f"{path}\nОткройте в браузере; решения выгрузите кнопкой и примените: python vox.py qc apply <файл>")


def cmd_pilot(args, project):
    from . import pilot
    if args.action == "plan":
        result = pilot.prepare(project, args.sections)
        print(f"Пилотный релиз {result['release']} (черновой, только для пробы)")
        _show_plan(result["plan"])
        print("Дальше (платно, с разрешения): python vox.py pilot run --yes")
    elif args.action == "run":
        _need_yes(args, "Пилотный синтез")
        _print(pilot.run(project, _client(project), qc_client=_optional_client(project)))
    elif args.action == "report":
        print(pilot.report(project))


def cmd_voices(args, project):
    from . import library
    if args.action == "list":
        rows = library.expiring(args.expiring) if args.expiring is not None else \
            [{"name": k, **v} for k, v in sorted(library.load()["voices"].items())]
        for v in rows:
            print(f"{v['name']}: {v['voice']}" + (f", истекает {v['expires_utc'][:10]}" if v.get("expires_utc") else "")
                  + (f" — {v['description']}" if v.get("description") else ""))
        if not rows:
            print("Пусто." + (" Ничего не истекает." if args.expiring is not None else ""))
    elif args.action == "add":
        _print(library.register(args.name, args.voice, args.description or "", args.style or "",
                                args.gender or "neutral", force=args.force))
    elif args.action == "import-cast":
        _print(library.import_cast(project))
    elif args.action == "use":
        _print(library.use(project, args.name, args.profile))


def cmd_subs(args, project):
    from . import subs
    engines = qc_mod.engines(project, _optional_client(project))
    if not engines:
        raise HarnessError("Для субтитров по распознаванию задайте qc.asr в book.toml (gigaam, faster-whisper, ...)")
    text = Path(args.text_file).read_text(encoding="utf-8") if args.text_file else args.text
    if not text:
        raise HarnessError("Нужен текст: --text или --text-file")
    out = Path(args.out) if args.out else Path(args.audio).with_suffix(".srt")
    write_text(out, subs.from_audio(text, Path(args.audio), engines[0]))
    print(out)


def cmd_costs(args, project):
    from . import costs
    reports, path = costs.run(project, args.all)
    for r in reports:
        t = r["total"]
        line = f"{r['project']}: потрачено ≈ ${t['usd']:.2f} ({t['requests']} запросов, {t['seconds'] / 60:.1f} мин)"
        if r["remaining"]:
            line += f"; осталось ≈ ${r['remaining']['usd']:.2f} ({r['remaining']['requests']} запросов)"
        if t["unknown"]:
            line += f"; неизвестный исход: {t['unknown']} (≈ ${t['unknown_usd']:.2f})"
        print(line)
    print(path)


def cmd_next(args, project):
    from .advisor import steps
    result = steps(project)
    if args.json:
        _print(result)
        return
    for i, step in enumerate(result):
        print(("→ " if i == 0 else "  ") + step["command"] + "\n    " + step["why"])


def cmd_show_job(args, project):
    """Точный запрос, который уйдёт в API (Flex и Batch), и его состояние."""
    from .freeze import load_jobs
    from .gemini import batch_request, interactions_payload
    from .ledger import Ledger
    jobs = {j["id"]: j for j in load_jobs(project)}
    job = jobs.get(args.job)
    if not job:
        raise HarnessError(f"Нет job {args.job}")
    ledger = Ledger(project)
    _print({"job": {k: v for k, v in job.items() if k != "core"},
            "interactions_payload": interactions_payload(job["core"], project.config["tts"]["service_tier"]),
            "batch_request": batch_request(job["core"]),
            "cached": ledger.has(job["request_hash"]),
            "ledger": ledger.state().get(job["request_hash"])})


def _clip_blocks(args):
    from . import clips
    if getattr(args, "text", None):
        return "say", [{"id": args.id or "say", "voice": args.voice, "style": args.style or "", "text": args.text,
                        "takes": args.takes or 1}]
    path = Path(args.file)
    blocks = clips.load_blocks(path)
    if args.blocks:
        blocks = [b for b in blocks if b["id"] in args.blocks]
    return path.stem, blocks


def cmd_clips(args, project):
    from . import clips
    name, blocks = _clip_blocks(args)
    p = clips.plan(project, blocks, args.takes)
    print(f"Дублей всего: {len(p['requests'])}; в кэше: {p['cached']}; новых запросов: {len(p['pending'])}; "
          f"символов: {p['chars']}")
    for b in p["blocked"]:
        print(f"   ⛔ {b['job']}: {b['reason']}")
    if args.action == "plan":
        return
    if p["pending"]:
        _need_yes(args, "Озвучка клипов")
    formats = args.formats.split(",") if args.formats else None
    recognizer = None
    if (args.subs or project.config["clips"].get("subtitles")) == "asr":
        found = qc_mod.engines(project, _optional_client(project))
        if not found:
            raise HarnessError("Субтитры по распознаванию: задайте qc.asr в book.toml")
        recognizer = found[0]
    _print(clips.render(project, _client(project) if p["pending"] else None, name, blocks, args.takes,
                        formats, args.preset, recognizer=recognizer))


def cmd_say(args, project):
    args.action = "render"
    args.file = None
    args.blocks = None
    cmd_clips(args, project)


def cmd_publish(args, project):
    from . import publish as pub
    options = dict(cover=args.cover, images=args.image or [], cues=args.cue or [], description=args.description or "",
                   narrator=args.narrator or "", link=args.link)
    if args.m4b:
        if not (args.chapters and args.title):
            raise HarnessError("Для публикации файла нужны --chapters и --title")
        result = pub.publish(Path(args.library), audio=Path(args.m4b), chapters=pub.load_chapters(Path(args.chapters)),
                             book_id=args.id or pub.slug(args.title), title=args.title, author=args.author or "",
                             version_label=args.version_label or "", status=args.status or "", **options)
    else:
        if project is None:
            raise HarnessError("Укажите --book <проект> или --m4b <файл>")
        result = pub.publish_project(project, Path(args.library), book_id=args.id, title=args.title,
                                     author=args.author, version_label=args.version_label, **options)
    _print(result)
    print("Дальше: выложите папку библиотеки на сервер рядом с плеером (см. player/README.md)")


def cmd_status(args, project):
    from .freeze import load_jobs
    from .ledger import Ledger
    info = {"книга": project.config["book"]["title"]}
    if project.sections_path.exists():
        sections = script_mod.load_sections(project)["sections"]
        info["разделов"] = f"{sum(s['audio'] for s in sections)} к озвучке из {len(sections)}"
        reports = [r for r in script_mod.check_all(project)] if project.script_dir.exists() else []
        if reports:
            info["блоков"] = sum(r["segments"] for r in reports)
            info["утверждено"] = sum(r["approved"] for r in reports)
            info["без говорящего"] = sum(r["unknown"] for r in reports)
            info["разделов с ошибками"] = sum(bool(r["errors"]) for r in reports)
    if project.active_path.exists():
        release = project.active_release()
        jobs = load_jobs(project, release)
        ledger = Ledger(project)
        info["релиз"] = release
        info["jobs"] = len(jobs)
        info["в кэше"] = sum(ledger.has(j["request_hash"]) for j in jobs)
        info["открытых QC"] = len(qc_mod.open_issues(project, release, jobs))
        info["пакетов Batch в работе"] = sum(r["status"] == "submitted" for r in synth.batches(project))
    _print(info)


# ---------------- разбор аргументов ----------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vox.py", description="Харнесс озвучки на Gemini 3.8 Flash TTS")
    p.add_argument("--book", default=os.environ.get("VOX_PROJECT", "."),
                   help="папка проекта (с book.toml); по умолчанию VOX_PROJECT или текущая")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="создать проект книги")
    s.add_argument("dir"); s.add_argument("--source"); s.add_argument("--title")
    s.add_argument("--kind", choices=["book", "clips"], default="book")
    s = sub.add_parser("next", help="следующий шаг с готовой командой"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("show-job", help="точный payload запроса"); s.add_argument("job")
    sub.add_parser("ingest", help="извлечь абзацы из исходника")
    s = sub.add_parser("draft", help="черновой сценарий: речь/ремарки")
    s.add_argument("--force", action="store_true"); s.add_argument("--sections", nargs="*")
    s = sub.add_parser("check", help="проверить сценарий и каст")
    s.add_argument("--approved", action="store_true", help="требовать review_status: approved")
    s.add_argument("--limit", type=int, default=15)
    s = sub.add_parser("approve", help="утвердить блоки разделов"); s.add_argument("sections", nargs="+")

    s = sub.add_parser("attribute", help="атрибуция ИИ: prepare | claim | status | import | roles | run")
    s.add_argument("action", choices=["prepare", "claim", "unclaim", "status", "import", "roles", "run"])
    s.add_argument("--worker", help="имя агента-исполнителя для claim"); s.add_argument("--count", type=int, default=1)
    s.add_argument("--sections", nargs="*"); s.add_argument("--names", nargs="*")
    s.add_argument("--force", action="store_true"); s.add_argument("--limit", type=int, default=10)
    s.add_argument("--yes", action="store_true")

    s = sub.add_parser("cast", help="роли и голоса")
    s.add_argument("action", choices=["show", "check", "add", "assign", "accept", "design", "audition", "import", "voices"])
    s.add_argument("--role"); s.add_argument("--name"); s.add_argument("--gender", default="neutral")
    s.add_argument("--alias", action="append"); s.add_argument("--profile"); s.add_argument("--description")
    s.add_argument("--voice"); s.add_argument("--path"); s.add_argument("--language")
    s.add_argument("--force", action="store_true"); s.add_argument("--yes", action="store_true")

    s = sub.add_parser("pron", help="словарь произношения")
    s.add_argument("action", choices=["scan", "add", "accept", "reject", "trial"])
    s.add_argument("--form"); s.add_argument("--say"); s.add_argument("--context"); s.add_argument("--note")
    s.add_argument("--text"); s.add_argument("--role"); s.add_argument("--name", default="trial")
    s.add_argument("--yes", action="store_true")

    s = sub.add_parser("freeze", help="заморозить релиз для синтеза")
    s.add_argument("--allow-draft", action="store_true", help="пробный релиз из неутверждённых блоков")
    s.add_argument("--proposed-pron", action="store_true", help="применить и непрослушанные записи словаря")
    s.add_argument("--sections", nargs="*")

    for name in ("plan", "synth"):
        s = sub.add_parser(name, help="план синтеза" if name == "plan" else "синтез (платно, нужен --yes)")
        s.add_argument("--sections", nargs="*"); s.add_argument("--jobs", nargs="*")
        if name == "synth":
            s.add_argument("--mode", choices=["batch", "flex"]); s.add_argument("--label")
            s.add_argument("--yes", action="store_true")

    s = sub.add_parser("batch", help="пакеты Batch: list | status | collect")
    s.add_argument("action", choices=["list", "status", "collect"]); s.add_argument("names", nargs="*")
    s.add_argument("--all", action="store_true")

    s = sub.add_parser("ledger", help="журнал запросов: show | resolve")
    s.add_argument("action", choices=["show", "resolve"]); s.add_argument("--job"); s.add_argument("--note")

    s = sub.add_parser("qc", help="проверка аудио: run | splices | accept | reject")
    s.add_argument("action", choices=["run", "splices", "accept", "reject", "apply"])
    s.add_argument("job", nargs="?", help="job для accept/reject; файл решений для apply")
    s.add_argument("--asr", action="store_true", help="для splices: распознавать окна вокруг стыков")
    s.add_argument("--note"); s.add_argument("--sections", nargs="*"); s.add_argument("--force", action="store_true")

    s = sub.add_parser("assemble", help="собрать главы")
    s.add_argument("--sections", nargs="*"); s.add_argument("--allow-missing", action="store_true")
    s = sub.add_parser("master", help="мастер M4B"); s.add_argument("--allow-open", action="store_true")
    sub.add_parser("status", help="сводка по проекту")
    s = sub.add_parser("review", help="HTML-страница прослушивания с принять/отклонить")
    s.add_argument("--all", action="store_true", help="все запросы, не только подозрительные")
    s.add_argument("--sections", nargs="*")
    s = sub.add_parser("pilot", help="пилот-калибровка: plan | run | report")
    s.add_argument("action", choices=["plan", "run", "report"]); s.add_argument("--sections", nargs="*")
    s.add_argument("--yes", action="store_true")
    s = sub.add_parser("voices", help="общая библиотека голосов: list | add | import-cast | use")
    s.add_argument("action", choices=["list", "add", "import-cast", "use"])
    s.add_argument("--name"); s.add_argument("--voice"); s.add_argument("--style"); s.add_argument("--description")
    s.add_argument("--gender"); s.add_argument("--profile"); s.add_argument("--expiring", type=int)
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("subs", help="субтитры SRT по распознаванию для готового WAV")
    s.add_argument("audio"); s.add_argument("--text"); s.add_argument("--text-file"); s.add_argument("-o", "--out")
    s = sub.add_parser("costs", help="панель затрат: costs.html")
    s.add_argument("--all", action="store_true", help="все проекты в projects/")

    s = sub.add_parser("clips", help="короткие тексты: plan | render (сценарий clips/*.md)")
    s.add_argument("action", choices=["plan", "render"]); s.add_argument("file")
    s.add_argument("--blocks", nargs="*"); s.add_argument("--takes", type=int)
    s.add_argument("--formats", help="wav,mp3,m4a,opus,flac"); s.add_argument("--preset", help="social|podcast|audiobook|acx|broadcast")
    s.add_argument("--subs", choices=["approx", "asr"], help="субтитры: оценка или по распознаванию (qc.asr)")
    s.add_argument("--yes", action="store_true")
    s = sub.add_parser("say", help="озвучить одну фразу в проекте (с кэшем и журналом)")
    s.add_argument("text"); s.add_argument("--voice", required=True); s.add_argument("--style")
    s.add_argument("--id"); s.add_argument("--takes", type=int)
    s.add_argument("--formats"); s.add_argument("--preset"); s.add_argument("--subs", choices=["approx", "asr"])
    s.add_argument("--yes", action="store_true")
    s = sub.add_parser("publish", help="выложить готовую книгу в библиотеку плеера (player/)")
    s.add_argument("--library", required=True, help="папка библиотеки (library.json + books/)")
    s.add_argument("--m4b", help="готовый файл книги вместо мастера проекта (.m4b/.m4a/.mp3)")
    s.add_argument("--chapters", help="главы для --m4b: chapters.ffmeta или manifest.json")
    s.add_argument("--id", help="латинский id книги (по умолчанию из названия)")
    s.add_argument("--title"); s.add_argument("--author"); s.add_argument("--narrator")
    s.add_argument("--description"); s.add_argument("--status"); s.add_argument("--cover", help="обложка jpg/png/webp")
    s.add_argument("--image", action="append", help="иллюстрация: ключ=путь|Подпись")
    s.add_argument("--cue", action="append", help="показ иллюстрации: ключ@глава, ключ@1..3, ключ@-1")
    s.add_argument("--version-label", help="префикс версии, например master-v3")
    s.add_argument("--link", action="store_true", help="жёсткая ссылка на аудио вместо копии (экономит место)")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            cmd_init(args)
            return 0
        from .env import load as load_env
        load_env(Path(args.book))
        try:
            project = Project.open(args.book)
        except HarnessError:
            if args.command not in ("voices", "costs", "publish"):
                raise
            project = None  # библиотека голосов и общая панель затрат работают и вне проекта
            if args.command == "costs" and not args.all or args.command == "voices" and args.action in ("import-cast", "use"):
                raise
            if args.command == "publish" and not args.m4b:
                raise
        for extra in ([] if project is None else project.config.get("vendor_paths", [])):
            sys.path.insert(0, str((project.root / extra).resolve()))
        handler = globals()["cmd_" + args.command.replace("-", "_")]
        return handler(args, project) or 0
    except HarnessError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
