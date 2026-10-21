"""Кэш аудио по хешу запроса и журнал всех платных попыток.

Правила (из опыта «Сокрушителя Войн»):
  * попытка записывается в журнал ДО отправки (reserve);
  * если исход неизвестен (обрыв POST, сбой процесса), запрос автоматически
    не повторяется — сначала нужно выяснить, не оплачен ли он;
  * число попыток на один запрос ограничено limits.max_attempts_per_request;
  * отклонённое после проверки аудио не удаляется, а переносится в rejected/.
"""
from __future__ import annotations

import shutil
from collections import defaultdict
from pathlib import Path

from .util import HarnessError, append_jsonl, read_json, read_jsonl, sha_bytes, write_bytes, write_json, now_utc


class Ledger:
    def __init__(self, project) -> None:
        self.project = project
        self.path = project.ledger_path
        self.cache = project.cache_dir
        self._events = None
        self._state = None

    # --- журнал ---
    def events(self) -> list[dict]:
        if self._events is None:
            self._events = read_jsonl(self.path)
        return self._events

    def record(self, event: str, request_hash: str, **fields) -> None:
        row = {"utc": now_utc(), "event": event, "request_hash": request_hash, **fields}
        append_jsonl(self.path, row)
        self.events().append(row)
        self._state = None

    def state(self) -> dict[str, dict]:
        """Сводка по каждому хешу: попытки, открытые резервы, последний исход."""
        if self._state is not None:
            return self._state
        result: dict[str, dict] = defaultdict(lambda: {"attempts": 0, "open": 0, "unknown": 0,
                                                         "last": None, "rejected": 0})
        for row in self.events():
            item = result[row["request_hash"]]
            if row["event"] == "reserve":
                item["attempts"] += 1
                item["open"] += 1
            elif row["event"] in ("success", "failure"):
                item["open"] = max(0, item["open"] - 1)
                item["last"] = row["event"]
            elif row["event"] == "unknown":
                item["open"] = max(0, item["open"] - 1)
                item["unknown"] += 1
                item["last"] = "unknown"
            elif row["event"] == "resolve_unknown":
                item["unknown"] = max(0, item["unknown"] - 1)
            elif row["event"] == "reject":
                item["rejected"] += 1
        self._state = dict(result)
        return self._state

    def blocked(self, request_hash: str, max_attempts: int) -> str | None:
        item = self.state().get(request_hash)
        if not item:
            return None
        if item["open"]:
            return "есть незавершённая отправка (возможно, ещё в Batch)"
        if item["unknown"]:
            return "исход прошлой отправки неизвестен: проверьте и выполните ledger resolve"
        # Каждое отклонённое после проверки аудио даёт право ровно на одну новую попытку.
        if item["attempts"] >= max_attempts + item["rejected"]:
            return f"исчерпан лимит попыток ({item['attempts']} из {max_attempts + item['rejected']})"
        return None

    # --- кэш ---
    def audio_path(self, request_hash: str) -> Path:
        return self.cache / request_hash[:2] / f"{request_hash}.wav"

    def has(self, request_hash: str) -> bool:
        return self.audio_path(request_hash).is_file()

    def store(self, job: dict, audio: bytes, source: str, technical: dict) -> None:
        path = self.audio_path(job["request_hash"])
        digest = sha_bytes(audio)
        if path.exists():
            if sha_bytes(path.read_bytes()) == digest:
                return
            raise HarnessError(f"{job['id']}: в кэше уже другое аудио для этого запроса")
        write_bytes(path, audio)
        write_json(path.with_suffix(".json"), {
            "request_hash": job["request_hash"], "core": job["core"], "first_job_id": job["id"],
            "source": source, "audio_sha256": digest, "stored_utc": now_utc(), "technical": technical})

    def meta(self, request_hash: str) -> dict:
        return read_json(self.audio_path(request_hash).with_suffix(".json"))

    def reject(self, job: dict, reason: str) -> Path:
        path = self.audio_path(job["request_hash"])
        if not path.exists():
            raise HarnessError(f"{job['id']}: в кэше нет аудио")
        folder = self.cache / "rejected"
        folder.mkdir(parents=True, exist_ok=True)
        n = len(list(folder.glob(job["request_hash"] + "-*.wav"))) + 1
        target = folder / f"{job['request_hash']}-{n}.wav"
        shutil.move(str(path), target)
        shutil.move(str(path.with_suffix(".json")), target.with_suffix(".json"))
        self.record("reject", job["request_hash"], job_id=job["id"], reason=reason)
        return target
