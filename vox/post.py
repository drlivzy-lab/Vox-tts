"""Постобработка через ffmpeg: громкость по стандартам, экспорт, измерение.

Пресеты громкости (интегральная громкость LUFS / true peak dBTP / LRA):
  audiobook  −19 / −2 / 7   как мастер «Сокрушителя Войн»; ACX требует RMS −23…−18 dB и пик ≤ −3 dB,
                            для ACX берите preset "acx"
  acx        −20 / −3.5 / 7  с запасом под требования Audible/ACX
  podcast    −16 / −1.5 / 9  Apple Podcasts, Spotify (стерео-подкасты)
  social     −14 / −1 / 9    Reels, TikTok, Shorts, YouTube — платформы нормализуют к ≈ −14
  broadcast  −23 / −1 / 15   EBU R128 (ТВ/радио)
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .util import HarnessError

LOUDNESS = {
    "audiobook": {"lufs": -19.0, "true_peak": -2.0, "lra": 7.0},
    "acx": {"lufs": -20.0, "true_peak": -3.5, "lra": 7.0},
    "podcast": {"lufs": -16.0, "true_peak": -1.5, "lra": 9.0},
    "social": {"lufs": -14.0, "true_peak": -1.0, "lra": 9.0},
    "broadcast": {"lufs": -23.0, "true_peak": -1.0, "lra": 15.0},
}
CODECS = {
    "wav": ["-c:a", "pcm_s16le"],
    "flac": ["-c:a", "flac"],
    "mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
    "m4a": ["-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"],
    "opus": ["-c:a", "libopus", "-b:a", "96k"],
}


def ffmpeg_path(config: dict | None = None) -> str:
    explicit = (config or {}).get("master", {}).get("ffmpeg")
    if explicit:
        return explicit
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise HarnessError("Нужен ffmpeg: установите его или pip install imageio-ffmpeg") from None


def run(ff: str, args: list) -> str:
    proc = subprocess.run([ff, "-hide_banner", "-nostdin", "-y", *map(str, args)], capture_output=True)
    if proc.returncode:
        raise HarnessError("ffmpeg: " + proc.stderr.decode("utf-8", "replace")[-1500:])
    return proc.stderr.decode("utf-8", "replace")


def _loudnorm_json(text: str) -> dict:
    found = re.findall(r"\{\s*\"input_i\".*?\}", text, re.S)
    if not found:
        raise HarnessError("loudnorm не вернул измерения")
    return json.loads(found[-1])


def preset(name_or_values) -> dict:
    if isinstance(name_or_values, dict):
        return name_or_values
    if name_or_values not in LOUDNESS:
        raise HarnessError(f"Пресет громкости: {', '.join(LOUDNESS)}")
    return LOUDNESS[name_or_values]


def loudnorm_filter(ff: str, source: Path, target: dict) -> str:
    """Двухпроходный loudnorm: замер, затем линейная коррекция без «качания» громкости."""
    base = f"loudnorm=I={target['lufs']}:TP={target['true_peak']}:LRA={target['lra']}"
    m = _loudnorm_json(run(ff, ["-i", source, "-af", base + ":print_format=json", "-f", "null", "-"]))
    return (f"{base}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
            f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")


def measure(ff: str, path: Path) -> dict:
    """Фактическая громкость файла (EBU R128): I, LRA, true peak."""
    text = run(ff, ["-i", path, "-af", "ebur128=peak=true", "-f", "null", "-"])
    summary = text[text.rfind("Summary:"):]
    get = lambda key: float(re.search(key + r":\s*(-?[\d.]+|-inf)", summary).group(1).replace("-inf", "-120"))
    return {"lufs": get(r"I"), "lra": get(r"LRA"), "true_peak": get(r"Peak")}


def export(ff: str, source: Path, out_base: Path, formats: list[str], target: dict | None,
           sample_rate: int = 24000, extra_filter: str = "") -> dict[str, Path]:
    """Нормализовать (если задан target) и сохранить в нужных форматах."""
    filters = [f for f in (extra_filter, loudnorm_filter(ff, source, target) if target else "") if f]
    results = {}
    for fmt in formats:
        if fmt not in CODECS:
            raise HarnessError(f"Формат {fmt}: доступны {', '.join(CODECS)}")
        rate = 48000 if fmt == "opus" else sample_rate
        dest = out_base.with_suffix("." + fmt)
        dest.parent.mkdir(parents=True, exist_ok=True)
        args = ["-i", source]
        if filters:
            args += ["-af", ",".join(filters)]
        run(ff, [*args, "-ar", rate, "-ac", 1, *CODECS[fmt], dest])
        results[fmt] = dest
    return results
