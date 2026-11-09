"""Выкладка плеера на сервер (player/tools/deploy_site.py + deploy_remote.py) без сети.

Чистые функции проверяются везде. Полный цикл (stage → tar по «ssh» → атомарный обмен → откат)
выполняется только на Linux: ssh и docker подменены локальными заглушками, остальное настоящее.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "player" / "tools"
TOKEN = "T" * 8 + "abcdefghijklmnop_-1234"
PREFIX = f"/listen/{TOKEN}/"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ds = _load("deploy_site")
dr = _load("deploy_remote")

LINUX = sys.platform.startswith("linux") and shutil.which("tar") and shutil.which("bash")


def _cfg(tmp_path, **extra):
    data = {"ssh_host": "fake", "public_host": "example.test", "prefix": PREFIX,
            "landing_dir": str(tmp_path / "landing"), "journal_dir": str(tmp_path / "journal"),
            "aliases": {"audio/old.m4b": "books/b1/book.m4b"}, **extra}
    path = tmp_path / "deploy.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return ds.load_config(path)


def _site(root: Path, audio: bytes = b"A" * 300_000, app: str = "console.log(1)\n" * 200, chunk: int = 65536):
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<!doctype html><title>player</title>", encoding="utf-8")
    (root / "sw.js").write_text("self.x=1", encoding="utf-8")
    (root / "app").mkdir(exist_ok=True)
    (root / "app" / "main.mjs").write_text(app, encoding="utf-8")
    book = root / "books" / "b1"
    book.mkdir(parents=True, exist_ok=True)
    (book / "book.m4b").write_bytes(audio)
    (book / "cover.jpg").write_bytes(b"jpg")
    sha = hashlib.sha256(audio).hexdigest()
    chunks = [{"size": len(audio[i:i + chunk]), "sha256": hashlib.sha256(audio[i:i + chunk]).hexdigest()}
              for i in range(0, len(audio), chunk)]
    meta = {"id": "b1", "version": f"v-{sha[:12]}", "title": "Книга",
            "audio": {"url": f"book.m4b?v={sha[:12]}", "size": len(audio), "sha256": sha, "chunkSize": chunk,
                      "chunks": chunks},
            "cover": "cover.jpg", "images": {"cover": {"src": "cover.jpg"}}}
    (book / "book.json").write_text(json.dumps(meta), encoding="utf-8")
    (root / "library.json").write_text(json.dumps({"books": [
        {"id": "b1", "path": "books/b1/book.json", "version": meta["version"]}]}), encoding="utf-8")
    return root


# ---------- настройки, сборка, план ----------

def test_config_requires_secret_prefix(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg["token"] == TOKEN and cfg["ssh"] == "ssh" and cfg["container"] == "behappy-landing"
    for bad in ("/listen/short/", "/other/" + TOKEN + "/", "/listen/" + TOKEN):
        with pytest.raises(ds.DeployError):
            _cfg(tmp_path, prefix=bad)
    with pytest.raises(ds.DeployError):
        _cfg(tmp_path, aliases={"../x": "books/b1/book.m4b"})


def test_redact_hides_token_everywhere(tmp_path):
    cfg = _cfg(tmp_path)
    data = ds.redact({"url": f"https://h{PREFIX}", "list": [f"/root/html{PREFIX}a", 5], PREFIX: True}, cfg["token"])
    assert TOKEN not in json.dumps(data) and data["list"][1] == 5
    journal = ds.Journal(cfg, "deploy", "20260101-000000")
    journal(status="x", path=f"/srv{PREFIX}")
    assert TOKEN not in journal.path.read_text(encoding="utf-8")


def test_scan_site_checks_book_integrity(tmp_path):
    site = _site(tmp_path / "site")
    scan = ds.scan_site(site, {"audio/old.m4b": "books/b1/book.m4b"})
    assert set(scan["files"]) == {"index.html", "sw.js", "app/main.mjs", "library.json", "books/b1/book.json",
                                  "books/b1/book.m4b", "books/b1/cover.jpg"}
    assert scan["books"][0]["id"] == "b1"
    (site / "books" / "b1" / "book.m4b").write_bytes(b"B" * 300_000)  # аудио не то, что в book.json
    with pytest.raises(ds.DeployError, match="не совпадает с book.json"):
        ds.scan_site(site, {})
    site = _site(tmp_path / "site2")
    (site / "books" / "b1" / "cover.jpg").unlink()
    with pytest.raises(ds.DeployError, match="нет иллюстрации"):
        ds.scan_site(site, {})
    site = _site(tmp_path / "site3")
    (site / "books" / "b1" / "book.m4b.tmp").write_bytes(b"x")
    with pytest.raises(ds.DeployError, match="служебный файл"):
        ds.scan_site(site, {})
    with pytest.raises(ds.DeployError, match="псевдоним"):
        ds.scan_site(_site(tmp_path / "site4"), {"audio/x.m4b": "books/b1/missing.m4b"})


def test_make_plan_links_unchanged_and_moved_files():
    local = {"a": {"bytes": 1, "sha256": "1"}, "big": {"bytes": 9, "sha256": "9"}, "moved": {"bytes": 2, "sha256": "2"},
             "new": {"bytes": 3, "sha256": "3"}}
    remote = {"a": {"bytes": 1, "sha256": "1"}, "big": {"bytes": 9, "sha256": "9"}, "alias": {"bytes": 9, "sha256": "9"},
              "old/place": {"bytes": 2, "sha256": "2"}, "gone": {"bytes": 4, "sha256": "4"}}
    plan = ds.make_plan(local, remote, {"alias": "big"})
    assert sorted(plan["links"]) == [["a", "a"], ["big", "big"], ["moved", "old/place"]]
    assert plan["upload"] == ["new"] and plan["upload_bytes"] == 3
    assert plan["removed"] == ["gone", "old/place"] and plan["changed"] == ["moved", "new"]
    same = ds.make_plan({"a": local["a"], "big": local["big"]},
                        {"a": remote["a"], "big": remote["big"], "alias": remote["alias"]}, {"alias": "big"})
    assert same["identical"] and same["upload"] == []


# ---------- nginx ----------

CONF = """server {
    listen 80;
    absolute_redirect off;

    location / {
        root /usr/share/nginx/html;
        try_files $uri $uri/ /index.html;
    }
}
"""


def test_nginx_block_insert_find_and_compare(tmp_path):
    cfg = _cfg(tmp_path)
    block = ds.nginx_block(cfg)
    assert f"location = /listen/{TOKEN} {{" in block and f"return 302 {PREFIX};" in block
    assert "audio/mpeg mp3;" in block and "image/webp webp;" in block
    text = dr.insert_block(CONF, block)
    assert text.index("absolute_redirect off;") < text.index(block) < text.index("location / {")
    marker = f"audiobook-test:{TOKEN}"
    start, end = dr.find_block(text, marker, PREFIX)
    assert text[start:end] == block
    # другой комментарий-маркер и отступы не считаются отличием
    variant = block.replace("managed by", "restored, managed by").replace("        index", "      index")
    assert dr.strip_conf(variant) == dr.strip_conf(block)
    assert dr.strip_conf(block.replace(" mp3;", ";")) != dr.strip_conf(block)
    without_anchor = CONF.replace("    absolute_redirect off;\n", "")
    assert dr.insert_block(without_anchor, block).index(block) < dr.insert_block(without_anchor, block).index("location / {")
    with pytest.raises(dr.RemoteError):
        dr.insert_block("server {}\n", block)


def test_remote_paths_reject_foreign_dirs(tmp_path):
    with pytest.raises(dr.RemoteError):
        dr.paths({"landing": str(tmp_path), "prefix": "/listen/x/"})
    with pytest.raises(dr.RemoteError):
        dr.service_dir(tmp_path, "html")
    with pytest.raises(dr.RemoteError):
        dr.service_dir(tmp_path, ".player-stage-20260101-000000", "backup")
    assert dr.service_dir(tmp_path, ".player-backup-20260101-000000-replaced", "backup").parent == tmp_path


# ---------- полный цикл на Linux ----------

FAKE_SSH = """#!/usr/bin/env python3
import os, subprocess, sys
args = sys.argv[1:]
while args and args[0] == "-o":
    args = args[2:]
