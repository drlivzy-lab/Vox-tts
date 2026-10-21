"""Раскладка папки книги и указатель активного релиза."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import load_config
from .util import HarnessError, read_json, write_json


@dataclass
class Project:
    root: Path
    config: dict

    @classmethod
    def open(cls, root: Path | str) -> "Project":
        root = Path(root).resolve()
        return cls(root, load_config(root))

    # --- текст ---
    @property
    def paragraphs_path(self) -> Path:
        return self.root / "text" / "paragraphs.jsonl"

    @property
    def sections_path(self) -> Path:
        return self.root / "text" / "sections.json"

    # --- сценарий и подготовка ---
    @property
    def script_dir(self) -> Path:
        return self.root / "script"

    @property
    def cast_path(self) -> Path:
        return self.root / "cast" / "cast.json"

    @property
    def lexicon_path(self) -> Path:
        return self.root / "pronunciation" / "lexicon.json"

    @property
    def attribution_dir(self) -> Path:
        return self.root / "attribution"

    # --- производство ---
    @property
    def releases_dir(self) -> Path:
        return self.root / "releases"

    @property
    def cache_dir(self) -> Path:
        return self.root / "audio" / "cache"

    @property
    def ledger_path(self) -> Path:
        return self.root / "audio" / "ledger.jsonl"

    @property
    def batches_dir(self) -> Path:
        return self.root / "audio" / "batches"

    def qc_dir(self, release: str) -> Path:
        return self.root / "qc" / release

    def chapters_dir(self, release: str) -> Path:
        return self.root / "chapters" / release

    def master_dir(self, release: str) -> Path:
        return self.root / "master" / release

    # --- активный релиз ---
    @property
    def active_path(self) -> Path:
        return self.releases_dir / "ACTIVE.json"

    def active_release(self) -> str:
        if not self.active_path.exists():
            raise HarnessError("Нет замороженного релиза: выполните freeze")
        return read_json(self.active_path)["release"]

    def set_active(self, release: str, manifest_sha: str) -> None:
        write_json(self.active_path, {"release": release, "manifest_sha256": manifest_sha})

    def release_dir(self, release: str | None = None) -> Path:
        return self.releases_dir / (release or self.active_release())
