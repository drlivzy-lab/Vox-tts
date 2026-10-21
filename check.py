"""Проверка харнесса одной командой: компиляция всех модулей и офлайн-тесты.

    python -X utf8 check.py            перед каждым изменением кода харнесса
    python check.py --install-hook     запускать автоматически перед git commit

Тесты не ходят в сеть и ничего не оплачивают (поддельный клиент tests/fakes.py).
Если системного ffmpeg нет, берётся imageio-ffmpeg (pip install imageio-ffmpeg).
"""
from __future__ import annotations

import compileall
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg"):
        return
    try:
        import imageio_ffmpeg
    except ImportError:
        print("ffmpeg не найден: тесты экспорта будут пропущены (pip install imageio-ffmpeg)")
        return
    exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
    shim = ROOT / ".ffmpeg-bin"
    shim.mkdir(exist_ok=True)
    target = shim / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not target.exists():
        shutil.copy2(exe, target)
        target.chmod(0o755)
    os.environ["PATH"] = str(shim) + os.pathsep + os.environ.get("PATH", "")


def install_hook() -> None:
    hook = ROOT / ".git" / "hooks" / "pre-commit"
    if not hook.parent.exists():
        sys.exit("Нет .git: сначала git init")
    hook.write_text(f"#!/bin/sh\n\"{sys.executable}\" -X utf8 \"{ROOT / 'check.py'}\" || exit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    print(f"Хук установлен: {hook}")


def main() -> int:
    if "--install-hook" in sys.argv:
        install_hook()
        return 0
    ok = all(compileall.compile_dir(ROOT / d, quiet=1) for d in ("vox", "tests"))
    ok = compileall.compile_file(ROOT / "mcp_server.py", quiet=1) and ok
    if not ok:
        print("Ошибка компиляции")
        return 1
    ensure_ffmpeg()
    return subprocess.call([sys.executable, "-X", "utf8", "-m", "pytest", "-q", str(ROOT / "tests")], cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