args = args[1:]  # имя хоста
sys.exit(subprocess.call(["bash", "-c", " ".join(args)]))
"""

FAKE_DOCKER = """#!/usr/bin/env python3
import hashlib, os, sys
conf = os.environ["FAKE_NGINX_CONF"]
a = sys.argv[1:]
if a[0] == "inspect":
    print("true")
elif a[0] == "exec" and a[2] == "sha256sum":
    print(hashlib.sha256(open(conf, "rb").read()).hexdigest(), a[3])
elif a[0] == "exec" and a[2] == "nginx":
    sys.exit(1 if b"BROKEN" in open(conf, "rb").read() else 0)
"""


@pytest.fixture
def server(tmp_path, monkeypatch):
    if not LINUX:
        pytest.skip("атомарный обмен каталогов (renameat2) и GNU tar — только Linux")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, src in (("fake-ssh", FAKE_SSH), ("docker", FAKE_DOCKER)):
        (bin_dir / name).write_text(src, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    landing = tmp_path / "landing"
    (landing / "html").mkdir(parents=True)
    (landing / "html" / "index.html").write_text("landing", encoding="utf-8")
    (landing / "nginx.conf").write_text(CONF, encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_NGINX_CONF", str(landing / "nginx.conf"))
    monkeypatch.setattr(ds, "fetch", lambda url, *a, **k: (200, {}, b"landing"))
    checks = {"fail": False}

    def fake_checks(cfg, manifest, home_before=None):
        return (["ok"], ["сломано"]) if checks["fail"] else (["ok"] * len(manifest["files"]), [])
    monkeypatch.setattr(ds, "https_checks", fake_checks)
    cfg = _cfg(tmp_path, ssh=str(bin_dir / "fake-ssh"))
    return cfg, landing, checks


def _run(cfg, *argv):
    return ds.main(["--config", str(Path(cfg["journal_dir"]).parent / "deploy.json"), *argv])


def _live(landing):
    return landing / "html" / PREFIX.strip("/")


def test_full_cycle_deploy_update_rollback_prune(tmp_path, server):
    cfg, landing, checks = server
    site = _site(tmp_path / "site")
    inode_before = (landing / "nginx.conf").stat().st_ino

    assert _run(cfg, "deploy", "--site", str(site)) == 1  # блока плеера в nginx ещё нет
    assert not _live(landing).exists()
    assert _run(cfg, "nginx", "--apply") == 0
    conf = (landing / "nginx.conf").read_text(encoding="utf-8")
    assert f"location ^~ {PREFIX}" in conf and (landing / "nginx.conf").stat().st_ino == inode_before
    assert _run(cfg, "nginx", "--apply") == 0  # повторно ничего не меняет
    assert len(list(landing.glob("nginx.conf.before-player-*"))) == 1

    assert _run(cfg, "deploy", "--site", str(site)) == 0  # первая выкладка
    live = _live(landing)
    assert (live / "app" / "main.mjs").read_bytes() == (site / "app" / "main.mjs").read_bytes()
    assert (live / "audio" / "old.m4b").stat().st_ino == (live / "books" / "b1" / "book.m4b").stat().st_ino
    assert oct((live / "index.html").stat().st_mode & 0o777) == "0o644"
    assert _run(cfg, "deploy", "--site", str(site)) == 0  # идентичная сборка — ничего не делает
    assert not list(landing.glob(".player-backup-*"))

    audio_ino = (live / "books" / "b1" / "book.m4b").stat().st_ino
    (site / "app" / "main.mjs").write_text("console.log(2)\n" * 200, encoding="utf-8")
    assert _run(cfg, "deploy", "--site", str(site)) == 0  # обновление: аудио не загружается заново
    backups = sorted(landing.glob(".player-backup-*"))
    assert len(backups) == 1 and "console.log(1)" in (backups[0] / "app" / "main.mjs").read_text()
    assert (live / "books" / "b1" / "book.m4b").stat().st_ino == audio_ino
    assert "console.log(2)" in (live / "app" / "main.mjs").read_text()
    assert not list(landing.glob(".player-stage-*"))

    checks["fail"] = True
    (site / "app" / "main.mjs").write_text("console.log(3)\n" * 200, encoding="utf-8")
    import time
    time.sleep(1.1)  # новая метка времени для служебных каталогов
    assert _run(cfg, "deploy", "--site", str(site)) == 1  # проверка не прошла → откат
    assert "console.log(2)" in (live / "app" / "main.mjs").read_text()
    failed = list(landing.glob(".player-failed-*"))
    assert len(failed) == 1 and "console.log(3)" in (failed[0] / "app" / "main.mjs").read_text()
    checks["fail"] = False

    time.sleep(1.1)
    assert _run(cfg, "rollback") == 0
    assert "console.log(1)" in (live / "app" / "main.mjs").read_text()
    assert any("console.log(2)" in (b / "app" / "main.mjs").read_text() for b in landing.glob(".player-backup-*"))

    assert _run(cfg, "prune", "--keep", "1") == 0  # без --yes только список
    assert list(landing.glob(".player-failed-*"))
    assert _run(cfg, "prune", "--keep", "1", "--yes") == 0
    assert not list(landing.glob(".player-failed-*")) and len(list(landing.glob(".player-backup-*"))) == 1
    journals = list(Path(cfg["journal_dir"]).glob("*.json"))
    assert journals and all(TOKEN not in j.read_text(encoding="utf-8") for j in journals)


def test_nginx_apply_restores_file_when_test_fails(tmp_path, server):
    cfg, landing, _ = server
    broken = CONF.replace("listen 80;", "listen 80; # BROKEN")
    (landing / "nginx.conf").write_text(broken, encoding="utf-8")
    assert _run(cfg, "nginx", "--apply") == 1
    assert (landing / "nginx.conf").read_text(encoding="utf-8") == broken


def test_activate_refuses_when_live_changed(tmp_path, server):
    cfg, landing, _ = server
    site = _site(tmp_path / "site")
    assert _run(cfg, "nginx", "--apply") == 0 and _run(cfg, "deploy", "--site", str(site)) == 0
    server_obj = ds.Server(cfg)
    info = server_obj.call("inspect")
    (_live(landing) / "extra.txt").write_text("x", encoding="utf-8")  # кто-то поменял плеер после проверки
    server_obj.call("stage", stage=".player-stage-20990101-000000",
                    links=[[rel, rel] for rel in info["files"] if rel != "audio/old.m4b"])
    with pytest.raises(ds.RemoteFailure, match="изменился"):
        server_obj.call("activate", stage=".player-stage-20990101-000000", backup=".player-backup-20990101-000000",
                        expected={k: v for k, v in info["files"].items() if k != "audio/old.m4b"},
                        aliases=cfg["aliases"], live_fingerprint=info["fingerprint"])
    assert (_live(landing) / "extra.txt").exists() and not list(landing.glob(".player-backup-*"))
