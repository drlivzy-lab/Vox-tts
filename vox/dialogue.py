"""Эвристическое разбиение русского абзаца на речь и авторские слова.

Результат — черновик: говорящих уточняет агент/LLM или человек. Гарантия
одна: буквы и цифры абзаца сохраняются полностью и в исходном порядке.
"""
from __future__ import annotations

import re

DASH = "[—–-]"
# Граница «речь, — ремарка» или «ремарка. — речь»: знак препинания, пробел, тире, пробел.
TOGGLE = re.compile(r"(?<=[,.!?…:;])\s+[—–]\s+")
LEADING = re.compile(r"^\s*[—–-]\s*")
INTRO = re.compile(r":\s+[—–]\s+")
SPEECH_VERB = re.compile(
    r"(сказа|спроси|ответи|произн[её]с|проговори|продолжи|прошепта|шепну|проворча|"
    r"буркну|замети|возрази|подтверди|воскликну|крикну|выкрикну|прокрича|отозва|"
    r"добави|уточни|поясни|объясни|перебил|поправи|поинтересова|потребова|веле|"
    r"приказа|позва|пробормота|прорыча|прохрипе|провозгласи|подума|написа|"
    r"фыркну|переспроси|согласи|повтори|выдави|заяви|предложи|напомни|прошипе|"
    r"выдохну|усмехну|объяви|сообщи|промолви|вскрича|протяну|рявкну|окликну)\w*", re.I)


def _piece(kind: str, text: str) -> dict:
    return {"kind": kind, "text": text.strip()}


def _tidy(pieces: list[dict]) -> list[dict]:
    out = [p for p in pieces if re.search(r"\w", p["text"])]
    for i, piece in enumerate(out[:-1]):
        text = piece["text"]
        # «Привет, — сказал он»: отдельный запрос с висящей запятой звучит оборванно.
        if text.endswith(","):
            text = text[:-1] + "."
        elif re.search(r"\w$", text):
            text += "."
        piece["text"] = text
    return out


def split_paragraph(text: str) -> list[dict]:
    """Вернуть [{kind: speech|narration, text}] для одного абзаца."""
    text = text.strip()
    if not text:
        return []
    if LEADING.match(text) and not re.match(r"^\s*-\w", text):
        body = LEADING.sub("", text, count=1)
        parts = TOGGLE.split(body)
        pieces = [_piece("speech" if i % 2 == 0 else "narration", part) for i, part in enumerate(parts)]
        return _tidy(pieces)
    if text.startswith("«"):
        match = re.match(r"^(«.*?»[,!?…]?)\s+[—–]\s+(.*)$", text)
        if match:
            rest = split_paragraph("— " + match.group(2)) if INTRO.search(match.group(2)) else [
                _piece("narration", match.group(2))]
            return _tidy([_piece("speech", match.group(1).strip("«»,")), *rest])
    match = INTRO.search(text)
    if match and SPEECH_VERB.search(text[:match.start()]):
        head = text[:match.start() + 1]
        tail = split_paragraph("— " + text[match.end():])
        return _tidy([_piece("narration", head), *tail])
    return [_piece("narration", text)]


def guess_speaker(remark: str, aliases: dict[str, str]) -> str | None:
    """Найти роль по ремарке «сказал Вашер». aliases: форма имени -> ID роли."""
    if not SPEECH_VERB.search(remark):
        return None
    found = []
    for alias, role in aliases.items():
        if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", remark):
            found.append((len(alias), role))
    roles = {role for _, role in found}
    if len(roles) == 1:
        return roles.pop()
    return None
