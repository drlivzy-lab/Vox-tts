"""Настройки проекта книги: book.toml поверх значений по умолчанию."""
from __future__ import annotations

import copy
import os
import re
import tomllib
from pathlib import Path

from .util import HarnessError

DEFAULTS: dict = {
    # kind: book — книга из файла; clips — короткие тексты (слоганы, реклама, рилсы).
    "project": {"kind": "book"},
    "book": {"title": "", "author": "", "language": "ru", "source": ""},
    "tts": {
        "model": "gemini-3.8-flash-tts",
        # batch — асинхронно и вдвое дешевле Standard; flex — интерактивно, та же цена.
        "mode": "batch",
        "service_tier": "flex",
        "api_key_env": "GEMINI_API_KEY",
        # Необязательный файл с ключом (строка ключа или «x-goog-api-key: ...»).
        "api_key_file": "",
        "narrator_role": "NARRATOR",
        # Нейтральный рассказчик: тембр задаёт сам голос, style пустой.
        "narrator_style": "",
        # card+scene: карточка роли + сценическая подача; scene: только подача.
        "style_policy": "card+scene",
        "request_timeout_s": 600,
    },
    "chunking": {
        # Стартовая эвристика Warbreaker: 30–90 с речи, не разрывая предложение.
        "max_words": 300,
        "max_input_tokens": 8192,
        "token_safety": 0.6,
        "merge_across_paragraphs": True,
        # Короткие ремарки рассказчика внутри диалога («— сказал он») синтезировать
        # группой и резать по паузам (vox/groups.py). Включайте после пилота.
        "group_remarks": False,
        "remark_max_words": 12,
        "group_max_remarks": 6,
    },
    "limits": {
        "max_requests_per_run": 300,
        "max_chars_per_run": 400000,
        "max_attempts_per_request": 2,
        "concurrency": 2,
        "min_interval_s": 1.0,
        "batch_max_payload_bytes": 19_000_000,
        # inline — запросы в теле (до ~20 МБ); file — JSONL через Files API (до 2 ГБ);
        # auto — inline, пока помещается, иначе файл.
        "batch_input": "auto",
        "batch_file_max_bytes": 1_900_000_000,
    },
    "pauses": {  # миллисекунды тишины между соседними запросами
        "continuation": 120,
        "turn": 250,
        "paragraph": 450,
        "scene": 1200,
        "heading": 900,
        "edge_keep_ms": 40,
        "silence_db": -50.0,
    },
    "effects": {
        "thought": {"type": "reverb", "wet": 0.16, "decay_s": 0.30},
    },
    "qc": {
        # gigaam — локально, русский, CTC (буквальнее Whisper); gemini — облачная дословная
        # расшифровка; faster-whisper / vosk — запасные варианты; none — только техника.
        "asr": "none",
        "asr_model": "",
        "second_asr": "none",
        "gemini_model": "gemini-3.8-flash",
        "splice_window_s": 3.0,
        "splice_level_jump_db": 6.0,
        "min_chars_per_s": 7.0,
        "max_chars_per_s": 28.0,
        "max_internal_silence_s": 2.5,
        "repeat_min_words": 4,
        "insert_min_words": 4,
    },
    "master": {
        # пресеты громкости: см. vox/post.py LOUDNESS (audiobook, podcast, social, broadcast)
        "preset": "audiobook",
        "lufs": -19.0,
        "true_peak": -2.0,
        "lra": 7.0,
        "bitrate": "64k",
        "chapter_gap_s": 2.0,
        "ffmpeg": "",
    },
    "llm": {
        # agent — пакеты для ИИ-агента (бесплатно, проверяется импортом);
        # gemini — прямой вызов текстовой модели (платно, только с --yes).
        "provider": "agent",
        "model": "gemini-3.8-flash",
        "packet_max_segments": 120,
    },
    "pricing": {
        # Standard-тариф gemini-3.8-flash-tts на 2026 г. (сверяйте с
        # https://ai.google.dev/gemini-api/docs/pricing; с 01.01.2027 объявлено удвоение).
        "usd_per_1m_input_tokens": 0.50,
        "usd_per_1m_output_tokens": 9.00,
        "batch_discount": 0.5,
        "audio_tokens_per_second": 25,
    },
    "clips": {
        "takes": 1,
        "formats": ["wav", "mp3"],
        "preset": "social",
        "subtitles": True,
    },
    "vendor_paths": [],
}


def _merge(base: dict, extra: dict, prefix: str = "") -> dict:
    result = copy.deepcopy(base)
    for key, value in extra.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge(result[key], value, prefix + key + ".")
        else:
            result[key] = value
    return result


def load_config(root: Path) -> dict:
    path = Path(root) / "book.toml"
    if not path.is_file():
        raise HarnessError(f"Нет {path}. Создайте проект: python book.py init <папка>")
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    config = _merge(DEFAULTS, data)
    if config["tts"]["mode"] not in ("batch", "flex"):
        raise HarnessError("tts.mode должен быть batch или flex")
    if config["tts"]["service_tier"] not in ("flex", "standard"):
        raise HarnessError("tts.service_tier должен быть flex или standard")
    return config


def api_key(config: dict, root: Path) -> str:
    from .env import load as load_env
    load_env(Path(root))
    key = os.environ.get(config["tts"]["api_key_env"], "").strip()
    if key:
        return key
    name = config["tts"].get("api_key_file")
    if name:
        path = (Path(root) / name).resolve()
        content = path.read_text(encoding="utf-8-sig")
        found = set(re.findall(r"(?i)x-goog-api-key:\s*([^\s'\"]+)", content))
        if not found:
            found = {line.strip() for line in content.splitlines()
                     if re.fullmatch(r"[A-Za-z0-9_\-]{20,}", line.strip())}
        if len(found) != 1:
            raise HarnessError(f"В {path.name} должен быть ровно один ключ")
        return found.pop()
    raise HarnessError(f"Нет ключа: впишите {config['tts']['api_key_env']}=... в файл .env "
                       "(образец .env.example) или укажите tts.api_key_file")


EXAMPLE_TOML = '''# Настройки проекта озвучки. Полный список значений — vox/config.py (DEFAULTS).

[project]
kind = "{kind}"         # book — книга; clips — короткие тексты (реклама, слоганы, рилсы)

[book]
title = "{title}"
author = ""
source = "{source}"     # FB2, EPUB, TXT или MD внутри папки проекта

[tts]
model = "gemini-3.8-flash-tts"
mode = "{mode}"          # batch (асинхронно, дёшево) или flex (сразу, та же цена)
service_tier = "flex"   # для интерактивных запросов; standard только осознанно
# ключ: файл .env (GEMINI_API_KEY=...) рядом с vox.py или в папке проекта
# api_key_file = "../../Google Cred.md"   # альтернативно: файл с ключом
narrator_role = "NARRATOR"
narrator_style = ""     # нейтральный рассказчик

[chunking]
max_words = 300

[limits]
max_requests_per_run = 300
max_attempts_per_request = 2

[qc]
asr = "none"            # gigaam (локально, рекомендуется) | gemini (облако) | faster-whisper | vosk
second_asr = "none"     # независимая вторая проверка, например gemini

[llm]
provider = "agent"      # agent: пакеты для ИИ-агента; gemini: прямой вызов
model = "gemini-3.8-flash"  # текстовая модель для provider = "gemini"
'''
