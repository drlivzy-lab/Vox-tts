import json
import shutil
import textwrap
from pathlib import Path

import pytest

from vox import attribute, cast as cast_mod, pronounce, qc as qc_mod, script as script_mod, synth
from vox.assemble import assemble
from vox.cli import main
from vox.dialogue import split_paragraph
from vox.freeze import freeze, load_jobs, split_long
from vox.gemini import ApiError
from vox.ingest import ingest
from vox.ledger import Ledger
from vox.project import Project
from vox.util import HarnessError, letters, read_json, write_json

from fakes import EchoRecognizer, FakeClient

FB2 = """<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
<description><title-info><book-title>Пробная книга</book-title>
<author><first-name>Иван</first-name><last-name>Автор</last-name></author></title-info></description>
<body>
<section><title><p>Пролог</p></title>
<p>Ветер гнал тучи над Халландреном. Вашер стоял у стены.</p>
<p>— Ты опоздал, — сказал Вар. — Я ждал тебя с рассвета.</p>
<p>— Дорога была длинной, — ответил Вашер.</p>
<empty-line/>
<p>Ночь опустилась на город<a l:href="#n1" type="note">1</a>.</p>
</section>
<section><title><p>Глава 1</p></title>
<epigraph><p>Цвет — это жизнь.</p></epigraph>
<p>Сири смотрела на дорогу. Она думала о доме.</p>
<p>— Мы почти приехали, — сказала Сири.</p>
</section>
</body>
<body name="notes"><section id="n1"><title><p>1</p></title><p>Примечание переводчика.</p></section></body>
</FictionBook>
"""


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "book.fb2"
    src.write_text(FB2, encoding="utf-8")
    assert main(["init", str(tmp_path / "proj"), "--source", str(src), "--title", "Проба"]) == 0
    proj = Project.open(tmp_path / "proj")
    ingest(proj)
    return proj


def setup_cast(proj):
    cast = cast_mod.load(proj)
    cast["profiles"]["V01"].update(voice="voice_narr", status="accepted")
    cast_mod.save(proj, cast)
    cast_mod.add_role(proj, "VASHER", "Вашер", "male", ["Вашер"], "V02")
    cast_mod.add_role(proj, "VAHR", "Вар", "male", ["Вар"], "V03")
    cast_mod.add_role(proj, "SIRI", "Сири", "female", ["Сири"], "V04")
    for pid, voice in (("V02", "Charon"), ("V03", "Fenrir"), ("V04", "Kore")):
        cast_mod.assign(proj, pid, voice)
        cast_mod.set_status(proj, pid, "accepted")


def test_ingest_structure(project):
    sections = read_json(project.sections_path)["sections"]
    assert [s["title"] for s in sections] == ["Пролог", "Глава 1", "1"]
    assert sections[2]["audio"] is False and sections[2]["kind"] == "note"
    paras = [json.loads(l) for l in project.paragraphs_path.read_text(encoding="utf-8").splitlines()]
    kinds = [p["kind"] for p in paras if p["section"] == "s001"]
    assert kinds == ["heading", "text", "text", "text", "scene_break", "text"]
    assert "1" not in next(p["text"] for p in paras if p["text"].startswith("Ночь"))
    with pytest.raises(HarnessError):
        ingest(project)  # повторное извлечение не перезаписывает ID


def test_dialogue_split_preserves_letters():
    for text in ["— Ты опоздал, — сказал Вар. — Я ждал тебя с рассвета.",
                 "Он сказал: — Иди домой.", "«Не сейчас», — подумала Сири.", "— Вот она – суть!"]:
        pieces = split_paragraph(text)
        assert letters(" ".join(p["text"] for p in pieces)) == letters(text)
    assert [p["kind"] for p in split_paragraph("— Ты опоздал, — сказал Вар. — Я ждал.")] == \
        ["speech", "narration", "speech"]


