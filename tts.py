"""Самостоятельный скрипт Gemini 3.8 Flash TTS — без проекта, кэша и журнала.

Ключ: файл .env рядом со скриптом (GEMINI_API_KEY=...), образец — .env.example.
Каждый запуск — платный запрос, поэтому нужен --yes.

Примеры:
  python tts.py "Привет! Это проба голоса." --voice Kore -o out/hello.wav --yes
  python tts.py --file text.txt --voice Charon --style "Calm, warm, unhurried." -o out/a.wav --yes
  python tts.py --dialogue ad.txt --speakers "Аня=Kore,Олег=Puck" -o out/ad.wav --yes
      (ad.txt: строки «Аня: ...», «Олег: ...»)
  python tts.py "Тест" --voice Puck --format mulaw --sample-rate 8000 -o out/phone.ulaw --yes
  python tts.py --list-voices --language ru-RU
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from vox import env
from vox.gemini import GeminiClient, core_request, dialogue_request
from vox.util import HarnessError, write_bytes

MIME = {"wav": "audio/wav", "l16": "audio/l16", "mulaw": "audio/mulaw", "alaw": "audio/alaw"}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description="Gemini 3.8 Flash TTS одной командой")
    p.add_argument("text", nargs="?")
    p.add_argument("--file", help="текст из файла (UTF-8)")
    p.add_argument("--dialogue", help="файл диалога: строки «Имя: реплика»")
    p.add_argument("--speakers", help="Имя=Голос,Имя=Голос (два студийных голоса)")
    p.add_argument("--voice", default="Kore", help="студийный голос или voice_...")
    p.add_argument("--style", default="", help="подача по-английски, напр. 'Warm, upbeat, a smile in the voice.'")
    p.add_argument("--model", default="gemini-3.8-flash-tts", help="или gemini-3.8-flash-lite-tts")
    p.add_argument("--tier", default="flex", choices=["flex", "standard", "priority"])
    p.add_argument("--format", default="wav", choices=list(MIME))
    p.add_argument("--sample-rate", type=int)
    p.add_argument("-o", "--out", default="out/tts.wav")
    p.add_argument("--list-voices", action="store_true")
    p.add_argument("--language")
    p.add_argument("--yes", action="store_true", help="подтверждаю платный запрос")
    args = p.parse_args(argv)
    env.load(Path.cwd())
    try:
        key = os.environ.get("GEMINI_API_KEY", "")
        if args.list_voices:
            client = GeminiClient(key, 600)
            for voice in client.list_voices(args.language, 1000):
                print(json.dumps({k: voice.get(k) for k in ("id", "display_name", "gender", "pitch", "description")},
                                 ensure_ascii=False))
            return 0
        if args.dialogue:
            speakers = dict(item.split("=", 1) for item in (args.speakers or "").split(",") if "=" in item)
            turns = []
            for line in Path(args.dialogue).read_text(encoding="utf-8").splitlines():
                name, sep, text = line.partition(":")
                if sep and text.strip():
                    turns.append({"speaker": name.strip(), "text": text.strip(), "style": args.style})
            core = dialogue_request(args.model, turns, speakers)
        else:
            text = Path(args.file).read_text(encoding="utf-8") if args.file else args.text
            if not text or not text.strip():
                raise HarnessError("Нет текста")
            core = core_request(args.model, args.voice, args.style, text.strip())
        if not args.yes:
            print(json.dumps(core, ensure_ascii=False, indent=2))
            raise HarnessError("Это платный запрос: проверьте его выше и добавьте --yes")
        client = GeminiClient(key, 600)
        audio = client.synthesize_raw(core, args.tier, MIME[args.format], args.sample_rate)
        write_bytes(Path(args.out), audio)
        print(f"{args.out}: {len(audio):,} байт")
        return 0
    except HarnessError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
