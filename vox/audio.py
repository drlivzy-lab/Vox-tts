"""WAV-ввод/вывод, техническая проверка, обрезка тишины, лёгкие эффекты."""
from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np

from .util import HarnessError, write_bytes

SR = 24000


def read_wav(data: bytes | Path) -> tuple[np.ndarray, int]:
    stream = io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else open(data, "rb")
    try:
        with wave.open(stream, "rb") as w:
            if w.getsampwidth() != 2 or w.getcomptype() != "NONE":
                raise HarnessError("Ожидался PCM 16 бит")
            channels, rate = w.getnchannels(), w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    except (EOFError, wave.Error) as exc:
        raise HarnessError("Некорректный WAV") from exc
    finally:
        stream.close()
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    return pcm.astype(np.float32) / 32768.0, rate


def wav_bytes(samples: np.ndarray, rate: int = SR) -> bytes:
    pcm = np.clip(np.rint(samples * 32767.0), -32768, 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        w.writeframes(pcm.tobytes())
    return buffer.getvalue()


def write_wav(path: Path, samples: np.ndarray, rate: int = SR) -> None:
    write_bytes(path, wav_bytes(samples, rate))


def frame_db(samples: np.ndarray, rate: int, frame_ms: int = 10) -> np.ndarray:
    step = max(1, rate * frame_ms // 1000)
    n = len(samples) // step
    if n == 0:
        return np.array([-120.0])
    frames = samples[: n * step].reshape(n, step)
    rms = np.sqrt(np.mean(frames * frames, axis=1)) + 1e-9
    return 20 * np.log10(rms)


def silences(samples: np.ndarray, rate: int, threshold_db: float, min_s: float) -> list[tuple[float, float]]:
    db = frame_db(samples, rate)
    quiet = np.r_[False, db < threshold_db, False].astype(np.int8)
    edges = np.diff(quiet)
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return [(a / 100, b / 100) for a, b in zip(starts, ends) if (b - a) / 100 >= min_s]


def technical(data: bytes, text: str, qc: dict) -> dict:
    """Быстрая проверка сразу после получения: формат, тишина, клиппинг, длительность."""
    samples, rate = read_wav(data)
    duration = len(samples) / rate if rate else 0
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    clipped = int(np.sum(np.abs(samples) >= 0.9999))
    issues = []
    if rate != SR:
        issues.append(f"частота {rate} Гц вместо {SR}")
    if duration < 0.2 or peak < 1e-3:
        issues.append("пустое или беззвучное аудио")
    chars = len([c for c in text if c.isalnum()])
    rate_cps = chars / duration if duration else 0
    # Слишком быстро — пропуск текста; слишком медленно — повторы/зацикливание/долгие паузы.
    if chars >= 40 and rate_cps > qc["max_chars_per_s"]:
        issues.append(f"подозрительно коротко: {rate_cps:.1f} симв/с")
    if chars >= 20 and 0 < rate_cps < qc["min_chars_per_s"]:
        issues.append(f"подозрительно долго: {rate_cps:.1f} симв/с")
    inner = [s for s in silences(samples, rate, -50, qc["max_internal_silence_s"])
             if s[0] > 0.05 and s[1] < duration - 0.05]
    if inner:
        issues.append(f"длинная пауза внутри: {inner[0][0]:.1f}–{inner[0][1]:.1f} с")
    if clipped > 10:
        issues.append(f"клиппинг: {clipped} отсчётов")
    return {"duration_s": round(duration, 3), "sample_rate": rate, "peak": round(peak, 4),
            "clipped": clipped, "chars_per_s": round(rate_cps, 2), "issues": issues}


def trim(samples: np.ndarray, rate: int, threshold_db: float, keep_ms: int) -> np.ndarray:
    db = frame_db(samples, rate)
    loud = np.flatnonzero(db >= threshold_db)
    if not len(loud):
        return samples[:0]
    step = rate // 100
    keep = rate * keep_ms // 1000
    start = max(0, loud[0] * step - keep)
    end = min(len(samples), (loud[-1] + 1) * step + keep)
    out = samples[start:end].copy()
    fade = min(len(out) // 2, rate // 200)  # 5 мс, чтобы не было щелчка на срезе
    if fade:
        ramp = np.linspace(0, 1, fade, dtype=np.float32)
        out[:fade] *= ramp
        out[-fade:] *= ramp[::-1]
    return out


def reverb(samples: np.ndarray, rate: int, wet: float, decay_s: float, seed: int = 7) -> np.ndarray:
    """Короткая мягкая реверберация для мыслей/телепатии; хвост добавляется в конец."""
    length = int(rate * decay_s * 3)
    rng = np.random.default_rng(seed)
    t = np.arange(length) / rate
    impulse = rng.standard_normal(length).astype(np.float32) * np.exp(-t / decay_s * 3).astype(np.float32)
    impulse /= np.sqrt(np.sum(impulse ** 2)) + 1e-9
    n = len(samples) + length - 1
    size = 1 << (n - 1).bit_length()
    wet_signal = np.fft.irfft(np.fft.rfft(samples, size) * np.fft.rfft(impulse, size), size)[:n]
    dry = np.concatenate([samples, np.zeros(length - 1, dtype=np.float32)])
    mixed = dry * (1 - wet) + wet_signal.astype(np.float32) * wet
    peak = np.max(np.abs(mixed)) if len(mixed) else 0
    if peak > 0.99:
        mixed *= 0.99 / peak
    return trim(mixed.astype(np.float32), rate, -60, 20)


def apply_effect(samples: np.ndarray, rate: int, effect: dict | None) -> np.ndarray:
    if not effect:
        return samples
    if effect.get("type") == "reverb":
        return reverb(samples, rate, float(effect.get("wet", 0.15)), float(effect.get("decay_s", 0.3)))
    raise HarnessError(f"Неизвестный эффект {effect.get('type')}")
