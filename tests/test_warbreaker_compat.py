"""Совместимость со сценариями «Сокрушителя Войн». Запуск:
WARBREAKER_ROOT="<папка с проектом Warbreaker>" python -m pytest tests"""
import os
from pathlib import Path

import pytest

from vox import cast as cast_mod, pronounce
from vox.config import EXAMPLE_TOML
from vox.freeze import build_jobs
from vox.project import Project
from vox.script import parse_md
from vox.util import letters

ROOT = os.environ.get("WARBREAKER_ROOT")


@pytest.mark.skipif(not ROOT, reason="WARBREAKER_ROOT не задан")
def test_existing_scripts_build_jobs(tmp_path):
    root = Path(ROOT)
    (tmp_path / "book.toml").write_text(EXAMPLE_TOML.format(title="WB", source=""), encoding="utf-8")
    project = Project.open(tmp_path)
    cast_mod.import_voice_plan(project, root / "casting" / "voice_plan.json")
    cast = cast_mod.load(project)
    for path in sorted((root / "script").glob("[0-9][0-9].md"))[:5]:
        segments = parse_md(path.read_text(encoding="utf-8"), path.name)
        voiced = [s for s in segments if s.get("mode") not in ("mouthed", "skip")]
        jobs = build_jobs(project, {"id": path.stem}, segments, cast, pronounce.Lexicon({"entries": []}))
        assert letters(" ".join(j["text"] for j in jobs)) == letters(" ".join(s["text"] for s in voiced))
        assert all(j["style"] == "" for j in jobs if j["speaker_id"] == "NARRATOR")
