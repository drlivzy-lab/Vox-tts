"""Атрибуция реплик и режиссура с помощью ИИ.

Два способа, один формат ответа:
  agent  — `attribute prepare` пишет пакеты attribution/packets/*.md; ИИ-агент
           (Codex, Claude и т. п.) кладёт ответы в attribution/responses/*.json.
  gemini — `attribute run --yes` отправляет те же пакеты текстовой модели.
`attribute import` строго проверяет ответы и только потом меняет сценарий:
слова не могут появиться или пропасть (разрешены CAPS-акцент, ё, пунктуация и теги
из таблицы Google), неизвестные роли не проходят. Блоки с уверенностью ниже high
попадают в attribution/REVIEW.md для человека.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import cast as cast_mod
from .script import ALLOWED_TAGS, MODES, load_script, load_sections, save_script
from .util import HarnessError, append_jsonl, check_id, letters, read_json, write_json, write_text, now_utc

INSTRUCTIONS = """Ты — режиссёр русской многоголосой аудиокниги на Gemini 3.8 Flash TTS.
Для каждого блока определи говорящего, режим речи и короткую подачу. Подробный справочник:
docs/SCRIPT_GUIDE.md (если доступен).

## Кто говорит
1. Авторские ремарки («сказал он», описания) читает рассказчик {narrator}, mode: narration.
2. Реплика вслух — mode: spoken, голос персонажа. Возврат к реплике после ремарки — тот же персонаж.
3. Цепочка реплик без ремарок обычно чередует двух собеседников. Проверь по смыслу:
   обращения по имени, ответы на вопросы, кто что знает, пол по глаголам («сказала»).
4. Мысли — mode: thought, голос персонажа (курсив в исходнике — сильный признак, см. note).
   Несобственно-прямая речь без кавычек и курсива — рассказчик.
5. Письма, записки, надписи — mode: written (голос автора текста, спокойно).
   Беззвучное (губами, жестом) — mode: mouthed, не озвучивается.
6. Заголовки — heading, эпиграфы — epigraph, читает рассказчик.
7. Безымянные эпизодические — роль EP_<КОРОТКО> (EP_GUARD, EP_OLD_WOMAN); перечисли в new_roles.
   Новых постоянных персонажей тоже добавь в new_roles (ID латиницей в ВЕРХНЕМ_РЕГИСТРЕ).

## Подача (style)
8. 3–12 английских слов о подаче ИМЕННО этой реплики: эмоция, темп, громкость, отношение.
   Не описывай тембр, возраст, пол, акцент — это свойства голоса. Нейтрально — пустая строка.
9. Дозируй: большинство реплик — пустой или мягкий style. Крик, плач, шёпот — только если это в тексте.
   Шёпот передавай как "Speak quietly in a voiced tone" (настоящий шёпот плохо разборчив).
   Одинаковую подачу повторяй дословно — так звучание стабильнее.
10. У рассказчика style всегда пустой.

## Текст
11. Слова менять нельзя. Можно (поле text): CAPS для одного логически ударного слова,
    восстановить ё, поправить пунктуацию для интонации, вставить теги из списка:
    {tags}.
    Теги — редко, только когда событие есть в тексте («вздохнул» → <sigh> у персонажа).
12. Если блок смешивает речь и ремарку, верни split: те же слова по частям в исходном порядке.

## Уверенность
13. confidence: high — говорящий явно назван или однозначен; medium — вывод по контексту;
    low — догадка. evidence — короткое основание («ремарка: сказала Сири», «ответ на вопрос Вара»).

Верни только JSON:
{{"segments": [{{"id": "...", "speaker_id": "...", "mode": "...", "style": "...",
               "confidence": "high|medium|low", "evidence": "...", "text": "(только если правишь)"}}],
 "splits": [{{"id": "...", "pieces": [{{"text": "...", "speaker_id": "...", "mode": "...", "style": "...",
             "confidence": "...", "evidence": "..."}}]}}],
 "new_roles": [{{"id": "...", "name": "...", "gender": "male|female|neutral", "aliases": ["..."],
                "description": "возраст, характер, манера речи — для подбора голоса"}}],
 "notes": ["замечания для человека"]}}
Каждый блок пакета должен встретиться ровно один раз: в segments или в splits.

