"""Загрузка .env без внешних зависимостей.

Порядок: переменные процесса > .env папки проекта > .env папки харнесса.
Файл .env никогда не коммитится и не копируется в отчёты.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[1]
LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse(path: Path) -> dict[str, str]:
    values = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = LINE.match(raw)
        if not match:
            continue
        key, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def load(*folders: Path) -> list[Path]:
    """Подгрузить .env из папок (первая важнее), не перезаписывая окружение процесса."""
    loaded = []
    for folder in [*folders, HARNESS_ROOT]:
        path = Path(folder) / ".env"
        if path.is_file() and path not in loaded:
            for key, value in parse(path).items():
                os.environ.setdefault(key, value)
            loaded.append(path)
    return loaded
