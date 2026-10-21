"""Панель затрат: сколько потрачено и сколько осталось, по журналу запросов.

  vox.py costs            текущий проект -> costs.html и сводка в консоли
  vox.py costs --all      все проекты в папке projects/ -> projects/COSTS.html

Оценка по прайсу из [pricing] book.toml: входные токены ≈ символы/2.5 (+ style),
выходные = длительность × audio_tokens_per_second. Batch и Flex — со скидкой batch_discount,
Standard — полная цена. Неудачные запросы Google не тарифицирует; «неизвестный исход»
(обрыв POST) показан отдельно как возможный расход. Это оценка — точные суммы в
Google Cloud Billing / AI Studio.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .util import read_jsonl, write_text, now_utc


def _price(pricing: dict, chars: float, seconds: float, discounted: bool) -> float:
    if pricing.get("usd_per_1m_input_tokens") is None or pricing.get("usd_per_1m_output_tokens") is None:
        return 0.0
    cost = ((chars / 2.5 + 60) * pricing["usd_per_1m_input_tokens"]
            + seconds * float(pricing["audio_tokens_per_second"]) * pricing["usd_per_1m_output_tokens"]) / 1e6
    return cost * (float(pricing["batch_discount"]) if discounted else 1.0)


def collect(project) -> dict:
    pricing = project.config["pricing"]
    events = read_jsonl(project.ledger_path)
    reserves: dict[str, dict] = {}
    days = defaultdict(lambda: {"usd": 0.0, "requests": 0, "seconds": 0.0})
    modes = defaultdict(lambda: {"usd": 0.0, "requests": 0})
    total = {"usd": 0.0, "requests": 0, "seconds": 0.0, "failures": 0, "unknown": 0, "unknown_usd": 0.0,
             "rejected": 0}
    for row in events:
        h = row["request_hash"]
        if row["event"] == "reserve":
            reserves[h] = row
        elif row["event"] == "success":
            res = reserves.get(h, {})
            seconds = float(row.get("duration_s") or 0)
            chars = float(res.get("chars") or seconds * 14)
            mode = "batch" if res.get("mode") == "batch" else res.get("tier") or res.get("mode", "?")
            discounted = res.get("mode") == "batch" or res.get("tier", "flex") == "flex"
            usd = _price(pricing, chars, seconds, discounted)
            day = row["utc"][:10]
            for bucket in (days[day], total):
                bucket["usd"] += usd
                bucket["requests"] += 1
                bucket["seconds"] += seconds
            modes[mode]["usd"] += usd
            modes[mode]["requests"] += 1
        elif row["event"] == "failure":
            total["failures"] += 1
        elif row["event"] == "unknown":
            total["unknown"] += 1
            res = reserves.get(h, {})
            total["unknown_usd"] += _price(pricing, float(res.get("chars") or 300), float(res.get("chars") or 300) / 14,
                                           res.get("mode") == "batch" or res.get("tier", "flex") == "flex")
        elif row["event"] == "reject":
            total["rejected"] += 1
    remaining = None
    if project.config["project"]["kind"] == "book" and project.active_path.exists():
        from . import synth
        from .freeze import load_jobs
        plan = synth.plan(project, load_jobs(project))
        remaining = {"requests": len(plan["pending"]), "usd": plan["est_cost_usd_batch_or_flex"] or 0.0,
                     "minutes": plan["est_audio_min"]}
    return {"project": project.config["book"]["title"] or project.root.name, "kind": project.config["project"]["kind"],
            "total": total, "days": dict(sorted(days.items())), "modes": dict(modes), "remaining": remaining}


def _usd(value: float) -> str:
    return f"${value:.2f}" if value >= 1 or value == 0 else f"${value:.4f}"


def render(reports: list[dict], title: str) -> str:
    days = defaultdict(float)
    for r in reports:
        for d, v in r["days"].items():
            days[d] += v["usd"]
    spent = sum(r["total"]["usd"] for r in reports)
    left = sum((r["remaining"] or {}).get("usd", 0) for r in reports)
    minutes = sum(r["total"]["seconds"] for r in reports) / 60
    unknown = sum(r["total"]["unknown"] for r in reports)
    peak = max(days.values(), default=0) or 1
    bars = "".join(
        f'<div class="col" title="{d}: {_usd(v)}"><div class="bar" style="height:{max(2, v / peak * 100):.1f}%"></div>'
        f'<span>{d[5:]}</span></div>' for d, v in sorted(days.items())[-45:])
    rows = "".join(
        f"<tr><td>{r['project']}</td><td>{r['kind']}</td><td class=n>{r['total']['requests']}</td>"
        f"<td class=n>{r['total']['seconds'] / 60:.1f}</td><td class=n>{_usd(r['total']['usd'])}</td>"
        f"<td class=n>{r['total']['failures']}</td><td class=n>{r['total']['unknown']}</td><td class=n>{r['total']['rejected']}</td>"
        f"<td class=n>{'—' if not r['remaining'] else '%s (%d запр.)' % (_usd(r['remaining']['usd']), r['remaining']['requests'])}</td></tr>"
        for r in reports)
    modes = defaultdict(lambda: [0, 0.0])
    for r in reports:
        for m, v in r["modes"].items():
            modes[m][0] += v["requests"]
            modes[m][1] += v["usd"]
    mode_rows = "".join(f"<tr><td>{m}</td><td class=n>{n}</td><td class=n>{_usd(u)}</td></tr>" for m, (n, u) in sorted(modes.items()))
    day_rows = "".join(f"<tr><td>{d}</td><td class=n>{_usd(v)}</td></tr>" for d, v in sorted(days.items(), reverse=True))
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Затраты на озвучку</title>
<style>
:root{{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--bar:#3d7a5c;--warn:#9a6b00}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--card:#1f1f1d;--fg:#ecebe6;--muted:#a3a29b;--line:#34332f;--bar:#6fbf95;--warn:#e0b45c}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}}main{{max-width:960px;margin:0 auto;padding:16px}}
h1{{font-size:20px}}h2{{font-size:15px;margin:24px 0 8px}}.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}}.tile b{{display:block;font-size:24px}}.tile span{{color:var(--muted);font-size:13px}}
.chart{{display:flex;align-items:flex-end;gap:2px;height:180px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 10px 24px;overflow-x:auto}}
.col{{flex:1;min-width:14px;max-width:48px;height:100%;display:flex;flex-direction:column;justify-content:flex-end;position:relative}}
.bar{{background:var(--bar);border-radius:4px 4px 0 0}}.col span{{position:absolute;bottom:-20px;left:0;font-size:10px;color:var(--muted);white-space:nowrap}}
.col:hover .bar{{opacity:.75}}table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;font-size:14px}}
td,th{{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left}}.n{{text-align:right;font-variant-numeric:tabular-nums}}
.wrap{{overflow-x:auto}}.note{{color:var(--muted);font-size:13px}}.warn{{color:var(--warn)}}
</style></head><body><main>
<h1>{title}</h1><p class="note">Оценка по журналу запросов и прайсу [pricing]; точные суммы — в биллинге Google. Обновлено {now_utc()}.</p>
<div class="tiles"><div class="tile"><b>{_usd(spent)}</b><span>потрачено (оценка)</span></div>
<div class="tile"><b>{_usd(left)}</b><span>осталось по планам книг</span></div>
<div class="tile"><b>{minutes:.1f} мин</b><span>получено аудио</span></div>
<div class="tile"><b class="{'warn' if unknown else ''}">{unknown}</b><span>запросов с неизвестным исходом</span></div></div>
<h2>Расход по дням, $</h2><div class="chart">{bars or '<span class="note">нет данных</span>'}</div>
<h2>Проекты</h2><div class="wrap"><table><tr><th>Проект</th><th>Тип</th><th class=n>Запросов</th><th class=n>Мин</th><th class=n>Потрачено</th><th class=n>Ошибок</th><th class=n>Неизв.</th><th class=n>Отклонено</th><th class=n>Осталось</th></tr>{rows}</table></div>
<h2>По режимам</h2><div class="wrap"><table><tr><th>Режим</th><th class=n>Запросов</th><th class=n>$</th></tr>{mode_rows}</table></div>
<h2>По дням</h2><div class="wrap"><table><tr><th>День</th><th class=n>$</th></tr>{day_rows}</table></div>
</main></body></html>"""


def run(project=None, everything: bool = False) -> tuple[list[dict], Path]:
    from .project import Project
    if everything:
        base = Path(__file__).resolve().parents[1] / "projects"
        projects = [Project.open(p.parent) for p in sorted(base.glob("*/book.toml"))]
        out = base / "COSTS.html"
        title = "Затраты на озвучку: все проекты"
    else:
        projects = [project]
        out = project.root / "costs.html"
        title = f"Затраты: {project.config['book']['title'] or project.root.name}"
    reports = [collect(p) for p in projects]
    write_text(out, render(reports, title))
    write_text(out.with_suffix(".json"), json.dumps(reports, ensure_ascii=False, indent=1))
    return reports, out
