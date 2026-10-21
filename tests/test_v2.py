import os
import shutil
from pathlib import Path

import pytest

from vox import attribute, clips, env, script as script_mod
from vox.advisor import steps
from vox.cli import main
from vox.gemini import dialogue_request, interactions_payload
from vox.project import Project
from vox.util import HarnessError, letters, read_json, write_json

from fakes import FakeClient
from test_pipeline import project, setup_cast  # noqa: F401  (фикстура)


def test_env_parse(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text('# c\nGEMINI_API_KEY="abc123"\nexport OTHER=x # comment\n', encoding="utf-8")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    env.load(tmp_path)
    assert os.environ["GEMINI_API_KEY"] == "abc123" and os.environ["OTHER"] == "x"
    monkeypatch.setenv("GEMINI_API_KEY", "process")
    env.load(tmp_path)
    assert os.environ["GEMINI_API_KEY"] == "process"  # окружение процесса важнее


def test_letters_ignore_tags_caps_yo():
    assert letters("Я <sigh> ждал ЕЁ.") == letters("я ждал ее")
    assert letters("Я |угу| ждал") != letters("Я ждал")


def test_dialogue_payload_rules():
    core = dialogue_request("m", [{"speaker": "A", "text": "Привет"}, {"speaker": "B", "text": "Здравствуй"}],
                            {"A": "Kore", "B": "Puck"})
    payload = interactions_payload(core, "flex")
    assert payload["generation_config"]["speech_config"]["mode"] == "conversational"
    assert all(c["annotations"][0]["speaker"] in ("A", "B") for c in payload["input"][0]["content"])
    with pytest.raises(HarnessError):
        dialogue_request("m", [{"speaker": "A", "text": "x"}], {"A": "voice_123", "B": "Puck"})
    with pytest.raises(HarnessError):
        dialogue_request("m", [{"speaker": "C", "text": "x"}], {"A": "Kore", "B": "Puck"})


def test_clips_render_takes_cache_and_exports(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("нужен ffmpeg")
    assert main(["init", str(tmp_path / "studio"), "--kind", "clips"]) == 0
    proj = Project.open(tmp_path / "studio")
    assert proj.config["tts"]["mode"] == "flex"
    script = tmp_path / "studio" / "clips" / "ad.md"
    script.write_text('''### slogan

```yaml
voice: Kore
style: "Warm, confident."
takes: 2
```

```text
Вкус, который ПОМНИШЬ. <short pause> Новый «Северный мёд».
```

### duo

```yaml
speakers: [Аня=Kore, Олег=Puck]
```

```text
Аня: Ты уже пробовал?
Олег: Третий день не могу остановиться!
```
''', encoding="utf-8")
    blocks = clips.load_blocks(script)
    client = FakeClient()
    result = clips.render(proj, client, "ad", blocks, formats=["wav", "mp3"], log=lambda *_: None)
    assert result["new_requests"] == 3 and result["rendered"] == 3
    out = tmp_path / "studio" / "out" / "ad"
    assert (out / "slogan" / "take-2.mp3").exists() and (out / "slogan" / "take-1.srt").read_text(encoding="utf-8")
    again = clips.render(proj, client, "ad", blocks, formats=["wav"], log=lambda *_: None)
    assert again["new_requests"] == 0  # дубли из кэша, без новых оплат
    from vox import post
    loud = post.measure(post.ffmpeg_path(), out / "slogan" / "take-1.wav")
    assert abs(loud["lufs"] - (-14)) < 1.5
    bad = [{"id": "x", "voice": "Kore", "text": "Привет <whisper> мир"}]
    with pytest.raises(HarnessError):
        clips.plan(proj, bad)


def test_attribution_text_edits_and_review(project):  # noqa: F811
    setup_cast(project)
    script_mod.draft(project)
    paths = attribute.prepare(project, ["s001"])
    text = paths[0].read_text(encoding="utf-8")
    assert "confidence" in text and "<short pause>" in text and "Пример" in text
    name = paths[0].stem
    ids = read_json(paths[0].with_name(name + ".ids.json"))["ids"]
    segs = {s["id"]: s for s in script_mod.load_script(project, "s001")}
    rows = []
    for i in ids:
        row = {"id": i, "speaker_id": segs[i]["speaker_id"], "mode": segs[i]["mode"], "style": "",
               "confidence": "high", "evidence": "ремарка"}
        if segs[i]["text"].startswith("Я ждал"):
            row.update(text="Я ждал тебя с РАССВЕТА. <sigh>", style="Weary reproach.", confidence="low",
                       evidence="угадано")
        rows.append(row)
    write_json(project.attribution_dir / "responses" / f"{name}.json", {"segments": rows})
    result = attribute.import_responses(project)
    assert result["applied"] == 1 and result["to_review"] == 1
    assert "РАССВЕТА" in (project.script_dir / "s001.md").read_text(encoding="utf-8")
    assert not next(r for r in script_mod.check_all(project) if r["section"] == "s001")["errors"]
    # правка, меняющая слова, отклоняется
    attribute.prepare(project, ["s001"], force=True)
    (project.attribution_dir / "applied" / f"{name}.json").unlink()
    rows[0]["text"] = "Совсем другие слова"
    write_json(project.attribution_dir / "responses" / f"{name}.json", {"segments": rows})
    assert attribute.import_responses(project)["errors"]


def test_next_guides_from_start(tmp_path):
    from test_pipeline import FB2
    src = tmp_path / "b.fb2"
    src.write_text(FB2, encoding="utf-8")
    main(["init", str(tmp_path / "p"), "--source", str(src)])
    proj = Project.open(tmp_path / "p")
    assert steps(proj)[0]["command"].endswith("ingest")
    main(["--book", str(tmp_path / "p"), "ingest"])
    assert steps(proj)[0]["command"].endswith("draft")
    main(["--book", str(tmp_path / "p"), "draft"])
    assert "attribute prepare" in steps(proj)[0]["command"]
