"""Общие функции: атомарная запись, хеши, канонический JSON, безопасные пути."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class HarnessError(Exception):
    """Ошибка, которую CLI показывает пользователю без трассировки."""


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_value(value: Any) -> str:
    return sha_text(canonical(value))


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_bytes(path: Path, data: bytes) -> None:
    _atomic(path, data)


def write_text(path: Path, text: str) -> None:
    _atomic(path, text.encode("utf-8"))


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    _atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    _atomic(path, body.encode("utf-8"))


def append_jsonl(path: Path, row: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def check_id(value: str, what: str = "id") -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise HarnessError(f"Некорректный {what}: {value!r}")
    return value


def safe_join(root: Path, relative: str) -> Path:
    root = Path(root).resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise HarnessError(f"Путь выходит за пределы проекта: {relative}")
    return target


WORD = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")


TAG = re.compile(r"<[^<>]{1,40}>")
STRESS = "́"


def letters(text: str) -> str:
    """Последовательность букв и цифр: инвариант «текст не потерян и не добавлен».

    Не учитываются регистр (CAPS-акцент), ё/е, знак ударения и теги <sigh>, <short pause>.
    """
    text = TAG.sub(" ", text.replace(STRESS, ""))
    return "".join(WORD.findall(text)).lower().replace("ё", "е")


def words(text: str) -> list[str]:
    text = TAG.sub(" ", text.replace(STRESS, ""))
    return [w.lower().replace("ё", "е") for w in WORD.findall(text)]