## Пример
Вход:
{{"id": "s003-0010", "mode": "spoken", "speaker_id": "UNKNOWN", "text": "Ты опоздал."}}
{{"id": "s003-0011", "mode": "narration", "speaker_id": "{narrator}", "text": "сказал Вар, не поднимая глаз."}}
{{"id": "s003-0012", "mode": "spoken", "speaker_id": "UNKNOWN", "text": "Дорога была длинной."}}
Ответ:
{{"segments": [
 {{"id": "s003-0010", "speaker_id": "VAHR", "mode": "spoken", "style": "Cold, flat reproach.", "confidence": "high", "evidence": "ремарка: сказал Вар"}},
 {{"id": "s003-0011", "speaker_id": "{narrator}", "mode": "narration", "style": "", "confidence": "high", "evidence": "ремарка"}},
 {{"id": "s003-0012", "speaker_id": "VASHER", "mode": "spoken", "style": "Tired, unapologetic.", "confidence": "medium", "evidence": "ответ на упрёк Вара; в сцене двое"}}]}}
"""


def _packet_name(section_id: str, index: int) -> str:
    return f"{section_id}-{index:02}"


def prepare(project, sections: list[str] | None = None, force: bool = False) -> list[Path]:
    cast = cast_mod.load(project)
    narrator = project.config["tts"]["narrator_role"]
    size = int(project.config["llm"]["packet_max_segments"])
    out_dir = project.attribution_dir / "packets"
    notes_path = project.attribution_dir / "BOOK_NOTES.md"
    notes = notes_path.read_text(encoding="utf-8") if notes_path.exists() else ""
    written = []
    roles = [{"id": rid, "name": r.get("name"), "gender": r.get("gender"), "aliases": r.get("aliases", []),
              "description": r.get("description", "")} for rid, r in cast["roles"].items()]
    instructions = INSTRUCTIONS.format(narrator=narrator, tags=", ".join(f"<{t}>" for t in sorted(ALLOWED_TAGS)))
    for section in load_sections(project)["sections"]:
        if not section["audio"] or (sections and section["id"] not in sections):
            continue
        segments = load_script(project, section["id"])
        for index, start in enumerate(range(0, len(segments), size), 1):
            name = _packet_name(section["id"], index)
            path = out_dir / f"{name}.md"
            if path.exists() and not force:
                continue
            chunk = segments[start:start + size]
            context = segments[max(0, start - 12):start]
            body = [f"# Пакет атрибуции {name}: {section['title']}", "", instructions,
                    "## Роли (cast.json)", "", "```json", json.dumps(roles, ensure_ascii=False, indent=1), "```", ""]
            if notes:
                body += ["## Заметки о книге (attribution/BOOK_NOTES.md)", "", notes.strip(), ""]
            if context:
                body += ["## Предыдущий контекст (не размечать)", "", "```jsonl"]
                body += [json.dumps({"id": s["id"], "speaker_id": s.get("speaker_id"), "text": s["text"]},
                                    ensure_ascii=False) for s in context]
                body += ["```", ""]
            body += ["## Блоки для разметки", "", "```jsonl"]
            for s in chunk:
                row = {"id": s["id"], "mode": s.get("mode"), "speaker_id": s.get("speaker_id"), "text": s["text"]}
                if s.get("note"):
                    row["note"] = s["note"]
                body.append(json.dumps(row, ensure_ascii=False))
            body += ["```", "", f"Ответ сохранить в attribution/responses/{name}.json", ""]
            write_text(path, "\n".join(body))
            write_json(out_dir / f"{name}.ids.json", {"section": section["id"], "ids": [s["id"] for s in chunk]})
            written.append(path)
    return written


def _check_text(new: str, old: str, where: str) -> list[str]:
    errors = []
    if letters(new) != letters(old):
        errors.append(f"{where}: изменены слова (разрешены только CAPS, ё, пунктуация и теги)")
    for tag in re.findall(r"<([^<>]{1,40})>", new):
        if tag.strip().lower() not in ALLOWED_TAGS:
            errors.append(f"{where}: тег <{tag}> не из таблицы Google")
    if re.search(r"\[[^\]]+\]|```|\|", new):
        errors.append(f"{where}: в тексте служебные скобки или | — они прозвучат вслух")
    return errors


def _validate(response: dict, ids: list[str], originals: dict[str, dict], known_roles: set[str],
              narrator: str) -> list[str]:
    errors = []
    new_roles = {r.get("id") for r in response.get("new_roles", [])}
    roles = known_roles | new_roles

    def check_row(row: dict, where: str) -> None:
        silent = row.get("mode") in ("mouthed", "skip")
        if row.get("speaker_id") not in roles and not silent:
            errors.append(f"{where}: неизвестная роль {row.get('speaker_id')}")
        if row.get("mode") not in MODES:
            errors.append(f"{where}: неизвестный mode {row.get('mode')}")
        if row.get("speaker_id") == narrator and row.get("style"):
            errors.append(f"{where}: у рассказчика style должен быть пустым")
        style = row.get("style") or ""
        if len(style) > 200 or re.search(r"[<>`]", style):
            errors.append(f"{where}: слишком длинный или подозрительный style")
        if row.get("confidence") not in (None, "high", "medium", "low"):
            errors.append(f"{where}: confidence — high, medium или low")

    seen = []
    for row in response.get("segments", []):
        sid = row.get("id")
        seen.append(sid)
        check_row(row, str(sid))
        if row.get("text") and sid in originals:
            errors += _check_text(row["text"], originals[sid]["text"], sid)
    for split in response.get("splits", []):
        sid = split.get("id")
        seen.append(sid)
        pieces = split.get("pieces") or []
        if sid in originals:
            errors += _check_text(" ".join(p.get("text", "") for p in pieces), originals[sid]["text"], f"{sid} split")
        for k, piece in enumerate(pieces):
            check_row(piece, f"{sid}[{k}]")
    for role in response.get("new_roles", []):
        try:
            check_id(role.get("id", ""), "ID роли")
        except HarnessError as exc:
            errors.append(str(exc))
    if sorted(seen) != sorted(ids):
        missing = set(ids) - set(seen)
        extra = [i for i in seen if i not in ids or seen.count(i) > 1]
        errors.append(f"покрытие блоков нарушено: нет {sorted(missing)[:5]}, лишние/повторы {sorted(set(extra))[:5]}")
    return errors


def import_responses(project, names: list[str] | None = None) -> dict:
    packets = project.attribution_dir / "packets"
    responses = project.attribution_dir / "responses"
    narrator = project.config["tts"]["narrator_role"]
    sections = {s["id"]: s for s in load_sections(project)["sections"]}
    done = {"applied": 0, "segments": 0, "splits": 0, "new_roles": 0, "to_review": 0, "errors": {}}
    for ids_path in sorted(packets.glob("*.ids.json")):
        name = ids_path.name[:-len(".ids.json")]
        if names and name not in names:
            continue
        response_path = responses / f"{name}.json"
        marker = project.attribution_dir / "applied" / f"{name}.json"
        if not response_path.exists() or marker.exists():
            continue
        meta = read_json(ids_path)
        try:
            response = read_json(response_path)
        except ValueError as exc:
            done["errors"][name] = [f"ответ не JSON: {exc}"]
            continue
        cast = cast_mod.load(project)
        segments = load_script(project, meta["section"])
        originals = {s["id"]: s for s in segments}
        errors = _validate(response, meta["ids"], originals, set(cast["roles"]), narrator)
        if errors:
            done["errors"][name] = errors
            continue
        for role in response.get("new_roles", []):
            if role["id"] not in cast["roles"]:
                cast["roles"][role["id"]] = {"name": role.get("name", role["id"]),
                                             "aliases": role.get("aliases", []),
                                             "gender": role.get("gender", "neutral"),
                                             "description": role.get("description", ""), "profile": None}
                done["new_roles"] += 1
        cast_mod.save(project, cast)
        updates = {r["id"]: r for r in response.get("segments", [])}
        splits = {s["id"]: s["pieces"] for s in response.get("splits", [])}

        def apply(seg: dict, row: dict) -> dict:
            seg = {**seg, "speaker_id": row["speaker_id"], "mode": row["mode"], "style": row.get("style") or "",
                   "confidence": row.get("confidence")}
            if row.get("text"):
                seg["text"] = row["text"].strip()
            if row.get("evidence"):
                seg["note"] = ((seg.get("note") + "; ") if seg.get("note") else "") + "ИИ: " + row["evidence"][:200]
            return seg

        result = []
        for seg in segments:
            if seg["id"] in updates:
                result.append(apply(seg, updates[seg["id"]]))
                done["segments"] += 1
            elif seg["id"] in splits:
                for k, piece in enumerate(splits[seg["id"]]):
                    result.append(apply({**seg, "id": f"{seg['id']}{chr(97 + k)}"}, piece))
                done["splits"] += 1
            else:
                result.append(seg)
        save_script(project, sections[meta["section"]], result)
        write_json(marker, {"applied_utc": now_utc(), "notes": response.get("notes", [])})
        append_jsonl(project.attribution_dir / "log.jsonl", {"packet": name, "utc": now_utc(),
                                                             "notes": response.get("notes", [])})
        done["applied"] += 1
    done["to_review"] = write_review(project)
    errors_path = project.attribution_dir / "import_errors.json"
    previous = read_json(errors_path) if errors_path.exists() else {}
    previous = {k: v for k, v in previous.items() if not (project.attribution_dir / "applied" / f"{k}.json").exists()}
    previous.update(done["errors"])
    write_json(errors_path, previous)
    if done["applied"]:
        roles_report(project)
    return done


def write_review(project) -> int:
    """Список для человека: блоки с уверенностью medium/low и заметки ИИ."""
    lines = ["# Проверка атрибуции", "",
             "Блоки, где ИИ не уверен. Исправьте speaker_id в script/<раздел>.md при необходимости, "
             "затем поставьте confidence: high или утвердите раздел.", "",
             "| Блок | Говорящий | Уверенность | Основание | Текст |", "|---|---|---|---|---|"]
    count = 0
    for section in load_sections(project)["sections"]:
        if not section["audio"] or not (project.script_dir / f"{section['id']}.md").exists():
            continue
        for seg in load_script(project, section["id"]):
            if seg.get("confidence") in ("medium", "low"):
                count += 1
                text = seg["text"][:120].replace("|", "/").replace("\n", " ")
                note = (seg.get("note") or "").replace("|", "/")
                lines.append(f"| {seg['id']} | {seg.get('speaker_id')} | {seg['confidence']} | {note} | {text} |")
    notes = [json.loads(l) for l in (project.attribution_dir / "log.jsonl").read_text(encoding="utf-8").splitlines()
             if l.strip()] if (project.attribution_dir / "log.jsonl").exists() else []
    if any(n["notes"] for n in notes):
        lines += ["", "## Заметки ИИ", ""]
        lines += [f"- {n['packet']}: {note}" for n in notes for note in n["notes"]]
    write_text(project.attribution_dir / "REVIEW.md", "\n".join(lines) + "\n")
    return count


def run_gemini(project, client, names: list[str] | None = None, limit: int = 10) -> list[str]:
    model = project.config["llm"]["model"]
    if not model:
        raise HarnessError("Укажите llm.model в book.toml (текстовая модель Gemini)")
    packets = project.attribution_dir / "packets"
    responses = project.attribution_dir / "responses"
    done = []
    for path in sorted(packets.glob("*.md")):
        name = path.stem
        if (names and name not in names) or (responses / f"{name}.json").exists():
            continue
        if len(done) >= limit:
            break
        result = client.generate_json(model, path.read_text(encoding="utf-8"))
        write_json(responses / f"{name}.json", result)
        done.append(name)
    return done


# ---------------- параллельная работа нескольких агентов ----------------

def _packets(project) -> list[str]:
    folder = project.attribution_dir / "packets"
    return sorted(p.stem for p in folder.glob("*.md")) if folder.exists() else []


def claim(project, worker: str, count: int = 1, stale_minutes: int = 120) -> list[str]:
    """Забрать пакеты без ответа для агента-исполнителя. Захват атомарный (эксклюзивный файл):
    два агента не получат один пакет. Захват старше stale_minutes без ответа можно перехватить."""
    check_id(worker, "имя исполнителя")
    claims = project.attribution_dir / "claims"
    claims.mkdir(parents=True, exist_ok=True)
    responses = project.attribution_dir / "responses"
    taken = []
    now = datetime.now(timezone.utc)
    for name in _packets(project):
        if len(taken) >= count:
            break
        if (responses / f"{name}.json").exists():
            continue
        path = claims / f"{name}.json"
        row = {"worker": worker, "utc": now_utc()}
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False))
        except FileExistsError:
            try:
                old = read_json(path)
                age = (now - datetime.fromisoformat(old["utc"])).total_seconds() / 60
            except (ValueError, KeyError):
                age = stale_minutes + 1
            if age <= stale_minutes or old.get("worker") == worker:
                continue
            write_json(path, {**row, "took_over_from": old.get("worker")})
        taken.append(name)
    return taken


def unclaim(project, names: list[str]) -> int:
    n = 0
    for name in names:
        path = project.attribution_dir / "claims" / f"{name}.json"
        if path.exists():
            path.unlink()
            n += 1
    return n


def status(project) -> list[dict]:
    """Состояние каждого пакета: свободен / в работе у … / есть ответ / применён / ошибка."""
    base = project.attribution_dir
    errors = read_json(base / "import_errors.json") if (base / "import_errors.json").exists() else {}
    rows = []
    for name in _packets(project):
        claim_path = base / "claims" / f"{name}.json"
        if (base / "applied" / f"{name}.json").exists():
            state = "applied"
        elif name in errors:
            state = "error"
        elif (base / "responses" / f"{name}.json").exists():
            state = "answered"
        elif claim_path.exists():
            state = "claimed"
        else:
            state = "free"
        row = {"packet": name, "state": state}
        if claim_path.exists():
            row["worker"] = read_json(claim_path).get("worker")
        if name in errors:
            row["errors"] = errors[name][:3]
        rows.append(row)
    return rows


def roles_report(project) -> Path:
    """attribution/ROLES.md: сводка по ролям для проверяющего прохода после параллельной разметки."""
    import difflib
    from collections import Counter, defaultdict
    cast = cast_mod.load(project)
    stats = defaultdict(lambda: {"lines": 0, "chars": 0, "scenes": set(), "sections": set(),
                                 "styles": Counter(), "unsure": 0})
    for section in load_sections(project)["sections"]:
        if not section["audio"] or not (project.script_dir / f"{section['id']}.md").exists():
            continue
        for seg in load_script(project, section["id"]):
            row = stats[seg.get("speaker_id") or "UNKNOWN"]
            row["lines"] += 1
            row["chars"] += len(seg["text"])
            row["scenes"].add(seg.get("scene_id"))
            row["sections"].add(section["id"])
            if seg.get("style"):
                row["styles"][seg["style"]] += 1
            row["unsure"] += seg.get("confidence") in ("medium", "low")
    lines = ["# Роли после атрибуции", "",
             "Проверьте: дубли ролей (одно лицо под двумя ID), роли без голоса, разнобой подачи.", "",
             "| Роль | Имя | Реплик | Символов | Сцен | Разделы | Неуверенных | Частая подача |",
             "|---|---|---|---|---|---|---|---|"]
    for role, r in sorted(stats.items(), key=lambda kv: -kv[1]["chars"]):
        name = cast["roles"].get(role, {}).get("name", "—")
        top = "; ".join(f"{s} ×{n}" for s, n in r["styles"].most_common(3)).replace("|", "/")
        sections = ", ".join(sorted(r["sections"])[:6]) + ("…" if len(r["sections"]) > 6 else "")
        lines.append(f"| {role} | {name} | {r['lines']} | {r['chars']} | {len(r['scenes'])} | {sections} | "
                     f"{r['unsure']} | {top} |")
    warnings = []
    ids = sorted(stats)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if difflib.SequenceMatcher(None, a, b).ratio() >= 0.8:
                warnings.append(f"похожие ID: {a} и {b} — не один ли это персонаж?")
    names = defaultdict(list)
    for rid, role in cast["roles"].items():
        names[(role.get("name") or "").lower()].append(rid)
    warnings += [f"одно имя у разных ролей: {', '.join(v)}" for k, v in names.items() if k and len(v) > 1]
    warnings += [f"{role}: {len(r['styles'])} разных подач — унифицируйте повторяющиеся"
                 for role, r in stats.items() if len(r["styles"]) > 12]
    warnings += [f"{role}: роли нет в cast.json" for role in stats if role not in cast["roles"] and role != "UNKNOWN"]
    if "UNKNOWN" in stats:
        warnings.append(f"UNKNOWN: {stats['UNKNOWN']['lines']} блоков без говорящего")
    if warnings:
        lines += ["", "## Предупреждения", ""] + [f"- {w}" for w in warnings]
    path = project.attribution_dir / "ROLES.md"
    write_text(path, "\n".join(lines) + "\n")
    return path
