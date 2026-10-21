"""Поддельные клиент Gemini и распознаватель: тесты не ходят в сеть и ничего не оплачивают."""
from __future__ import annotations

import base64

import numpy as np

from vox import audio as audio_mod
from vox.gemini import ApiError, core_text
from vox.util import words


def _voiced(text: str, seconds: float | None = None) -> np.ndarray:
    chars = len([c for c in text if c.isalnum()])
    duration = seconds if seconds is not None else max(0.6, chars / 14)
    t = np.arange(int(duration * audio_mod.SR)) / audio_mod.SR
    return 0.3 * np.sin(2 * np.pi * 180 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))


def fake_wav(text: str, seconds: float | None = None) -> bytes:
    """Тон длительностью ~14 символов/с; строки (\n) разделены паузой 0.45 с, как у модели."""
    lead = np.zeros(int(0.15 * audio_mod.SR))
    gap = np.zeros(int(0.45 * audio_mod.SR))
    lines = [l for l in text.split("\n") if l.strip()] or [text]
    chunks = [lead]
    for i, line in enumerate(lines):
        if i:
            chunks.append(gap)
        chunks.append(_voiced(line, seconds if len(lines) == 1 else None))
    chunks.append(lead)
    return audio_mod.wav_bytes(np.concatenate(chunks).astype(np.float32))


class FakeClient:
    def __init__(self, fail_with: ApiError | None = None, fail_after: int = 10**9) -> None:
        self.calls = []
        self.batches = {}
        self.files = {}
        self.file_batches = set()
        self.fail_with = fail_with
        self.fail_after = fail_after

    def synthesize(self, core, service_tier="flex"):
        if len(self.calls) >= self.fail_after and self.fail_with:
            self.calls.append(("fail", core))
            raise self.fail_with
        self.calls.append(("tts", core))
        return fake_wav(core_text(core))

    def batch_create(self, model, display_name, rows):
        name = f"batches/fake{len(self.batches) + 1}"
        self.calls.append(("batch", name, len(rows)))
        self.batches[name] = rows
        return {"name": name}

    # --- Files API ---
    def upload_file(self, data, mime_type, display_name):
        name = f"files/up{len(self.files) + 1}"
        self.calls.append(("upload", name))
        self.files[name] = data
        return name

    def download_file(self, name):
        self.calls.append(("download", name))
        return self.files[name]

    def batch_create_file(self, model, display_name, file_name):
        import json
        rows = [json.loads(l) for l in self.files[file_name].decode("utf-8").splitlines() if l.strip()]
        name = f"batches/fake{len(self.batches) + 1}"
        self.calls.append(("batch_file", name, len(rows)))
        self.batches[name] = [{"request": r["request"], "metadata": {"key": r["key"]}} for r in rows]
        self.file_batches.add(name)
        return {"name": name}

    @staticmethod
    def _response(row):
        text = row["request"]["contents"][0]["parts"][0]["text"]
        data = base64.b64encode(fake_wav(text)).decode()
        return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"inlineData": {
            "mimeType": "audio/wav", "data": data}}]}}]}

    def batch_get(self, name):
        import json
        rows = self.batches[name]
        if name in self.file_batches:
            out = f"files/out-{name.split('/')[-1]}"
            self.files[out] = "\n".join(json.dumps({"key": r["metadata"]["key"], "response": self._response(r)})
                                        for r in rows).encode("utf-8")
            return {"name": name, "done": True, "metadata": {"state": "BATCH_STATE_SUCCEEDED"},
                    "response": {"responsesFile": out}}
        responses = [{"metadata": r["metadata"], "response": self._response(r)} for r in rows]
        return {"name": name, "done": True, "metadata": {"state": "BATCH_STATE_SUCCEEDED"},
                "response": {"inlinedResponses": {"inlinedResponses": responses}}}

    def create_voice(self, model, display_name, prompt, gender, language_code="ru-RU"):
        return "voice_fake001", fake_wav("проба голоса")

    def generate_json(self, model, prompt):
        raise AssertionError("not used")


class EchoRecognizer:
    """«Распознаёт» отправленный текст; можно внедрить повтор для проверки QC."""
    name = "echo"

    def __init__(self, texts: dict, repeat_in: set | None = None) -> None:
        self.texts = texts
        self.repeat_in = repeat_in or set()

    def transcribe(self, path):
        text = self.texts.get(path.stem, "")
        tokens = words(text)
        if path.stem in self.repeat_in and len(tokens) > 8:
            tokens = tokens[:8] + tokens[3:8] + tokens[8:]
        return {"words": [{"word": w, "start": i * 0.3, "end": i * 0.3 + 0.25} for i, w in enumerate(tokens)],
                "anomalies": [], "engine": "echo"}
