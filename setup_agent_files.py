"""Разложить файлы для агентов по служебным местам (их нельзя записать удалённо):

    python setup_agent_files.py

  setup/mcp.json         -> .mcp.json                         (MCP-сервер vox для Claude Code)
  setup/vox-SKILL.md     -> .claude/skills/vox/SKILL.md       (навык для Claude Code)
  setup/github-tests.yml -> .github/workflows/tests.yml       (тесты в GitHub)
Существующие файлы не перезаписываются без --force.
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLAN = {"setup/mcp.json": ".mcp.json", "setup/vox-SKILL.md": ".claude/skills/vox/SKILL.md",
        "setup/github-tests.yml": ".github/workflows/tests.yml"}

for src, dst in PLAN.items():
    target = ROOT / dst
    if target.exists() and "--force" not in sys.argv:
        print(f"уже есть: {dst}")
        continue
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / src, target)
    print(f"создан: {dst}")
