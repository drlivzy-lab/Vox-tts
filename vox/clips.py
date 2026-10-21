"""Короткие тексты: слоганы, реклама, рилсы, реплики для видео.

Сценарий clips/<имя>.md в том же формате блоков, что и книга:

    ### slogan-1

    ```yaml
    voice: Kore              # студийный голос, voice_... или профиль/роль из cast.json
    style: "Warm, confident, upbeat; a slight smile in the voice."
    takes: 3                 # сколько дублей сделать (выбирают на слух)
    ```

    ```text
    Вкус, который ПОМНИШЬ. <short pause> Новый «Северный мёд».
    ```

Диалог двух студийных голосов одним запросом (естественные перехваты реплик):

    ```yaml
    speakers: [Аня=Kore, Олег=Puck]
    style: "Friendly radio ad banter."
    ```

    ```text
    Аня: Ты уже пробовал новый кофе?
    Олег: |угу| Третий день не могу остановиться!
    ```

Каждый дубль — отдельный запрос с собственным хешем: повторный запуск не платит
за уже полученные дубли. Результат: out/<сценарий>/<блок>/take-N.{wav,mp3,...},
нормализованные по пресету (social: −14 LUFS), субтитры .srt и index.md.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from . import audio as audio_mod
from . import cast as cast_mod
from . import post
from . import pronounce
from . import subs
from .gemini import ApiError, core_request, core_text, dialogue_request
from .ledger import Ledger
from .script import ALLOWED_TAGS, parse_md
from .util import HarnessError, check_id, sha_value, write_text, now_utc


def resolve_voice(project, value: str) -> str:
    if not value:
        raise HarnessError("Укажите voice")
    cast = cast_mod.load(project)
    if value in cast.get("profiles", {}) and cast["profiles"][value].get("voice"):
        return cast["profiles"][value]["voice"]
    if value in cast.get("roles", {}):
        return cast_mod.voice_for(cast, value)
    from . import library
    entry = library.get(value)
    if entry:
        return entry["voice"]
    match = next((v for v in cast_mod.STUDIO_VOICES if v.lower() == value.lower()), None)
    if match:
        return match
    if value.startswith(("voice_", "voicekey_")):
        return value
    raise HarnessError(f"Неизвестный голос {value}: студийное имя, voice_..., профиль или роль")


def _speakers(value) -> dict[str, str]:
    items = value if isinstance(value, list) else [value]
    result = {}
    for item in items:
        name, sep, voice = str(item).partition("=")
        if not sep:
            raise HarnessError("speakers: [Имя=Голос, Имя=Голос]")
        result[name.strip()] = voice.strip()
    return result


def build_requests(project, block: dict, takes: int | None = None) -> list[dict]:
    model = project.config["tts"]["model"]
    lexicon = pronounce.Lexicon(pronounce.load(project))
    check_id(block["id"], "id блока")
    for tag in re.findall(r"<([^<>]{1,40})>", block["text"]):
        if tag.strip().lower() not in ALLOWED_TAGS:
            raise HarnessError(f"{block['id']}: тег <{tag}> не из таблицы Google — прозвучит вслух")
    count = int(takes or block.get("takes") or project.config["clips"]["takes"])
    requests = []
    for take in range(1, count + 1):
        if block.get("speakers"):
            speakers = {name: resolve_voice(project, voice) for name, voice in _speakers(block["speakers"]).items()}
            turns = []
            for line in block["text"].splitlines():
                if not line.strip():
                    continue
                name, sep, text = line.partition(":")
                if not sep or name.strip() not in speakers:
                    raise HarnessError(f"{block['id']}: строка диалога «Имя: текст» с именем из speakers: {line}")
                turns.append({"speaker": name.strip(), "text": lexicon.apply(text.strip())[0],
                              "style": block.get("style") or ""})
            core = dialogue_request(model, turns, speakers, take)
        else:
            text = lexicon.apply(block["text"].strip())[0]
            voice_name = block.get("voice") or block.get("speaker_id", "")
            from . import library
            default_style = (library.get(voice_name) or {}).get("style", "") if voice_name else ""
            core = core_request(model, resolve_voice(project, voice_name),
                                block.get("style") or default_style, text, take)
        requests.append({"id": f"{block['id']}-take{take}", "block": block["id"], "take": take, "core": core,
                         "request_hash": sha_value(core), "text": core_text(core)})
    return requests


def srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def subtitles(text: str, samples: np.ndarray, rate: int, max_chars: int = 42) -> str:
    """Приблизительные субтитры: фразы распределяются по звучащей части пропорционально длине."""
    clean = re.sub(r"<[^>]+>|\|[^|]*\|", " ", text)
    clean = re.sub(r"^[^:\n]{1,30}:\s*", "", clean, flags=re.M)
    phrases = []
    for sentence in re.split(r"(?<=[.!?…])\s+|\n+", clean):
        sentence = " ".join(sentence.split())
        while len(sentence) > max_chars:
            cut = max(sentence.rfind(", ", 0, max_chars), sentence.rfind(" ", 0, max_chars))
            cut = cut if cut > 10 else max_chars
            phrases.append(sentence[:cut + 1].strip())
            sentence = sentence[cut + 1:].strip()
        if sentence:
            phrases.append(sentence)
    db = audio_mod.frame_db(samples, rate)
    voiced = np.flatnonzero(db > -45)
    if not phrases or not len(voiced):
        return ""
    start, end = voiced[0] / 100, (voiced[-1] + 1) / 100
    total = sum(len(p) for p in phrases)
    out, cursor = [], start
    for i, phrase in enumerate(phrases, 1):
        span = (end - start) * len(phrase) / total
        out.append(f"{i}\n{srt_time(cursor)} --> {srt_time(cursor + span)}\n{phrase}\n")
        cursor += span
    return "\n".join(out)


def plan(project, blocks: list[dict], takes: int | None = None) -> dict:
    ledger = Ledger(project)
    max_attempts = int(project.config["limits"]["max_attempts_per_request"])
    requests = [r for b in blocks for r in build_requests(project, b, takes)]
    pending, cached, blocked = [], 0, []
    for r in requests:
        if ledger.has(r["request_hash"]):
            cached += 1
        elif reason := ledger.blocked(r["request_hash"], max_attempts):
            blocked.append({"job": r["id"], "reason": reason})
        else:
            pending.append(r)
    return {"requests": requests, "pending": pending, "cached": cached, "blocked": blocked,
            "chars": sum(len(r["text"]) for r in pending)}


def render(project, client, name: str, blocks: list[dict], takes: int | None = None,
           formats: list[str] | None = None, preset_name: str | None = None, log=print,
           recognizer=None) -> dict:
    """recognizer — движок распознавания для точных субтитров (vox/subs.py); без него — оценка."""
    cfg = project.config
    tier = cfg["tts"]["service_tier"]
    ledger = Ledger(project)
    p = plan(project, blocks, takes)
    for r in p["pending"]:
        ledger.record("reserve", r["request_hash"], job_id=r["id"], mode="flex", tier=tier, chars=len(r["text"]))
        try:
            data = client.synthesize(r["core"], tier)
        except ApiError as exc:
            ledger.record("unknown" if exc.unknown_outcome else "failure", r["request_hash"], job_id=r["id"],
                          status=exc.status, error=str(exc)[:300])
            log(f"ОШИБКА {r['id']}: {exc}")
            if exc.quota:
                break
            continue
        tech = audio_mod.technical(data, r["text"], cfg["qc"])
        ledger.store(r, data, f"flex:{tier}", tech)
        ledger.record("success", r["request_hash"], job_id=r["id"], duration_s=tech["duration_s"])
        log(f"OK {r['id']}: {tech['duration_s']} с" + (f"  ⚠ {'; '.join(tech['issues'])}" if tech["issues"] else ""))
    ff = post.ffmpeg_path(cfg)
    target = post.preset(preset_name or cfg["clips"]["preset"])
    formats = formats or cfg["clips"]["formats"]
    out_root = project.root / "out" / name
    index = [f"# {name}", "", f"Пресет громкости: {preset_name or cfg['clips']['preset']} "
             f"({target['lufs']} LUFS, пик {target['true_peak']} dBTP). Дубли — выберите лучший на слух.", ""]
    produced = 0
    for r in p["requests"]:
        if not ledger.has(r["request_hash"]):
            index.append(f"- {r['id']}: нет аудио")
            continue
        raw = ledger.audio_path(r["request_hash"])
        samples, rate = audio_mod.read_wav(raw)
        trimmed = audio_mod.trim(samples, rate, cfg["pauses"]["silence_db"], 120)
        base = out_root / r["block"] / f"take-{r['take']}"
        tmp = base.with_name(base.name + ".trim.wav")
        audio_mod.write_wav(tmp, trimmed, rate)
        files = post.export(ff, tmp, base, formats, target)
        if cfg["clips"].get("subtitles"):
            srt = subs.from_audio(r["text"], tmp, recognizer) if recognizer else ""
            write_text(base.with_suffix(".srt"), srt or subtitles(r["text"], trimmed, rate))
        tmp.unlink()
        tech = ledger.meta(r["request_hash"]).get("technical", {})
        index.append(f"- **{r['id']}** — {len(trimmed) / rate:.1f} с: " + ", ".join(
            f"[{fmt}]({p.relative_to(out_root).as_posix()})" for fmt, p in files.items())
            + (f" ⚠ {'; '.join(tech['issues'])}" if tech.get("issues") else ""))
        produced += 1
    index += ["", f"Собрано {now_utc()}."]
    write_text(out_root / "index.md", "\n".join(index) + "\n")
    return {"rendered": produced, "new_requests": len(p["pending"]), "cached": p["cached"],
            "blocked": p["blocked"], "folder": str(out_root)}


def load_blocks(path: Path) -> list[dict]:
    return parse_md(Path(path).read_text(encoding="utf-8"), Path(path).name)
