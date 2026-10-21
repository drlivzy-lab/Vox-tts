"""Клиент Gemini API: TTS (Interactions), Batch, голоса, текстовая модель.

Ключ берётся только из окружения/файла и никогда не попадает в журналы.
Политика повторов: GET повторяется при сетевых сбоях и 5xx; POST не повторяется
никогда — оборванный POST мог быть уже принят и оплачен.
"""
from __future__ import annotations

import base64
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

from .util import HarnessError

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
UPLOAD_ROOT = "https://generativelanguage.googleapis.com/upload/v1beta"
DOWNLOAD_ROOT = "https://generativelanguage.googleapis.com/download/v1beta"


class ApiError(HarnessError):
    def __init__(self, message: str, status: int | None = None, retry_after: str | None = None,
                 provider_error: dict | None = None, unknown_outcome: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.provider_error = provider_error or {}
        # True: запрос мог дойти и быть оплачен (обрыв/тайм-аут POST).
        self.unknown_outcome = unknown_outcome

    @property
    def quota(self) -> bool:
        return self.status == 429

    @property
    def transient(self) -> bool:
        return self.status in (500, 502, 503, 504)


def core_request(model: str, voice: str, style: str, text: str, take: int = 1) -> dict:
    """Каноническое содержание запроса: от него считается хеш кэша и журнала.

    take > 1 — отдельный дубль того же текста (новый хеш, тот же payload API).
    """
    core = {"v": 1, "model": model, "voice": voice, "style": style or "", "text": text}
    if take > 1:
        core["take"] = take
    return core


def dialogue_request(model: str, turns: list[dict], speakers: dict[str, str], take: int = 1) -> dict:
    """Двухголосый диалог одним запросом: только студийные голоса, ровно два говорящих."""
    if len(speakers) != 2 or len(set(v.lower() for v in speakers.values())) != 2:
        raise HarnessError("Диалог одним запросом: ровно два говорящих с разными голосами")
    if any(v.startswith(("voice_", "voicekey_")) for v in speakers.values()):
        raise HarnessError("Диалог одним запросом поддерживает только студийные голоса; "
                           "созданные голоса синтезируйте по репликам")
    for turn in turns:
        if turn.get("speaker") not in speakers or not str(turn.get("text", "")).strip():
            raise HarnessError(f"Реплика без известного говорящего или пустая: {turn}")
    core = {"v": 1, "model": model, "speakers": dict(speakers),
            "dialogue": [{"speaker": t["speaker"], "text": t["text"], "style": t.get("style") or ""}
                         for t in turns]}
    if take > 1:
        core["take"] = take
    return core


def core_text(core: dict) -> str:
    if "dialogue" in core:
        return "\n".join(t["text"] for t in core["dialogue"])
    return core["text"]


def interactions_payload(core: dict, service_tier: str, mime_type: str = "audio/wav",
                         sample_rate: int | None = None) -> dict:
    response_format = {"type": "audio", "mime_type": mime_type}
    if sample_rate:
        response_format["sample_rate"] = sample_rate
    if "dialogue" in core:
        content = []
        for turn in core["dialogue"]:
            annotation = {"type": "speech_metadata", "speaker": turn["speaker"]}
            if turn["style"]:
                annotation["style"] = turn["style"]
            content.append({"type": "text", "text": turn["text"], "annotations": [annotation]})
        speech_config = {"mode": "conversational",
                         "speakers": [{"speaker": s, "voice": v} for s, v in core["speakers"].items()]}
    else:
        annotation = {"type": "speech_metadata"}
        if core["style"]:
            annotation["style"] = core["style"]
        content = [{"type": "text", "text": core["text"], "annotations": [annotation]}]
        speech_config = [{"voice": core["voice"]}]
    return {
        "model": core["model"],
        "service_tier": service_tier,
        "input": [{"type": "user_input", "content": content}],
        "response_format": response_format,
        "generation_config": {"speech_config": speech_config},
    }


def batch_request(core: dict) -> dict:
    if "dialogue" in core:
        raise HarnessError("Диалог одним запросом отправляйте через Flex, не через Batch")
    part = {"text": core["text"]}
    if core["style"]:
        part["speech_metadata"] = {"style": core["style"]}
    return {"contents": [{"role": "user", "parts": [part]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"voice": core["voice"]}}}}