def test_full_pipeline_batch_and_flex(project, monkeypatch):
    setup_cast(project)
    stats = script_mod.draft(project)
    assert stats.get("unknown", 0) == 0  # ремарки «сказал Вар» и «ответил Вашер» узнаны
    with pytest.raises(HarnessError):
        freeze(project)  # блоки ещё не утверждены
    script_mod.approve(project, ["s001", "s002"])
    pronounce.add_entry(project, "Халландреном", "/xɐˈlandrʲɪnəm/")
    rel = freeze(project)
    jobs = load_jobs(project)
    # непрослушанная запись словаря в релиз не попадает
    assert not any("/" in j["text"] for j in jobs)
    pronounce.set_status(project, "Халландреном", "accepted")
    rel2 = freeze(project)
    assert rel2["release"] != rel["release"]
    jobs = load_jobs(project)
    assert any("/xɐˈlandrʲɪnəm/" in j["text"] for j in jobs)
    narr = [j for j in jobs if j["speaker_id"] == "NARRATOR"]
    assert all(j["style"] == "" for j in narr)
    assert [j["join_before"] for j in jobs if j["section"] == "s001"][0] == "start"
    assert "scene" in [j["join_before"] for j in jobs]

    client = FakeClient()
    # пакет через CLI-путь без --yes невозможен
    assert main(["--book", str(project.root), "synth"]) == 1
    p = synth.plan(project, jobs)
    names = synth.batch_prepare(project, p["pending"], "all")
    synth.batch_submit(project, client, names[0])
    with pytest.raises(HarnessError):
        synth.batch_submit(project, client, names[0])  # повторная отправка запрещена
    # пока пакет в работе, Flex этот же запрос не отправит
    assert synth.plan(project, jobs)["blocked"]
    result = synth.batch_collect(project, client, names[0])
    assert result["ok"] == len(p["pending"]) and result["failed"] == 0
    assert synth.plan(project, jobs)["pending"] == []

    # QC с внедрённым повтором
    texts = {j["request_hash"]: j["text"] for j in jobs}
    long_job = max(jobs, key=lambda j: len(j["text"].split()))
    monkeypatch.setattr(qc_mod, "engines", lambda project, client=None: [EchoRecognizer(
        {h: pronounce.Lexicon(pronounce.load(project)).to_readable(t) for h, t in texts.items()},
        {long_job["request_hash"]})])
    stats = qc_mod.run(project)
    assert stats["suspect"] >= 1
    issues = qc_mod.open_issues(project, project.active_release(), jobs)
    assert long_job["id"] in [i["job_id"] for i in issues]

    # отклонение -> одна новая попытка через Flex
    qc_mod.decide(project, long_job["id"], "reject", "повтор фразы")
    p = synth.plan(project, jobs)
    assert [j["id"] for j in p["pending"]] == [long_job["id"]]
    flex = synth.run_flex(project, client, p["pending"], log=lambda *_: None)
    assert flex["ok"] == 1
    monkeypatch.setattr(qc_mod, "engines", lambda project, client=None: [EchoRecognizer(
        {h: pronounce.Lexicon(pronounce.load(project)).to_readable(t) for h, t in texts.items()})])
    qc_mod.run(project, force=True)
    assert qc_mod.open_issues(project, project.active_release(), jobs) == []

    from vox.advisor import steps
    chapters = assemble(project)
    (project.root / "pronunciation" / "candidates.md").write_text("", encoding="utf-8")
    assert steps(project)[0]["command"].endswith("qc splices")
    from vox import splices
    report = splices.run(project)
    assert report["joins"] > 0
    assert not [f for f in read_json(project.qc_dir(project.active_release()) / "splices" / "s001.json")["findings"]
                if f["type"] in ("наложение", "пауза не по карте", "щелчок")]
    assert {c["section"] for c in chapters} == {"s001", "s002"}
    meta = read_json(project.chapters_dir(project.active_release()) / "s001.json")
    starts = [t["start_s"] for t in meta["timeline"]]
    assert starts == sorted(starts)

    if shutil.which("ffmpeg"):
        from vox.master import master
        result = master(project)
        assert (project.master_dir(project.active_release()) / "book.m4b").exists()
        assert [c["title"] for c in result["chapters"]] == ["Пролог", "Глава 1"]


