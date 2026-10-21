"""Словарь произношения: подстановки только в текст для синтеза.

Литературный текст сценария не меняется. На этапе freeze каждое вхождение
формы заменяется на проверенную подсказку: IPA в слешах (`/xɐˈlandrʲɪn/`,
у нас принятый на слух способ задать ударение), форма с ударением (`Ва́р`)
или иное написание. Омонимы различаются полем context (регулярное выражение,
которое должно найтись в тексте блока).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .util import HarnessError, read_json, write_json, write_text

STATUSES = {"proposed", "accepted", "rejected"}


def empty() -> dict:
    return {"entries": [], "note": "status: proposed -> accepted только после прослушивания пробы"}


def load(project) -> dict:
    return read_json(project.lexicon_path) if project.lexicon_path.exists() else empty()


class Lexicon:
    def __init__(self, data: dict, include_proposed: bool = False) -> None:
        self.rules = []
        for entry in data.get("entries", []):
            status = entry.get("status", "proposed")
            if status not in STATUSES:
                raise HarnessError(f"Неизвестный статус словаря: {status}")
            if status == "rejected" or (status == "proposed" and not include_proposed):
                continue
            form, say = entry.get("form"), entry.get("say")
            if not form or not say:
                raise HarnessError(f"Запись словаря без form/say: {entry}")
            flags = 0 if entry.get("case_sensitive", True) else re.I
            pattern = re.compile(r"(?<![\ẃ])" + re.escape(form) + r"(?![\ẃ])", flags)
            context = re.compile(entry["context"], re.I) if entry.get("context") else None
            self.rules.append((len(form), pattern, say, context, form))
        # Длинные формы раньше коротких: «Халландрене» не должна стать «Халландрен»+«е».
        self.rules.sort(key=lambda r: -r[0])
        self.readable = {say: form for _, _, say, _, form in self.rules}

    def apply(self, text: str) -> tuple[str, list[str]]:
        applied: list[str] = []
        pieces = re.split(r"(/[^/\n]+/)", text)  # готовые IPA-вставки не трогаем
        for i in range(0, len(pieces), 2):
            chunk = pieces[i]
            for _, pattern, say, context, form in self.rules:
                if context and not context.search(text):
                    continue
                chunk, count = pattern.subn(say, chunk)
                if count:
                    applied += [form] * count
            pieces[i] = chunk
        return "".join(pieces), applied

    def to_readable(self, text: str) -> str:
        """Обратная замена подсказок на слова — для сверки распознанного текста."""
        text = re.sub(r"/[^/\n]+/", lambda m: self.readable.get(m.group(0), " "), text)
        return text.replace("́", "")


SENTENCE_START = re.compile(r"(^|[.!?…]\s+|[—–]\s+|«|\"|\(|:\s+)$")


def scan(project, scripts: dict[str, list[dict]]) -> dict:
    """Кандидаты в словарь: имена собственные, латиница, цифры, аббревиатуры."""
    data = load(project)
    known = {e["form"] for e in data["entries"]}
    names, other = Counter(), Counter()
    contexts: dict[str, list[str]] = defaultdict(list)
    for section_id, segments in scripts.items():
        for seg in segments:
            if seg.get("mode") in ("mouthed", "skip"):
                continue
            text = seg["text"]
            for match in re.finditer(r"[A-Za-zА-ЯЁа-яё́’'-]+|\d+", text):
                word = match.group(0).strip("-’'")
                if not word or word in known:
                    continue
                before = text[:match.start()]
                if re.fullmatch(r"\d+", word):
                    key = ("число", word)
                elif re.search(r"[A-Za-z]", word):
                    key = ("латиница", word)
                elif len(word) > 1 and word.isupper():
                    key = ("аббревиатура/CAPS", word)
                elif word[0].isupper() and not SENTENCE_START.search(before):
                    key = ("имя", word)
                else:
                    continue
                (names if key[0] == "имя" else other)[key] += 1
                if len(contexts[word]) < 2:
                    contexts[word].append(f"{seg['id']}: …{text[max(0, match.start() - 40):match.end() + 40]}…")
    lines = ["# Кандидаты в словарь произношения", "",
             "Слова, где синтез чаще всего ошибается. Добавьте нужные в pronunciation/lexicon.json "
             "(status: proposed), прослушайте пробу (`pron trial`), затем пометьте accepted.", "",
             "CAPS в тексте модель читает как логическое ударение, а цифры лучше писать словами.", "",
             "| Тип | Слово | Вхождений | Контекст |", "|---|---|---|---|"]
    for (kind, word), count in [*names.most_common(), *other.most_common()]:
        ctx = "<br>".join(c.replace("|", "\\|") for c in contexts[word])
        lines.append(f"| {kind} | {word} | {count} | {ctx} |")
    write_text(project.root / "pronunciation" / "candidates.md", "\n".join(lines) + "\n")
    if not project.lexicon_path.exists():
        write_json(project.lexicon_path, data)
    return {"names": len(names), "other": len(other)}


def add_entry(project, form: str, say: str, context: str | None = None, note: str = "") -> None:
    data = load(project)
    if any(e["form"] == form and e.get("context") == context for e in data["entries"]):
        raise HarnessError(f"Запись для {form} уже есть")
    data["entries"].append({"form": form, "say": say, "context": context, "status": "proposed",
                            "note": note})
    write_json(project.lexicon_path, data)


def set_status(project, form: str, status: str) -> int:
    if status not in STATUSES:
        raise HarnessError(f"Статус: {', '.join(sorted(STATUSES))}")
    data = load(project)
    hits = [e for e in data["entries"] if e["form"] == form]
    if not hits:
        raise HarnessError(f"Нет записи {form}")
    for entry in hits:
        entry["status"] = status
    write_json(project.lexicon_path, data)
    return len(hits)