def pcm_to_wav(pcm: bytes, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        out.writeframes(pcm)
    return buffer.getvalue()


def decode_audio(encoded: str, mime: str = "") -> bytes:
    raw = base64.b64decode(encoded, validate=True)
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return raw
    if mime.lower().startswith(("audio/l16", "audio/pcm")):
        match = re.search(r"rate=(\d+)", mime)
        return pcm_to_wav(raw, int(match.group(1)) if match else 24000)
    raise ApiError(f"Неожиданный формат аудио: {mime or 'неизвестно'}")


def audio_from_interaction(data: dict) -> bytes:
    found = None
    for step in data.get("steps", []):
        if step.get("type") == "model_output":
            for item in step.get("content", []):
                if item.get("type") == "audio" and item.get("data"):
                    found = (item["data"], item.get("mime_type", "audio/wav"))
    if not found:
        raise ApiError("Ответ не содержит аудио")
    return decode_audio(*found)


def audio_from_batch_row(row: dict) -> bytes:
    if row.get("error"):
        raise ApiError("Ошибка запроса в Batch: " + json.dumps(row["error"], ensure_ascii=False)[:500])
    candidates = row.get("response", {}).get("candidates", [])
    if len(candidates) != 1:
        raise ApiError(f"Ожидался один кандидат, получено {len(candidates)}")
    reason = candidates[0].get("finishReason")
    if reason not in (None, "STOP"):
        raise ApiError(f"Генерация остановлена: {reason}")
    parts = candidates[0].get("content", {}).get("parts", [])
    blobs = [p.get("inlineData") or p.get("inline_data") for p in parts
             if p.get("inlineData") or p.get("inline_data")]
    if len(blobs) != 1:
        raise ApiError("Ожидался ровно один аудиофрагмент")
    return decode_audio(blobs[0]["data"], blobs[0].get("mimeType", blobs[0].get("mime_type", "")))


def batch_output(operation: dict) -> tuple[str, object]:
    """("inline", rows) или ("file", "files/...") — где лежат результаты пакета."""
    output = (operation.get("response") or operation.get("metadata", {}).get("output")
              or operation.get("output") or {})
    rows = output.get("inlinedResponses")
    if isinstance(rows, dict):
        rows = rows.get("inlinedResponses")
    if rows is not None:
        return "inline", rows
    if output.get("responsesFile"):
        return "file", output["responsesFile"]
    raise ApiError("В операции нет результатов")


def batch_rows(operation: dict) -> list[dict]:
    kind, value = batch_output(operation)
    if kind == "file":
        raise ApiError("Batch вернул файл результатов: используйте rows_from_jsonl(download_file(...))")
    return value


def batch_file_line(core: dict, key: str) -> dict:
    """Строка JSONL входного файла Batch (Files API)."""
    return {"key": key, "request": batch_request(core)}


def rows_from_jsonl(data: bytes) -> list[dict]:
    """Файл результатов Batch -> строки в том же виде, что inline (metadata.key + response/error)."""
    rows = []
    for line in data.decode("utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        row = {"metadata": {"key": item.get("key")}}
        if "response" in item:
            row["response"] = item["response"]
        if "error" in item or "status" in item:
            row["error"] = item.get("error") or item.get("status")
        rows.append(row)
    return rows


class GeminiClient:
    def __init__(self, api_key: str, timeout: int = 600) -> None:
        if not api_key:
            raise HarnessError("Нет ключа Gemini API: впишите GEMINI_API_KEY=... в файл .env (образец .env.example)")
        self._key = api_key
        self.timeout = timeout

    def _redact(self, text: str) -> str:
        return str(text).replace(self._key, "[REDACTED]")

    def request(self, method: str, route: str, payload: dict | None = None,
                timeout: int | None = None) -> dict:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"x-goog-api-key": self._key}
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        url = route if route.startswith("http") else API_ROOT + route
        for attempt in range(4):
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                try:
                    error = json.load(exc).get("error", {})
                except (ValueError, OSError):
                    error = {}
                if method == "GET" and exc.code in (500, 502, 503, 504) and attempt < 3:
                    time.sleep(5 * 2 ** attempt)
                    continue
                detail = self._redact(error.get("message", ""))[:400]
                raise ApiError(f"Gemini API: HTTP {exc.code} при {method} {route.split('?')[0]}"
                               + (f": {detail}" if detail else ""), status=exc.code,
                               retry_after=exc.headers.get("Retry-After") if exc.headers else None,
                               provider_error=json.loads(self._redact(json.dumps(error)))) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if method == "GET" and attempt < 3:
                    time.sleep(5 * 2 ** attempt)
                    continue
                raise ApiError(f"Сетевая ошибка при {method}: {type(exc).__name__}",
                               unknown_outcome=method == "POST") from None
        raise AssertionError("unreachable")

    def _raw(self, method: str, url: str, data: bytes | None, headers: dict, timeout: int | None = None,
             retries: int = 0) -> tuple[bytes, dict]:
        headers = {"x-goog-api-key": self._key, **headers}
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as response:
                    return response.read(), {k.lower(): v for k, v in response.headers.items()}
            except urllib.error.HTTPError as exc:
                if attempt < retries and exc.code in (500, 502, 503, 504):
                    time.sleep(5 * 2 ** attempt)
                    continue
                detail = self._redact(exc.read()[:400].decode("utf-8", "replace"))
                raise ApiError(f"Gemini API: HTTP {exc.code} при {method} {url.split('?')[0]}: {detail}",
                               status=exc.code) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt < retries:
                    time.sleep(5 * 2 ** attempt)
                    continue
                raise ApiError(f"Сетевая ошибка при {method}: {type(exc).__name__}") from None
        raise AssertionError("unreachable")

    # --- Files API (большие пакеты Batch) ---
    def upload_file(self, data: bytes, mime_type: str, display_name: str) -> str:
        """Загрузка файла (resumable, одним куском). Бесплатно; файл живёт у Google 48 часов."""
        _, headers = self._raw("POST", UPLOAD_ROOT + "/files", json.dumps({"file": {"display_name": display_name}}).encode(),
                               {"X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                                "X-Goog-Upload-Header-Content-Length": str(len(data)),
                                "X-Goog-Upload-Header-Content-Type": mime_type,
                                "Content-Type": "application/json"})
        url = headers.get("x-goog-upload-url")
        if not url:
            raise ApiError("Files API не вернул адрес загрузки")
        body, _ = self._raw("POST", url, data, {"Content-Length": str(len(data)), "X-Goog-Upload-Offset": "0",
                                                "X-Goog-Upload-Command": "upload, finalize"}, timeout=1800)
        name = json.loads(body).get("file", {}).get("name")
        if not name:
            raise ApiError("Files API не вернул имя файла")
        return name

    def download_file(self, name: str) -> bytes:
        body, _ = self._raw("GET", f"{DOWNLOAD_ROOT}/{name}:download?alt=media", None, {}, timeout=1800, retries=3)
        return body

    # --- TTS ---
    def synthesize(self, core: dict, service_tier: str = "flex") -> bytes:
        """WAV 24 кГц моно PCM16 (для конвейера); другие форматы — synthesize_raw."""
        data = self.request("POST", "/interactions", interactions_payload(core, service_tier))
        return audio_from_interaction(data)

    def synthesize_raw(self, core: dict, service_tier: str, mime_type: str,
                       sample_rate: int | None = None) -> bytes:
        data = self.request("POST", "/interactions",
                            interactions_payload(core, service_tier, mime_type, sample_rate))
        for step in data.get("steps", []):
            if step.get("type") == "model_output":
                for item in step.get("content", []):
                    if item.get("type") == "audio" and item.get("data"):
                        return base64.b64decode(item["data"], validate=True)
        raise ApiError("Ответ не содержит аудио")

    def check_model(self, model: str) -> dict:
        return self.request("GET", "/models/" + model)

    # --- Batch ---
    def batch_create(self, model: str, display_name: str, rows: list[dict]) -> dict:
        payload = {"batch": {"display_name": display_name,
                             "input_config": {"requests": {"requests": rows}}}}
        return self.request("POST", f"/models/{model}:batchGenerateContent", payload, timeout=300)

    def batch_create_file(self, model: str, display_name: str, file_name: str) -> dict:
        payload = {"batch": {"display_name": display_name, "input_config": {"file_name": file_name}}}
        return self.request("POST", f"/models/{model}:batchGenerateContent", payload, timeout=300)

    def batch_get(self, name: str) -> dict:
        return self.request("GET", "/" + name, timeout=300)

    # --- голоса ---
    def list_voices(self, language: str | None = None, limit: int = 200) -> list[dict]:
        params = {"page_size": str(min(limit, 1000))}
        if language:
            params["language_code"] = language
        voices = []
        while True:
            data = self.request("GET", "/voices?" + urllib.parse.urlencode(params))
            voices.extend(data.get("voices", []))
            token = data.get("next_page_token") or data.get("nextPageToken")
            if not token or len(voices) >= limit:
                return voices[:limit]
            params["page_token"] = token

    def create_voice(self, model: str, display_name: str, prompt: str, gender: str,
                     language_code: str = "ru-RU") -> tuple[str, bytes]:
        data = self.request("POST", "/voices", {"store": True, "voice": {
            "model": model, "type": "prompted", "display_name": display_name,
            "gender": gender, "language_code": language_code, "prompted": {"input": prompt}}})
        sample = data.get("sample_audio") or data.get("sampleAudio") or {}
        if not data.get("id") or not sample.get("data"):
            raise ApiError("Voice design не вернул ID и образец")
        return data["id"], decode_audio(sample["data"], sample.get("mime_type", "audio/wav"))

    # --- понимание аудио (дословная расшифровка для QC) ---
    def transcribe_verbatim(self, model: str, wav: bytes, prompt: str) -> dict:
        if len(wav) > 18_000_000:
            raise ApiError("Фрагмент больше 18 МБ: проверяйте по частям")
        data = self.request("POST", f"/models/{model}:generateContent", {
            "contents": [{"role": "user", "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode()}}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0}})
        try:
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            return json.loads(text)
        except (KeyError, IndexError, ValueError) as exc:
            raise ApiError("Модель расшифровки вернула не JSON") from exc

    # --- текстовая модель (атрибуция) ---
    def generate_json(self, model: str, prompt: str) -> dict:
        data = self.request("POST", f"/models/{model}:generateContent", {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"}})
        try:
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            return json.loads(text)
        except (KeyError, IndexError, ValueError) as exc:
            raise ApiError("Текстовая модель вернула не JSON") from exc
