"""Общая библиотека голосов для всех проектов: library/voices.json рядом с vox.py.

Зачем: спроектированный голос (voice design) живёт в аккаунте Google год и стоит денег;
его карточку, образец и дату истечения нужно помнить вне конкретной книги. Здесь же —
«фирменные» голоса компании с закреплённой подачей для рекламы и рилсов.

  vox.py voices list [--expiring 30]
  vox.py voices add --name brand --voice voice_... --style "Warm, confident." --description "..."
  vox.py voices import-cast              голоса текущего проекта -> библиотека
  vox.py voices use --name brand --profile V05   голос библиотеки -> профиль каста
В clips/*.md и `say` можно писать voice: brand — подача по умолчанию берётся из библиотеки.
Путь можно переопределить переменной VOX_LIBRARY.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .util import HarnessError, check_id, read_json, write_json, now_utc

TTL_DAYS = 365  # созданные голоса хранятся у Google год


def root() -> Path:
    return Path(os.environ.get("VOX_LIBRARY") or Path(__file__).resolve().parents[1] / "library")


def load() -> dict:
    path = root() / "voices.json"
    return read_json(path) if path.exists() else {"voices": {}}


def save(data: dict) -> None:
    write_json(root() / "voices.json", data)


def is_designed(voice: str) -> bool:
    return voice.startswith(("voice_", "voicekey_"))


def register(name: str, voice: str, description: str = "", style: str = "", gender: str = "neutral",
             design_prompt: str = "", sample: Path | None = None, project: str = "", created: str | None = None,
             force: bool = False) -> dict:
    check_id(name, "имя голоса")
    data = load()
    if name in data["voices"] and data["voices"][name]["voice"] != voice and not force:
        raise HarnessError(f"В библиотеке уже есть {name} с другим голосом (--force заменит)")
    entry = data["voices"].get(name, {})
    created = created or entry.get("created_utc") or now_utc()
    entry.update(voice=voice, description=description or entry.get("description", ""),
                 style=style or entry.get("style", ""), gender=gender or entry.get("gender", "neutral"),
                 design_prompt=design_prompt or entry.get("design_prompt", ""), created_utc=created,
                 projects=sorted(set(entry.get("projects", [])) | ({project} if project else set())))
    if is_designed(voice):
        entry["expires_utc"] = (datetime.fromisoformat(created) + timedelta(days=TTL_DAYS)).replace(microsecond=0).isoformat()
    if sample and Path(sample).exists():
        target = root() / "samples" / f"{name}{Path(sample).suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sample, target)
        entry["sample"] = target.relative_to(root()).as_posix()
    data["voices"][name] = entry
    save(data)
    return entry


def get(name: str) -> dict | None:
    return load()["voices"].get(name)


def expiring(days: int = 30) -> list[dict]:
    limit = datetime.now(timezone.utc) + timedelta(days=days)
    out = []
    for name, entry in load()["voices"].items():
        if entry.get("expires_utc") and datetime.fromisoformat(entry["expires_utc"]) <= limit:
            out.append({"name": name, **entry})
    return sorted(out, key=lambda e: e["expires_utc"])


def import_cast(project) -> list[str]:
    from . import cast as cast_mod
    cast = cast_mod.load(project)
    tag = project.root.name
    added = []
    for pid, profile in cast["profiles"].items():
        voice = profile.get("voice")
        if not voice:
            continue
        roles = [r.get("name", rid) for rid, r in cast["roles"].items() if r.get("profile") == pid]
        sample = (profile.get("samples") or {}).get("design")
        register(f"{tag}-{pid}", voice, description=profile.get("title", "") + (": " + ", ".join(roles) if roles else ""),
                 gender=profile.get("presentation", "neutral"), design_prompt=profile.get("design_prompt", ""),
                 sample=project.root / sample if sample else None, project=tag, created=profile.get("created_utc"))
        added.append(f"{tag}-{pid}")
    return added


def use(project, name: str, profile_id: str) -> dict:
    from . import cast as cast_mod
    entry = get(name)
    if not entry:
        raise HarnessError(f"В библиотеке нет голоса {name}: vox.py voices list")
    cast = cast_mod.load(project)
    profile = cast["profiles"].setdefault(profile_id, {"title": name, "voice": None, "presentation": entry.get("gender", "neutral"),
                                                       "design_prompt": entry.get("design_prompt", ""), "status": "candidate",
                                                       "samples": {}})
    profile.update(voice=entry["voice"], status="accepted", library=name)
    cast_mod.save(project, cast)
    register(name, entry["voice"], project=project.root.name)
    return {"profile": profile_id, "voice": entry["voice"]}
