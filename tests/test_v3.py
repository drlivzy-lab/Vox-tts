"""Улучшения v3: пилот, страница прослушивания, группировка ремарок, параллельная атрибуция,
библиотека голосов, Batch через Files API, субтитры по распознаванию, затраты, MCP."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from vox import attribute, costs, library, pilot, qc as qc_mod, review, script as script_mod, subs, synth
from vox.assemble import assemble
from vox.freeze import freeze, load_jobs
from vox.ledger import Ledger
from vox.util import HarnessError, read_json, write_json

from fakes import FakeClient
from test_pipeline import project, setup_cast  # noqa: F401  (фикстура)

quiet = dict(log=lambda *_: None)


def _ready(proj):
    setup_cast(proj)
    script_mod.draft(proj)
    script_mod.approve(proj, ["s001", "s002"])


def test_grouped_remarks_one_request_cut_and_assembled(project):  # noqa: F811
    _ready(project)
    project.config["chunking"]["group_remarks"] = True
    freeze(project)
    jobs = load_jobs(project)
    refs = [j for j in jobs if j.get("group_ref")]
    assert len(refs) == 1
    primary = next(j for j in jobs if j["id"] == refs[0]["group_ref"])
    assert primary["group_parts"] == ["сказал Вар.", "ответил Вашер."] and "\n" in primary["text"]
    assert refs[0]["request_hash"] == primary["request_hash"]
    client = FakeClient()
    pending = synth.plan(project, jobs)["pending"]
    assert refs[0]["id"] not in [j["id"] for j in pending]
    synth.run_flex(project, client, pending, **quiet)
    assert len(client.calls) == len(pending)  # группа — один запрос
    stats = qc_mod.run(project, **quiet)
    assert stats["suspect"] == 0
    chapters = assemble(project)
    meta = read_json(project.chapters_dir(project.active_release()) / "s001.json")
    ids = [t["job_id"] for t in meta["timeline"]]
    assert primary["id"] in ids and refs[0]["id"] in ids
    by_id = {t["job_id"]: t for t in meta["timeline"]}
    for job_id in (primary["id"], refs[0]["id"]):  # каждая часть — своя ремарка, ~0.6 с, а не вся группа
        assert 0.4 < by_id[job_id]["end_s"] - by_id[job_id]["start_s"] < 1.0
    assert chapters


def test_group_cut_failure_is_reported(project):  # noqa: F811
    _ready(project)
    project.config["chunking"]["group_remarks"] = True
    freeze(project)
    jobs = load_jobs(project)
    primary = next(j for j in jobs if j.get("group_parts"))
    from fakes import fake_wav
    ledger = Ledger(project)
    tech = {"duration_s": 1.0, "issues": []}
    ledger.store(primary, fake_wav(primary["text"].replace("\n", " ")), "test", tech)  # без паузы между частями
    qc_mod.run(project, **quiet)
    result = read_json(project.qc_dir(project.active_release()) / "jobs" / f"{primary['id']}.json")
    assert any(f["type"] == "нарезка группы" for f in result["findings"])


def test_batch_via_files_api(project):  # noqa: F811
    _ready(project)
    project.config["limits"]["batch_input"] = "file"
    freeze(project)
    jobs = load_jobs(project)
    client = FakeClient()
    names = synth.batch_prepare(project, synth.plan(project, jobs)["pending"], "all")
    manifest = read_json(project.batches_dir / names[0] / "manifest.json")
    assert manifest["input"] == "file" and (project.batches_dir / names[0] / "requests.jsonl").exists()
    synth.batch_submit(project, client, names[0])
    assert [c[0] for c in client.calls] == ["upload", "batch_file"]
    with pytest.raises(HarnessError):
        synth.batch_submit(project, client, names[0])
    result = synth.batch_collect(project, client, names[0])
    assert result["ok"] == manifest["requests"] and result["failed"] == 0
    assert (project.batches_dir / names[0] / "responses.jsonl").exists()


def test_review_page_and_apply(project, tmp_path):  # noqa: F811
    _ready(project)
    freeze(project)
    jobs = load_jobs(project)
    synth.run_flex(project, FakeClient(), synth.plan(project, jobs)["pending"], **quiet)
    qc_mod.run(project, **quiet)
    with pytest.raises(HarnessError):
        review.build(project)  # подозрений нет — без --all слушать нечего
    page = review.build(project, everything=True)
    html = page.read_text(encoding="utf-8")
    assert "../../audio/cache/" in html and jobs[0]["id"] in html and "</script>" in html
    decisions = tmp_path / "d.json"
    decisions.write_text(json.dumps({"release": project.active_release(), "decisions": {
        jobs[0]["id"]: {"verdict": "reject", "note": "повтор"}, jobs[1]["id"]: {"verdict": "accept"}}}), encoding="utf-8")
    assert review.apply(project, decisions) == {"accept": 1, "reject": 1, "skipped": []}
    assert [j["id"] for j in synth.plan(project, jobs)["pending"]] == [jobs[0]["id"]]


def test_pilot_calibrates_plan(project):  # noqa: F811
    setup_cast(project)
    script_mod.draft(project)  # пилот можно до утверждения
    result = pilot.prepare(project, ["s001"])
    assert result["plan"]["pending"]
    out = pilot.run(project, FakeClient(), **quiet)
    calib = read_json(project.root / "pilot" / "calibration.json")
    assert calib["chars_per_second"] and "VAHR" in calib["speakers"]
    text = (project.root / "pilot" / "CALIBRATION.md").read_text(encoding="utf-8")
    assert "Что прослушать" in text and out["synth"]["ok"] > 0
    assert synth.plan(project, load_jobs(project))["chars_per_second"] == calib["chars_per_second"]
    from vox.advisor import _stale_release
    assert _stale_release(project)  # пилотный релиз не годится для всей книги


def test_parallel_attribution_claims(project):  # noqa: F811
    setup_cast(project)
    script_mod.draft(project)
    attribute.prepare(project)
    a = attribute.claim(project, "agentA")
    b = attribute.claim(project, "agentB")
    assert a and b and a != b
    assert attribute.claim(project, "agentC", count=5) == []  # всё разобрано
    states = {r["packet"]: r for r in attribute.status(project)}
    assert states[a[0]]["state"] == "claimed" and states[a[0]]["worker"] == "agentA"
    path = attribute.roles_report(project)
    assert "VAHR" in path.read_text(encoding="utf-8")


def test_voice_library(project, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("VOX_LIBRARY", str(tmp_path / "lib"))
    library.register("brand", "Kore", description="фирменный", style="Warm, confident.")
    old = (datetime.now(timezone.utc) - timedelta(days=350)).replace(microsecond=0).isoformat()
    library.register("old-narrator", "voice_abc", created=old)
    assert [v["name"] for v in library.expiring(30)] == ["old-narrator"]
    library.use(project, "brand", "V09")
    from vox import cast as cast_mod, clips
    assert cast_mod.load(project)["profiles"]["V09"]["voice"] == "Kore"
    req = clips.build_requests(project, {"id": "s", "voice": "brand", "text": "Привет"})[0]
    assert req["core"]["voice"] == "Kore" and req["core"]["style"] == "Warm, confident."


def test_subtitles_alignment():
    observed = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4}
                for i, w in enumerate("вкус который помнишь новый северный".split())]
    aligned = subs.align("Вкус, который ПОМНИШЬ. <short pause> Новый «Северный мёд».", observed)
    assert [a["word"] for a in aligned][-1] == "мёд»." and aligned[3]["start"] == 1.5
    text = subs.srt(aligned)
    assert text.count("-->") == 2 and "Вкус, который ПОМНИШЬ." in text


def test_costs_dashboard(project):  # noqa: F811
    _ready(project)
    freeze(project)
    jobs = load_jobs(project)
    pending = synth.plan(project, jobs)["pending"]
    synth.run_flex(project, FakeClient(), pending[:2], **quiet)
    reports, path = costs.run(project)
    assert reports[0]["total"]["requests"] == 2 and reports[0]["total"]["usd"] > 0
    assert reports[0]["remaining"]["requests"] == len(pending) - 2
    assert "Расход по дням" in path.read_text(encoding="utf-8")


def test_mcp_blocks_paid_commands():
    import mcp_server
    assert mcp_server._paid(["synth"]) and mcp_server._paid(["clips", "render", "x.md"])
    assert mcp_server._paid(["plan", "--yes"]) and not mcp_server._paid(["clips", "plan", "x.md"])
    text, error = mcp_server.call("vox", {"command": "say Привет --voice Kore"})
    assert error and "Отклонено" in text
