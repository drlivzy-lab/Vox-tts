"""Субтитры по распознаванию: точные тайминги слов вместо пропорциональной оценки.

Текст субтитров — всегда исходный (с пунктуацией и регистром), распознавание даёт только
время: слова сопоставляются (difflib), несовпавшие получают время интерполяцией между
соседями. Точность зависит от движка: faster-whisper даёт время каждого слова,
GigaAM — по окнам ≤ 20 с (приблизительно внутри окна), Gemini — по сегментам.

  vox.py subs out/ad/slogan/take-1.wav --text "Вкус, который помнишь." -o take-1.srt
В клипах: [clips] subtitles = "asr" (или --subs asr) при настроенном qc.asr.
"""
from __future__ import annotations

import difflib
import re

from .util import words


def _srt_time(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def display_tokens(text: str) -> list[str]:
    clean = re.sub(r"<[^>]+>|\|[^|]*\||/[^/\n]+/", " ", text)
    clean = re.sub(r"(?m)^[^:\n]{1,30}:\s*", "", clean)  # «Имя:» в диалогах
    return clean.split()


def align(text: str, observed: list[dict]) -> list[dict]:
    """Исходные слова текста с временем {word, start, end} по распознанным словам."""
    tokens = display_tokens(text)
    norm, owner = [], []
    for i, token in enumerate(tokens):
        for w in words(token):
            norm.append(w)
            owner.append(i)
    heard, times = [], []
    for w in observed:
        for t in words(w["word"]):
            heard.append(t)
            times.append((float(w["start"]), float(w["end"])))
    start = [None] * len(tokens)
    end = [None] * len(tokens)
    matcher = difflib.SequenceMatcher(None, norm, heard, autojunk=False)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            i, (a, b) = owner[block.a + k], times[block.b + k]
            start[i] = a if start[i] is None else min(start[i], a)
            end[i] = b if end[i] is None else max(end[i], b)
    known = [i for i in range(len(tokens)) if start[i] is not None]
    if not known:
        return []
    i = 0
    while i < len(tokens):  # серии несовпавших слов делят промежуток между соседями поровну
        if start[i] is not None:
            i += 1
            continue
        j = i
        while j < len(tokens) and start[j] is None:
            j += 1
        lo = end[i - 1] if i > 0 else max(0.0, start[j] - 0.4 * (j - i))
        hi = start[j] if j < len(tokens) else end[i - 1] + 0.4 * (j - i)
        step = max(0.0, hi - lo) / (j - i)
        for k in range(i, j):
            start[k], end[k] = lo + step * (k - i), lo + step * (k - i + 1)
        i = j
    return [{"word": t, "start": round(start[i], 3), "end": round(max(end[i], start[i]), 3)}
            for i, t in enumerate(tokens)]


def srt(aligned: list[dict], max_chars: int = 42, max_s: float = 6.0) -> str:
    cues, current = [], []
    for item in aligned:
        line = " ".join(w["word"] for w in current + [item])
        if current and (len(line) > max_chars or item["end"] - current[0]["start"] > max_s):
            cues.append(current)
            current = []
        current.append(item)
        if re.search(r"[.!?…]$", item["word"]):
            cues.append(current)
            current = []
    if current:
        cues.append(current)
    out = []
    for n, cue in enumerate(cues, 1):
        nxt = cues[n][0]["start"] if n < len(cues) else cue[-1]["end"] + 0.3
        end = min(cue[-1]["end"] + 0.25, max(cue[-1]["end"], nxt - 0.02))
        out.append(f"{n}\n{_srt_time(cue[0]['start'])} --> {_srt_time(end)}\n{' '.join(w['word'] for w in cue)}\n")
    return "\n".join(out)


def from_audio(text: str, wav_path, recognizer) -> str:
    """SRT по распознаванию; пустая строка, если распознавание ничего не дало."""
    aligned = align(text, recognizer.transcribe(wav_path)["words"])
    return srt(aligned) if aligned else ""