def test_cache_reuse_across_releases(project):
    setup_cast(project)
    script_mod.draft(project)
    script_mod.approve(project, ["s001", "s002"])
    freeze(project)
    client = FakeClient()
    jobs = load_jobs(project)
    synth.run_flex(project, client, synth.plan(project, jobs)["pending"], log=lambda *_: None)
    first_calls = len(client.calls)
    # правим одну реплику -> новый релиз, синтезируется только она
    path = script_mod.script_path(project, "s002")
    raw = path.read_text(encoding="utf-8").replace("Мы почти приехали.", "Мы почти приехали!")
    path.write_text(raw, encoding="utf-8")
    freeze(project)
    p = synth.plan(project, load_jobs(project))
    assert len(p["pending"]) == 1
    synth.run_flex(project, client, p["pending"], log=lambda *_: None)
    assert len(client.calls) == first_calls + 1


def test_quota_stops_run_and_unknown_blocks(project):
    setup_cast(project)
    script_mod.draft(project)
    script_mod.approve(project, ["s001", "s002"])
    freeze(project)
    jobs = load_jobs(project)
    project.config["limits"]["concurrency"] = 1
    project.config["limits"]["min_interval_s"] = 0
    client = FakeClient(ApiError("quota", status=429, retry_after="60"), fail_after=2)
    stats = synth.run_flex(project, client, synth.plan(project, jobs)["pending"], log=lambda *_: None)
    assert stats["ok"] == 2 and stats["failed"] == 1 and "429" in stats["stopped_reason"]
    assert sum(1 for c in client.calls if c[0] == "fail") == 1  # после 429 запросы не продолжаются

    client2 = FakeClient(ApiError("timeout", unknown_outcome=True), fail_after=0)
    pending = synth.plan(project, jobs)["pending"]
    synth.run_flex(project, client2, pending[:1], log=lambda *_: None)
    blocked = {b["job"] for b in synth.plan(project, jobs)["blocked"]}
    assert pending[0]["id"] in blocked  # неизвестный исход не повторяется сам


def test_script_check_catches_lost_text_and_tags(project):
    setup_cast(project)
    script_mod.draft(project)
    path = script_mod.script_path(project, "s001")
    raw = path.read_text(encoding="utf-8")
    raw = raw.replace("Вашер стоял у стены.", "Вашер стоял.").replace("Я ждал тебя", "Я <sigh> <whisper> ждал тебя")
    path.write_text(raw, encoding="utf-8")
    report = next(r for r in script_mod.check_all(project) if r["section"] == "s001")
    text = " ".join(report["errors"])
    assert "не совпадает" in text and "<whisper>" in text and "<sigh>" not in text


def test_attribution_import_validates(project):
    setup_cast(project)
    script_mod.draft(project)
    paths = attribute.prepare(project, ["s002"])
    name = paths[0].stem
    ids = read_json(paths[0].with_name(name + ".ids.json"))["ids"]
    segs = {s["id"]: s for s in script_mod.load_script(project, "s002")}
    bad = {"segments": [{"id": i, "speaker_id": "NARRATOR", "mode": "narration", "style": ""} for i in ids[:-1]]}
    write_json(project.attribution_dir / "responses" / f"{name}.json", bad)
    assert attribute.import_responses(project)["errors"]
    target = next(i for i in ids if segs[i]["text"].startswith("Сири смотрела"))
    good = {"segments": [{"id": i, "speaker_id": segs[i]["speaker_id"], "mode": segs[i]["mode"], "style": ""}
                         for i in ids if i != target],
            "splits": [{"id": target, "pieces": [
                {"text": "Сири смотрела на дорогу.", "speaker_id": "NARRATOR", "mode": "narration", "style": ""},
                {"text": "Она думала о доме.", "speaker_id": "SIRI", "mode": "thought", "style": "wistful"}]}],
            "new_roles": []}
    write_json(project.attribution_dir / "responses" / f"{name}.json", good)
    assert attribute.import_responses(project)["applied"] == 1
    updated = script_mod.load_script(project, "s002")
    assert any(s["mode"] == "thought" and s["speaker_id"] == "SIRI" for s in updated)
    assert not next(r for r in script_mod.check_all(project) if r["section"] == "s002")["errors"]


def test_split_long_respects_sentences():
    text = " ".join(f"Предложение номер {i} довольно длинное." for i in range(60))
    parts = split_long(text, 50)
    assert all(len(p.split()) <= 50 for p in parts)
    assert letters(" ".join(parts)) == letters(text)
