"""Распознавание для QC: дословно, без подсказки ожидаемого текста.

Почему не просто Whisper: seq2seq-модели «причёсывают» речь — выбрасывают повторы,
заикания и оговорки, иногда дописывают несказанное. Для поиска дефектов синтеза
нужны буквальные модели:
  gigaam   — GigaAM-v3 CTC (Сбер, MIT, лучший открытый русский ASR; CTC пишет то, что
             слышит, без языковой «доводки»). Локально, бесплатно. Таймкоды — по окнам.
  gemini   — gemini-3.8-flash с инструкцией дословной стенограммы и списком аномалий
             (заикания, повторы, обрывы, лишние звуки, странное ударение). Облако, платно,
             независимая вторая проверка.
  faster-whisper, vosk — запасные варианты (Whisper склонен сглаживать повторы).
Результат: {"words": [{word, start, end}], "anomalies": [...], "engine": "..."}.
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import numpy as np

from . import audio as audio_mod
from .util import HarnessError, words as tokenize

VERBATIM_PROMPT = """Ты — стенограф для контроля качества синтезированной русской речи.
Запиши ДОСЛОВНО всё, что звучит. Ничего не исправляй, не сглаживай и не додумывай:
- повторённые слова и фразы пиши столько раз, сколько они прозвучали;
- заикание и оборванные слова — через дефис: «с-с-слово», «нача- начало»;
- лишние звуки — в квадратных скобках: [вдох], [смех], [вздох], [шум], [щелчок];
- паузы длиннее секунды — [пауза N с]; неразборчивое — [неразборчиво];
- числа пиши так, как они произнесены, словами.
Отдельно перечисли аномалии: повтор, заикание, обрыв, перезапуск фразы, лишний звук,
искажение звука, явно неверное ударение (укажи слово и как прозвучало), смена тембра
или голоса посреди записи, шум/клиппинг, чтение служебной разметки вслух.
Время — от начала файла в формате MM:SS.s.
Верни только JSON:
{"segments": [{"start": "00:00.0", "end": "00:04.2", "text": "..."}],
 "anomalies": [{"time": "00:02.1", "type": "...", "detail": "..."}]}"""


def _seconds(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    match = re.fullmatch(r"\s*(?:(\d+):)?(\d+(?:[.,]\d+)?)\s*", str(value or "0"))
    if not match:
        return 0.0
    return int(match.group(1) or 0) * 60 + float(match.group(2).replace(",", "."))


def spread_words(text: str, start: float, end: float) -> list[dict]:
    """Слова сегмента с приблизительными таймкодами (пропорционально длине)."""
    tokens = [t for t in re.split(r"\s+", re.sub(r"\[[^\]]*\]", " ", text)) if tokenize(t)]
    if not tokens:
        return []
    total = sum(len(t) for t in tokens)
    out, cursor = [], start
    for token in tokens:
        span = (end - start) * len(token) / total if total else 0
        out.append({"word": token, "start": round(cursor, 2), "end": round(cursor + span, 2)})
        cursor += span
    return out


def windows(samples: np.ndarray, rate: int, max_s: float = 20.0) -> list[tuple[int, int]]:
    """Нарезка на окна ≤ max_s по самым тихим местам (для коротко-контекстных моделей)."""
    limit = int(max_s * rate)
    out, start = [], 0
    while len(samples) - start > limit:
        lo, hi = start + int(limit * 0.6), start + limit
        db = audio_mod.frame_db(samples[lo:hi], rate)
        cut = lo + int(np.argmin(db)) * (rate // 100)
        out.append((start, cut))
        start = cut
    out.append((start, len(samples)))
    return out


class Engine:
    name = "none"

    def transcribe(self, path: Path) -> dict:
        raise NotImplementedError


class GigaAM(Engine):
    def __init__(self, model: str = "") -> None:
        self.variant = model or "v3_ctc"
        try:
            import gigaam  # pip install git+https://github.com/salute-developers/GigaAM
            self.model = gigaam.load_model(self.variant)
        except ImportError:
            try:
                from transformers import AutoModel
            except ImportError as exc:
                raise HarnessError("Установите GigaAM: pip install git+https://github.com/salute-developers/GigaAM "
                                   "(или transformers для ai-sage/GigaAM-v3)") from exc
            revision = self.variant.replace("v3_", "")
            self.model = AutoModel.from_pretrained("ai-sage/GigaAM-v3", revision=revision, trust_remote_code=True)
        self.name = f"gigaam:{self.variant}"

    def transcribe(self, path: Path) -> dict:
        samples, rate = audio_mod.read_wav(path)
        words = []
        with tempfile.TemporaryDirectory() as tmp:
            for i, (a, b) in enumerate(windows(samples, rate)):
                part = Path(tmp) / f"w{i}.wav"
                audio_mod.write_wav(part, samples[a:b], rate)
                text = self.model.transcribe(str(part))
                if isinstance(text, dict):
                    text = text.get("transcription") or text.get("text") or ""
                words += spread_words(str(text), a / rate, b / rate)
        return {"words": words, "anomalies": [], "engine": self.name}


class GeminiAudio(Engine):
    def __init__(self, client, model: str) -> None:
        if client is None:
            raise HarnessError("Для qc.asr = gemini нужен ключ API")
        self.client, self.model = client, model
        self.name = f"gemini:{model}"

    def transcribe(self, path: Path) -> dict:
        result = self.client.transcribe_verbatim(self.model, Path(path).read_bytes(), VERBATIM_PROMPT)
        words = []
        for seg in result.get("segments", []):
            words += spread_words(seg.get("text", ""), _seconds(seg.get("start")), _seconds(seg.get("end")))
        anomalies = [{"time_s": _seconds(a.get("time")), "type": a.get("type", ""), "detail": a.get("detail", "")}
                     for a in result.get("anomalies", [])]
        return {"words": words, "anomalies": anomalies, "engine": self.name,
                "text": " ".join(s.get("text", "") for s in result.get("segments", []))}


class FasterWhisper(Engine):
    def __init__(self, model: str = "") -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise HarnessError("pip install faster-whisper") from exc
        self.model = WhisperModel(model or "large-v3", device="auto", compute_type="int8")
        self.name = f"faster-whisper:{model or 'large-v3'}"

    def transcribe(self, path: Path) -> dict:
        segments, _ = self.model.transcribe(str(path), language="ru", word_timestamps=True,
                                            condition_on_previous_text=False, vad_filter=False,
                                            initial_prompt="Ммм, ну, это, это... как бы с-с-сказать.")
        words = [{"word": w.word, "start": w.start, "end": w.end} for s in segments for w in (s.words or [])]
        return {"words": words, "anomalies": [], "engine": self.name}


class Vosk(Engine):
    def __init__(self, model: str) -> None:
        try:
            from vosk import Model, KaldiRecognizer, SetLogLevel
        except ImportError as exc:
            raise HarnessError("pip install vosk и укажите qc.asr_model = путь к модели") from exc
        SetLogLevel(-1)
        self.model, self.kaldi = Model(model), KaldiRecognizer
        self.name = f"vosk:{Path(model).name}"

    def transcribe(self, path: Path) -> dict:
        samples, rate = audio_mod.read_wav(path)
        target = np.arange(0, len(samples), rate / 16000)
        pcm = np.clip(np.interp(target, np.arange(len(samples)), samples) * 32767, -32768, 32767).astype("<i2")
        rec = self.kaldi(self.model, 16000)
        rec.SetWords(True)
        out = []
        for start in range(0, len(pcm), 4000):
            if rec.AcceptWaveform(pcm[start:start + 4000].tobytes()):
                out += json.loads(rec.Result()).get("result", [])
        out += json.loads(rec.FinalResult()).get("result", [])
        return {"words": [{"word": w["word"], "start": w["start"], "end": w["end"]} for w in out],
                "anomalies": [], "engine": self.name}


def engine(kind: str, config: dict, client=None) -> Engine | None:
    qc = config["qc"]
    if kind in ("", "none", None):
        return None
    if kind == "gigaam":
        return GigaAM(qc.get("asr_model", ""))
    if kind == "gemini":
        return GeminiAudio(client, qc.get("gemini_model", "gemini-3.8-flash"))
    if kind == "faster-whisper":
        return FasterWhisper(qc.get("asr_model", ""))
    if kind == "vosk":
        return Vosk(qc.get("asr_model", ""))
    raise HarnessError(f"Неизвестный движок распознавания: {kind}")
