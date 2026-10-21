"""MCP-сервер харнесса: команды vox как инструменты агента (stdio, без зависимостей).

Подключение в Claude Code — файл .mcp.json рядом (уже лежит), в других клиентах:
    command: python   args: ["-X", "utf8", "<путь>/mcp_server.py"]   env: VOX_PROJECT=<папка проекта>

Платные команды через MCP НЕ выполняются: synth, clips render, say, pilot run, cast design/audition,
pron trial, attribute run и всё с --yes. Их агент запускает в терминале после явного «да»
пользователя — так подтверждение остаётся за человеком. Также не выполняется qc run, если
настроено облачное распознавание (qc.asr/second_asr = gemini).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from vox import __version__ as VERSION  # noqa: E402
from vox.cli import main as vox_main  # noqa: E402

PAID = {("synth",), ("clips", "render"), ("say",), ("pilot", "run"), ("cast", "design"), ("cast", "audition"),
        ("pron", "trial"), ("attribute", "run")}

TOOLS = [
    {"name": "vox_next", "description": "Следующий шаг конвейера с готовой командой и причиной. Начинайте с него.",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string", "description": "папка проекта; по умолчанию VOX_PROJECT"}}}},
    {"name": "vox_status", "description": "Сводка проекта: разделы, утверждено, релиз, кэш, открытые QC, пакеты Batch.",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}}},
    {"name": "vox_show_job", "description": "Точный запрос к API для job и его состояние в журнале.",
     "inputSchema": {"type": "object", "properties": {"job": {"type": "string"}, "project": {"type": "string"}},
                     "required": ["job"]}},
    {"name": "vox", "description": ("Любая бесплатная команда vox.py, например 'check', 'plan --sections s003', "
                                    "'attribute claim --worker a1 --count 2', 'qc accept s003-j0004 --note ок', "
                                    "'review --all', 'costs'. Платные команды отклоняются: их запускают в терминале "
                                    "с --yes только после разрешения пользователя."),
     "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}, "project": {"type": "string"}},
                     "required": ["command"]}},
]


def _paid(argv: list[str]) -> str | None:
    if "--yes" in argv:
        return "флаг --yes"
    words = [a for a in argv if not a.startswith("-")]
    for rule in PAID:
        if tuple(words[:len(rule)]) == rule:
            return "платная команда " + " ".join(rule)
    return None


def _cloud_qc(project: str) -> bool:
    try:
        from vox.project import Project
        cfg = Project.open(project).config["qc"]
    except Exception:
        return False
    return "gemini" in (cfg.get("asr"), cfg.get("second_asr"))


def run_vox(argv: list[str], project: str | None) -> tuple[str, bool]:
    project = project or os.environ.get("VOX_PROJECT") or "."
    reason = _paid(argv)
    if reason:
        return (f"Отклонено ({reason}): через MCP платное не выполняется. Покажите пользователю план "
                f"(`plan`/`clips plan`) и после его явного согласия выполните в терминале: "
                f"python vox.py --book \"{project}\" {' '.join(argv)}"), True
    if argv[:2] == ["qc", "run"] and _cloud_qc(project):
        return "Отклонено: qc run с облачным распознаванием (gemini) платный — запускайте в терминале с разрешения.", True
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = vox_main(["--book", project, *argv])
        except SystemExit as exc:  # argparse
            code = exc.code if isinstance(exc.code, int) else 2
        except Exception as exc:  # noqa: BLE001 — агенту нужен текст ошибки, а не падение сервера
            err.write(f"{type(exc).__name__}: {exc}")
            code = 1
    text = out.getvalue() + (("\n" + err.getvalue()) if err.getvalue() else "")
    return text.strip() or "(нет вывода)", code != 0


def call(name: str, args: dict) -> tuple[str, bool]:
    project = args.get("project")
    if name == "vox_next":
        return run_vox(["next"], project)
    if name == "vox_status":
        return run_vox(["status"], project)
    if name == "vox_show_job":
        return run_vox(["show-job", args["job"]], project)
    if name == "vox":
        return run_vox(shlex.split(args.get("command", ""), posix=os.name != "nt"), project)
    return f"Неизвестный инструмент {name}", True


def handle(message: dict) -> dict | None:
    method, mid = message.get("method"), message.get("id")
    if mid is None:  # уведомление
        return None
    if method == "initialize":
        result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "vox", "version": VERSION},
                  "instructions": "Харнесс озвучки на Gemini TTS. Сначала vox_next; правила — AGENTS.md."}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params", {})
        text, error = call(params.get("name", ""), params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": text}], "isError": error}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Метод {method} не поддерживается"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve() -> None:
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
    for line in stdin:
        if not line.strip():
            continue
        try:
            reply = handle(json.loads(line))
        except Exception as exc:  # noqa: BLE001
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
