"""Каст: роли книги -> голосовые профили -> голоса Gemini.

cast/cast.json:
  roles:    {ROLE: {name, aliases, gender, description, profile, card?}}
  profiles: {V01: {title, voice, presentation, design_prompt, status, samples}}

Роль — персонаж с постоянным speaker_id; профиль — тембр, который может
исполнять несколько эпизодических ролей, если они не встречаются в одной сцене.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from .util import HarnessError, check_id, read_json, sha_bytes, write_bytes, write_json, now_utc

# 30 студийных голосов из руководства Google; язык определяется по тексту.
STUDIO_VOICES = (
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe",
    "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib",
    "Rasalgethi", "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima",
    "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
)
AUDITION_LINES = {
    "neutral": ("Мы доберёмся до города к вечеру. Там нас уже ждут.", ""),
    "tense": ("Подожди. За дверью кто-то есть. Не делай ни шагу.", "Quietly tense and alert."),
}


def load(project) -> dict:
    if not project.cast_path.exists():
        narrator = project.config["tts"]["narrator_role"]
        return {"roles": {narrator: {"name": "Рассказчик", "aliases": [], "gender": "male",
                                     "description": "", "profile": "V01"}},
                "profiles": {"V01": {"title": "Рассказчик", "voice": None, "presentation": "male",
                                     "design_prompt": "", "status": "candidate", "samples": {}}}}
    return read_json(project.cast_path)


def save(project, cast: dict) -> None:
    write_json(project.cast_path, cast)


def add_role(project, role_id: str, name: str, gender: str, aliases: list[str],
             profile: str | None = None, description: str = "") -> None:
    cast = load(project)
    check_id(role_id, "ID роли")
    if role_id in cast["roles"]:
        raise HarnessError(f"Роль {role_id} уже есть")
    if profile and profile not in cast["profiles"]:
        cast["profiles"][profile] = {"title": name, "voice": None, "presentation": gender,
                                     "design_prompt": "", "status": "candidate", "samples": {}}
    cast["roles"][role_id] = {"name": name, "aliases": aliases, "gender": gender,
                              "description": description, "profile": profile}
    save(project, cast)


def effective_style(project, cast: dict, role_id: str, scene_style: str) -> str:
    """Итоговый style запроса: карточка роли + подача сцены; рассказчик нейтрален."""
    tts = project.config["tts"]
    if role_id == tts["narrator_role"]:
        return tts["narrator_style"]
    scene = (scene_style or "").strip()
    if tts["style_policy"] == "scene":
        return scene
    role = cast["roles"][role_id]
    card = (role.get("card") or cast["profiles"][role["profile"]].get("design_prompt") or "").strip()
    if not card:
        return scene
    return card if not scene else card + "\nScene delivery: " + scene


def voice_for(cast: dict, role_id: str) -> str:
    role = cast["roles"].get(role_id)
    if not role:
        raise HarnessError(f"Роли {role_id} нет в cast.json")
    profile = cast["profiles"].get(role.get("profile") or "")
    if not profile or not profile.get("voice"):
        raise HarnessError(f"У роли {role_id} не назначен голос")
    return profile["voice"]


def usage(scripts: dict[str, list[dict]]) -> dict:
    """Объём работы каждой роли и совместное участие в сценах."""
    chars, segments = Counter(), Counter()
    scenes: dict[str, set] = defaultdict(set)
    for section, segs in scripts.items():
        for seg in segs:
            if seg.get("mode") in ("mouthed", "skip"):
                continue
            role = seg.get("speaker_id")
            chars[role] += len(seg["text"])
            segments[role] += 1
            scenes[seg.get("scene_id") or section].add(role)
    return {"chars": chars, "segments": segments, "scenes": scenes}


def check(project, scripts: dict[str, list[dict]]) -> dict:
    cast = load(project)
    errors, warnings = [], []
    info = usage(scripts)
    narrator = project.config["tts"]["narrator_role"]
    for role in info["chars"]:
        if role not in cast["roles"] and role != "UNKNOWN":
            errors.append(f"роль {role} есть в сценарии, но не в cast.json")
    voices = defaultdict(list)
    for pid, profile in cast["profiles"].items():
        if profile.get("voice"):
            voices[profile["voice"].lower()].append(pid)
        elif any(r.get("profile") == pid for r in cast["roles"].values()):
            errors.append(f"профиль {pid} без голоса")
        if profile.get("status") != "accepted":
            warnings.append(f"профиль {pid} не утверждён после прослушивания")
    for voice, owners in voices.items():
        if len(owners) > 1:
            errors.append(f"голос {voice} назначен нескольким профилям: {', '.join(owners)}")
    for rid, role in cast["roles"].items():
        if not role.get("profile") or role["profile"] not in cast["profiles"]:
            errors.append(f"роль {rid} без профиля")
    conflicts = []
    for scene, roles in info["scenes"].items():
        by_profile = defaultdict(list)
        for role in roles:
            if role in cast["roles"]:
                by_profile[cast["roles"][role].get("profile")].append(role)
        for profile, owners in by_profile.items():
            if len(owners) > 1 and profile:
                conflicts.append(f"{scene}: {', '.join(sorted(owners))} звучат одним профилем {profile}")
    if narrator not in cast["roles"]:
        errors.append(f"нет роли рассказчика {narrator}")
    return {"errors": errors, "warnings": warnings, "conflicts": conflicts,
            "roles": len(cast["roles"]), "profiles": len(cast["profiles"]),
            "top": info["chars"].most_common(15)}


def assign(project, profile_id: str, voice: str, force: bool = False) -> None:
    cast = load(project)
    if profile_id not in cast["profiles"]:
        raise HarnessError(f"Нет профиля {profile_id}")
    voice = next((v for v in STUDIO_VOICES if v.lower() == voice.lower()), voice)
    check_id(voice, "голос")
    profile = cast["profiles"][profile_id]
    if profile.get("voice") and not force:
        raise HarnessError("У профиля уже есть голос; --force заменит его")
    taken = [p for p, other in cast["profiles"].items()
             if p != profile_id and (other.get("voice") or "").lower() == voice.lower()]
    if taken:
        raise HarnessError(f"Голос уже у профиля {taken[0]}")
    profile.update(voice=voice, status="candidate", samples={})
    save(project, cast)


def set_status(project, profile_id: str, status: str) -> None:
    cast = load(project)
    if profile_id not in cast["profiles"]:
        raise HarnessError(f"Нет профиля {profile_id}")
    if status not in ("candidate", "accepted"):
        raise HarnessError("Статус профиля: candidate или accepted")
    cast["profiles"][profile_id]["status"] = status
    cast["profiles"][profile_id]["status_utc"] = now_utc()
    save(project, cast)


def design(project, client, profile_id: str, force: bool = False) -> Path:
    """Voice design: создать сохранённый голос по описанию профиля (платно)."""
    cast = load(project)
    profile = cast["profiles"].get(profile_id)
    if not profile:
        raise HarnessError(f"Нет профиля {profile_id}")
    if profile.get("voice") and not force:
        raise HarnessError("У профиля уже есть голос; --force создаст новый")
    if not profile.get("design_prompt"):
        raise HarnessError("Заполните design_prompt: возраст, тембр, фактура, акцент, базовая манера "
                           "(1–2 предложения, по-английски)")
    voice_id, audio = client.create_voice(project.config["tts"]["model"],
                                          f"{project.config['book']['title'] or 'book'} {profile_id}",
                                          profile["design_prompt"], profile.get("presentation", "neutral"))
    path = project.root / "cast" / "samples" / f"{profile_id}_design.wav"
    write_bytes(path, audio)
    profile.update(voice=voice_id, status="candidate", created_utc=now_utc(),
                   samples={"design": path.relative_to(project.root).as_posix()})
    save(project, cast)
    from . import library
    library.register(f"{project.root.name}-{profile_id}", voice_id, description=profile.get("title", ""),
                     gender=profile.get("presentation", "neutral"), design_prompt=profile["design_prompt"],
                     sample=path, project=project.root.name, force=True)
    return path


def audition(project, client, profile_id: str) -> list[Path]:
    """Две русские пробы профиля: нейтральная и напряжённая (платно)."""
    from .gemini import core_request
    cast = load(project)
    profile = cast["profiles"].get(profile_id)
    if not profile or not profile.get("voice"):
        raise HarnessError(f"У профиля {profile_id} нет голоса")
    paths = []
    for label, (text, style) in AUDITION_LINES.items():
        core = core_request(project.config["tts"]["model"], profile["voice"], style, text)
        audio = client.synthesize(core, project.config["tts"]["service_tier"])
        path = project.root / "cast" / "samples" / f"{profile_id}_{label}.wav"
        write_bytes(path, audio)
        profile.setdefault("samples", {})[label] = path.relative_to(project.root).as_posix()
        profile.setdefault("samples_sha256", {})[label] = sha_bytes(audio)
        paths.append(path)
    profile["status"] = "candidate"
    save(project, cast)
    return paths


def import_voice_plan(project, plan_path: Path) -> dict:
    """Перенести утверждённые голоса из voice_plan.json «Сокрушителя Войн»."""
    plan = read_json(plan_path)
    base = Path(plan_path).resolve().parents[1]
    cast = {"roles": {}, "profiles": {}}
    for pid, profile in plan["profiles"].items():
        prompt = profile.get("voice_design_prompt", "")
        card_path = profile.get("voice_design_card")
        if card_path and (base / card_path).exists():
            prompt = read_json(base / card_path).get("voice_design_prompt", prompt)
        cast["profiles"][pid] = {
            "title": profile.get("title", pid), "voice": profile.get("gemini_voice_id"),
            "presentation": profile.get("presentation", "neutral"), "design_prompt": prompt,
            "status": "accepted" if profile.get("audition_status") == "accepted" else "candidate",
            "samples": {}, "imported_from": str(plan_path)}
    for role, pid in plan["role_voice"].items():
        cast["roles"][role] = {"name": role.title(), "aliases": [], "gender":
                               cast["profiles"][pid]["presentation"], "description": "", "profile": pid}
    save(project, cast)
    return {"roles": len(cast["roles"]), "profiles": len(cast["profiles"])}
