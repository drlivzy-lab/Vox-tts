"""Синтез: план, бюджет, Flex-запросы и Batch-пакеты.

Ничего платного не выполняется без явного --yes. Перед каждым запуском
показывается план: сколько запросов, символов и оценка стоимости.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import audio as audio_mod
from .freeze import load_jobs
from .gemini import ApiError, audio_from_batch_row, batch_file_line, batch_output, batch_request, rows_from_jsonl
from .ledger import Ledger
from .util import HarnessError, canonical, read_json, sha_file, write_bytes, write_json, write_jsonl, now_utc


def select(jobs: list[dict], sections: list[str] | None = None, job_ids: list[str] | None = None) -> list[dict]:
    if sections:
        jobs = [j for j in jobs if j["section"] in sections]
    if job_ids:
        wanted = set(job_ids)
        jobs = [j for j in jobs if j["id"] in wanted]
        missing = wanted - {j["id"] for j in jobs}
        if missing:
            raise HarnessError(f"Нет таких jobs в релизе: {', '.join(sorted(missing))}")
    return jobs


def speech_rate(project) -> float:
    path = project.root / "pilot" / "calibration.json"
    if path.exists():
        value = read_json(path).get("chars_per_second")
        if value:
            return float(value)
    return 14.0


def plan(project, jobs: list[dict]) -> dict:
    ledger = Ledger(project)
    max_attempts = int(project.config["limits"]["max_attempts_per_request"])
    pending, cached, blocked, seen = [], 0, [], set()
    for job in jobs:
        if ledger.has(job["request_hash"]):
            cached += 1
            continue
        if job["request_hash"] in seen:  # одинаковый запрос в двух местах книги — синтез один
            continue
        reason = ledger.blocked(job["request_hash"], max_attempts)
        if reason:
            blocked.append({"job": job["id"], "reason": reason})
            continue
        seen.add(job["request_hash"])
        pending.append(job)
    chars = sum(len(j["text"]) for j in pending)
    input_tokens = sum(j["est_tokens"] for j in pending)
    # Русская речь ≈ 14 символов в секунду; после пилота — измеренный темп (pilot/calibration.json).
    seconds = chars / speech_rate(project)
    pricing = project.config["pricing"]
    output_tokens = seconds * float(pricing["audio_tokens_per_second"])
    cost = None
    if pricing["usd_per_1m_input_tokens"] is not None and pricing["usd_per_1m_output_tokens"] is not None:
        cost = (input_tokens * pricing["usd_per_1m_input_tokens"]
                + output_tokens * pricing["usd_per_1m_output_tokens"]) / 1e6
    return {"total": len(jobs), "cached": cached, "pending": pending, "blocked": blocked,
            "chars": chars, "est_audio_min": round(seconds / 60, 1), "est_input_tokens": input_tokens,
            "est_output_tokens": int(output_tokens), "chars_per_second": speech_rate(project),
            "est_cost_usd_standard": None if cost is None else round(cost, 2),
            "est_cost_usd_batch_or_flex": None if cost is None else round(cost * pricing["batch_discount"], 2)}


def enforce_budget(project, pending: list[dict]) -> list[dict]:
    limits = project.config["limits"]
    allowed, chars = [], 0
    for job in pending:
        if len(allowed) >= limits["max_requests_per_run"] or chars + len(job["text"]) > limits["max_chars_per_run"]:
            break
        allowed.append(job)
        chars += len(job["text"])
    return allowed


def run_flex(project, client, jobs: list[dict], log=print) -> dict:
    """Интерактивный синтез по одному запросу (tier из tts.service_tier)."""
    ledger = Ledger(project)
    limits = project.config["limits"]
    tier = project.config["tts"]["service_tier"]
    stop = threading.Event()
    lock = threading.Lock()
    last_start = [0.0]
    stats = {"ok": 0, "failed": 0, "unknown": 0, "suspect": 0, "stopped_reason": None}

    def one(job: dict) -> None:
        if stop.is_set():
            return
        with lock:
            wait = last_start[0] + float(limits["min_interval_s"]) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            last_start[0] = time.monotonic()
            ledger.record("reserve", job["request_hash"], job_id=job["id"], mode="flex", tier=tier,
                          chars=len(job["text"]))
        try:
            data = client.synthesize(job["core"], tier)
        except ApiError as exc:
            with lock:
                if exc.unknown_outcome:
                    ledger.record("unknown", job["request_hash"], job_id=job["id"], error=str(exc)[:300])
                    stats["unknown"] += 1
                else:
                    ledger.record("failure", job["request_hash"], job_id=job["id"], status=exc.status,
                                  error=str(exc)[:300], retry_after=exc.retry_after)
                    stats["failed"] += 1
                if exc.quota:
                    stats["stopped_reason"] = f"квота исчерпана (429){', повторить через ' + exc.retry_after + ' с' if exc.retry_after else ''}"
                    stop.set()
            log(f"ОШИБКА {job['id']}: {exc}")
            return
        try:
            tech = audio_mod.technical(data, job["text"], project.config["qc"])
        except HarnessError as exc:
            with lock:
                ledger.record("failure", job["request_hash"], job_id=job["id"], status="bad_audio", error=str(exc))
                stats["failed"] += 1
            return
        with lock:
            ledger.store(job, data, f"flex:{tier}", tech)
            ledger.record("success", job["request_hash"], job_id=job["id"], duration_s=tech["duration_s"])
            stats["ok"] += 1
            stats["suspect"] += bool(tech["issues"])
        log(f"OK {job['id']}: {tech['duration_s']} с" + (f"  ⚠ {'; '.join(tech['issues'])}" if tech["issues"] else ""))

    with ThreadPoolExecutor(max_workers=max(1, int(limits["concurrency"]))) as pool:
        for future in as_completed([pool.submit(one, job) for job in jobs]):
            future.result()
    return stats


# ---------------- Batch ----------------

def _batch_dir(project, name: str):
    return project.batches_dir / name


def batch_prepare(project, jobs: list[dict], label: str) -> list[str]:
    """Разложить запросы по пакетам: inline (≤ batch_max_payload_bytes) или JSONL-файл (Files API)."""
    release = project.active_release()
    limits = project.config["limits"]
    inline_limit = int(limits["batch_max_payload_bytes"])
    mode = limits.get("batch_input", "auto")
    if mode not in ("auto", "inline", "file"):
        raise HarnessError("limits.batch_input: auto, inline или file")
    total = sum(len(canonical({"request": batch_request(j["core"]), "metadata": {"key": j["id"]}}).encode()) + 4
                for j in jobs)
    use_file = mode == "file" or (mode == "auto" and total > inline_limit)
    limit = int(limits["batch_file_max_bytes"]) if use_file else inline_limit
    groups, current, size = [], [], 0
    for job in jobs:
        row = {"request": batch_request(job["core"]), "metadata": {"key": job["id"]}}
        row_size = len(canonical(row).encode("utf-8")) + 4
        if current and size + row_size > limit:
            groups.append(current)
            current, size = [], 0
        current.append((job, row))
        size += row_size
    if current:
        groups.append(current)
    names = []
    existing = {p.name for p in project.batches_dir.iterdir()} if project.batches_dir.exists() else set()
    for group in groups:
        n = 1
        while f"{release}-{label}-{n:02}" in existing:
            n += 1
        name = f"{release}-{label}-{n:02}"
        existing.add(name)
        folder = _batch_dir(project, name)
        display = f"{project.config['book']['title'] or 'book'} {name}"[:120]
        if use_file:
            request_file = folder / "requests.jsonl"
            write_jsonl(request_file, [batch_file_line(j["core"], j["id"]) for j, _ in group])
            write_json(folder / "request.json", {"batch": {"display_name": display, "input_config": {"file": request_file.name}}})
        else:
            request_file = folder / "request.json"
            write_json(request_file, {"batch": {"display_name": display,
                                                "input_config": {"requests": {"requests": [r for _, r in group]}}}})
        write_json(folder / "job_map.json", [{"id": j["id"], "request_hash": j["request_hash"]} for j, _ in group])
        write_json(folder / "manifest.json", {"name": name, "release": release, "status": "prepared",
                                              "model": project.config["tts"]["model"], "requests": len(group),
                                              "chars": sum(len(j["text"]) for j, _ in group),
                                              "input": "file" if use_file else "inline",
                                              "request_sha256": sha_file(request_file),
                                              "prepared_utc": now_utc()})
        names.append(name)
    return names


def batch_submit(project, client, name: str) -> dict:
    folder = _batch_dir(project, name)
    manifest = read_json(folder / "manifest.json")
    file_mode = manifest.get("input") == "file"
    request_file = folder / ("requests.jsonl" if file_mode else "request.json")
    if sha_file(request_file) != manifest["request_sha256"]:
        raise HarnessError(f"{request_file.name} изменён после подготовки")
    marker = folder / "submission.json"
    ledger = Ledger(project)
    mapping = read_json(folder / "job_map.json")
    max_attempts = int(project.config["limits"]["max_attempts_per_request"])
    for row in mapping:
        if ledger.has(row["request_hash"]):
            raise HarnessError(f"{row['id']} уже есть в кэше: подготовьте пакет заново")
        reason = ledger.blocked(row["request_hash"], max_attempts)
        if reason:
            raise HarnessError(f"{row['id']}: {reason}")
    if file_mode and marker.exists():
        raise HarnessError("Пакет уже отправлялся. Повторная отправка запрещена")
    if file_mode and not manifest.get("uploaded_file"):
        # Загрузка бесплатна и безопасна для повтора; платный шаг — создание пакета ниже.
        manifest["uploaded_file"] = client.upload_file(request_file.read_bytes(), "application/jsonl",
                                                       f"{name}.jsonl")
        manifest["uploaded_utc"] = now_utc()
        write_json(folder / "manifest.json", manifest)
    # Маркер создаётся эксклюзивно: второй запуск не отправит пакет повторно.
    try:
        with marker.open("x", encoding="utf-8") as stream:
            stream.write('{"status": "submitting"}\n')
    except FileExistsError:
        raise HarnessError("Пакет уже отправлялся. Повторная отправка запрещена") from None
    chars = {j["id"]: len(j["text"]) for j in load_jobs(project, manifest["release"])}
    for row in mapping:
        ledger.record("reserve", row["request_hash"], job_id=row["id"], mode="batch", batch=name,
                      chars=chars.get(row["id"]))
    payload = read_json(folder / "request.json")
    try:
        if file_mode:
            result = client.batch_create_file(manifest["model"], payload["batch"]["display_name"],
                                              manifest["uploaded_file"])
        else:
            result = client.batch_create(manifest["model"], payload["batch"]["display_name"],
                                         payload["batch"]["input_config"]["requests"]["requests"])
    except ApiError as exc:
        if exc.unknown_outcome:
            write_json(marker, {"status": "unknown", "error": str(exc), "utc": now_utc()})
            for row in mapping:
                ledger.record("unknown", row["request_hash"], job_id=row["id"], batch=name)
            raise HarnessError("Исход отправки неизвестен: проверьте список Batch в AI Studio, "
                               "прежде чем что-то повторять") from None
        for row in mapping:
            ledger.record("failure", row["request_hash"], job_id=row["id"], batch=name, status=exc.status,
                          error=str(exc)[:300])
        marker.rename(folder / f"submission_failed_{int(time.time())}.json")
        write_json(folder / f"submission_error_{int(time.time())}.json",
                   {"status": exc.status, "error": str(exc), "retry_after": exc.retry_after,
                    "provider_error": exc.provider_error})
        raise
    operation = result.get("name", "")
    write_json(marker, {"status": "submitted", "operation": operation, "utc": now_utc()})
    manifest.update(status="submitted", operation=operation)
    write_json(folder / "manifest.json", manifest)
    return {"name": name, "operation": operation, "requests": len(mapping)}


def batch_status(project, client, name: str) -> dict:
    folder = _batch_dir(project, name)
    marker = read_json(folder / "submission.json")
    if marker.get("status") != "submitted":
        raise HarnessError(f"Пакет {name} не отправлен")
    operation = client.batch_get(marker["operation"])
    state = operation.get("metadata", {}).get("state") or operation.get("state")
    stats = operation.get("metadata", {}).get("batchStats") or {}
    return {"name": name, "state": state, "done": operation.get("done", False), "stats": stats}


def batch_collect(project, client, name: str, log=print) -> dict:
    folder = _batch_dir(project, name)
    marker = read_json(folder / "submission.json")
    manifest = read_json(folder / "manifest.json")
    if manifest.get("status") == "collected":
        return {"name": name, "already_collected": True}
    operation = client.batch_get(marker["operation"])
    state = operation.get("metadata", {}).get("state") or operation.get("state")
    if not operation.get("done"):
        raise HarnessError(f"Пакет ещё выполняется: {state}")
    ledger = Ledger(project)
    mapping = {row["id"]: row for row in read_json(folder / "job_map.json")}
    jobs = {j["id"]: j for j in load_jobs(project, manifest["release"])}
    stats = {"ok": 0, "failed": 0, "suspect": 0}
    rows = []
    if state == "BATCH_STATE_SUCCEEDED":
        kind, value = batch_output(operation)
        if kind == "file":
            data = client.download_file(value)
            write_bytes(folder / "responses.jsonl", data)  # копия ответа: файл у Google временный
            rows = rows_from_jsonl(data)
        else:
            rows = value
    returned = set()
    for row in rows:
        key = row.get("metadata", {}).get("key")
        if key not in mapping or key in returned:
            raise HarnessError(f"Неожиданный или повторный ключ в ответе: {key}")
        returned.add(key)
        job = jobs[key]
        try:
            data = audio_from_batch_row(row)
            tech = audio_mod.technical(data, job["text"], project.config["qc"])
        except (ApiError, HarnessError) as exc:
            ledger.record("failure", job["request_hash"], job_id=key, batch=name, error=str(exc)[:300])
            stats["failed"] += 1
            log(f"ОШИБКА {key}: {exc}")
            continue
        ledger.store(job, data, f"batch:{name}", tech)
        ledger.record("success", job["request_hash"], job_id=key, batch=name, duration_s=tech["duration_s"])
        stats["ok"] += 1
        stats["suspect"] += bool(tech["issues"])
    for key in set(mapping) - returned:
        ledger.record("failure", mapping[key]["request_hash"], job_id=key, batch=name,
                      error=f"нет результата, состояние {state}")
        stats["failed"] += 1
    manifest.update(status="collected", state=state, collected_utc=now_utc(), **{"result": stats})
    write_json(folder / "manifest.json", manifest)
    return {"name": name, "state": state, **stats}


def batches(project) -> list[dict]:
    if not project.batches_dir.exists():
        return []
    return [read_json(p / "manifest.json") for p in sorted(project.batches_dir.iterdir())
            if (p / "manifest.json").exists()]
