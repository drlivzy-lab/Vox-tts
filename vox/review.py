"""Страница прослушивания релиза: qc/<релиз>/review.html.

Открывается двойным щелчком (файл, без сервера): плеер каждого запроса, текст,
находки QC с переходом к нужной секунде, кнопки «принять» / «отклонить» и заметка.
Решения хранятся в браузере и выгружаются кнопкой «Скачать решения» в JSON, который
применяется командой `vox.py qc apply <файл>` (те же правила, что qc accept/reject).
Клавиши: J/K — следующий/предыдущий, Пробел — играть, A — принять, R — отклонить.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import qc as qc_mod
from .freeze import load_jobs
from .ledger import Ledger
from .util import HarnessError, read_json, write_text, now_utc


def build(project, sections: list[str] | None = None, everything: bool = False) -> Path:
    release = project.active_release()
    jobs = [j for j in load_jobs(project, release) if not j.get("group_ref")]
    if sections:
        jobs = [j for j in jobs if j["section"] in sections]
    ledger = Ledger(project)
    folder = project.qc_dir(release)
    decisions = read_json(qc_mod.decisions_path(project, release)) if qc_mod.decisions_path(project, release).exists() else {}
    open_ids = {r["job_id"] for r in qc_mod.open_issues(project, release, jobs)}
    items = []
    for job in jobs:
        if not ledger.has(job["request_hash"]):
            continue
        result_path = folder / "jobs" / f"{job['id']}.json"
        result = read_json(result_path) if result_path.exists() else {}
        if not everything and job["id"] not in open_ids:
            continue
        audio = Path(os.path.relpath(ledger.audio_path(job["request_hash"]), folder)).as_posix()
        items.append({
            "id": job["id"], "section": job["section"], "speaker": job["speaker_id"], "voice": job["voice"],
            "style": job["style"], "mode": job["mode"], "text": job["text"], "audio": audio,
            "status": result.get("status", "unchecked"), "open": job["id"] in open_ids,
            "findings": [{"type": f["type"], "detail": f.get("observed") or f.get("detail") or f.get("expected", ""),
                          "start": f.get("start_s"), "source": f.get("source", "")} for f in result.get("findings", [])],
            "transcripts": {k: v for k, v in result.get("transcripts", {}).items() if not k.endswith(":tokens")},
            "decision": decisions.get(job["id"], {}).get("verdict"),
            "duration": result.get("technical", {}).get("duration_s"),
        })
    if not items:
        raise HarnessError("Нечего слушать: нет аудио" + ("" if everything else " с открытыми подозрениями (добавьте --all)"))
    title = f"{project.config['book']['title'] or project.root.name} — {release}"
    data = {"release": release, "title": title, "built": now_utc(), "items": items, "all": everything}
    path = folder / ("review_all.html" if everything else "review.html")
    write_text(path, PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
               .replace("__TITLE__", title.replace("<", "&lt;")))
    return path


def apply(project, path: Path) -> dict:
    """Применить решения, выгруженные со страницы прослушивания."""
    data = read_json(path)
    release = project.active_release()
    if data.get("release") != release:
        raise HarnessError(f"Решения для релиза {data.get('release')}, активный — {release}")
    done = {"accept": 0, "reject": 0, "skipped": []}
    for job_id, row in sorted(data.get("decisions", {}).items()):
        verdict = row.get("verdict")
        if verdict not in ("accept", "reject"):
            continue
        try:
            qc_mod.decide(project, job_id, verdict, (row.get("note") or "").strip()[:300] or "страница прослушивания")
            done[verdict] += 1
        except HarnessError as exc:
            done["skipped"].append(f"{job_id}: {exc}")
    return done


PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Прослушивание — __TITLE__</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--acc:#2d6a4f;--bad:#b23a3a;--warn:#9a6b00}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--fg:#ecebe6;--muted:#a3a29b;--line:#34332f;--acc:#6fbf95;--bad:#e07b7b;--warn:#e0b45c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px;z-index:2}
h1{font-size:17px;margin:0 0 8px}.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
select,input,button{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:5px 9px}
button{cursor:pointer}button.primary{background:var(--acc);color:#fff;border-color:var(--acc)}
main{max-width:960px;margin:0 auto;padding:12px 16px 80px}
.item{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:10px 0}
.item.cur{outline:2px solid var(--acc)}.meta{color:var(--muted);font-size:13px;display:flex;flex-wrap:wrap;gap:10px}
.text{white-space:pre-wrap;margin:8px 0}audio{width:100%;margin:4px 0}
.f{font-size:13px;margin:2px 0}.f a{color:var(--warn);cursor:pointer;text-decoration:underline}
.acts{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px}.acts input{flex:1;min-width:180px}
.v-accept{border-left:4px solid var(--acc)}.v-reject{border-left:4px solid var(--bad)}
.tag{font-size:12px;padding:1px 7px;border-radius:9px;border:1px solid var(--line)}
details{font-size:13px;color:var(--muted)}.count{color:var(--muted);font-size:13px}
</style></head><body>
<header><h1>Прослушивание: __TITLE__</h1>
<div class="bar">
 <select id="flt"><option value="open">Открытые подозрения</option><option value="all">Все</option><option value="undecided">Без решения</option></select>
 <select id="spk"><option value="">Все голоса</option></select>
 <span class="count" id="cnt"></span>
 <button class="primary" id="exp">Скачать решения</button>
 <button id="clr">Сбросить решения</button>
</div>
<div class="count">J/K — следующий/предыдущий · Пробел — играть · A — принять · R — отклонить. Затем: <code id="cmd"></code></div>
</header>
<main id="list"></main>
<script>
const DATA=__DATA__;const KEY="vox-review-"+DATA.release+"-"+DATA.title;
let dec={};try{dec=JSON.parse(localStorage.getItem(KEY)||"{}")}catch(e){}
const save=()=>{try{localStorage.setItem(KEY,JSON.stringify(dec))}catch(e){}};
const $=s=>document.querySelector(s),list=$("#list");let cur=0,shown=[];
$("#cmd").textContent="python vox.py qc apply decisions-"+DATA.release+".json";if(DATA.all)$("#flt").value="all";
[...new Set(DATA.items.map(i=>i.speaker))].sort().forEach(s=>{const o=document.createElement("option");o.value=s;o.textContent=s;$("#spk").append(o)});
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function verdict(i){return (dec[i.id]||{}).verdict||i.decision||""}
function render(){const f=$("#flt").value,s=$("#spk").value;
 shown=DATA.items.filter(i=>(!s||i.speaker===s)&&(f==="all"||(f==="open"&&i.open)||(f==="undecided"&&!verdict(i))));
 list.innerHTML=shown.map((i,n)=>`<div class="item v-${verdict(i)}" id="it${n}">
 <div class="meta"><b>${esc(i.id)}</b><span>${esc(i.speaker)} · ${esc(i.voice)}</span><span>${esc(i.mode)}</span>
 ${i.duration?`<span>${i.duration} с</span>`:""}<span class="tag">${esc(i.status)}</span>${verdict(i)?`<span class="tag">${verdict(i)==="accept"?"принято":"отклонено"}</span>`:""}</div>
 ${i.style?`<div class="meta">style: ${esc(i.style)}</div>`:""}
 <audio controls preload="none" src="${esc(i.audio)}"></audio>
 <div class="text">${esc(i.text)}</div>
 ${i.findings.map(f=>`<div class="f">⚠ ${esc(f.type)}: ${esc(f.detail)} ${f.start!=null?`<a data-n="${n}" data-t="${f.start}">${f.start} с</a>`:""} <span class="count">${esc(f.source)}</span></div>`).join("")}
 ${Object.keys(i.transcripts).length?`<details><summary>Расшифровка</summary>${Object.entries(i.transcripts).map(([k,v])=>`<div><b>${esc(k)}</b>: ${esc(v)}</div>`).join("")}</details>`:""}
 <div class="acts"><button data-v="accept" data-n="${n}">Принять (A)</button><button data-v="reject" data-n="${n}">Отклонить (R)</button>
 <input data-note="${n}" placeholder="заметка: что не так" value="${esc((dec[i.id]||{}).note||"")}"></div></div>`).join("");
 $("#cnt").textContent=`показано ${shown.length} из ${DATA.items.length}; решений: ${Object.keys(dec).length}`;
 cur=Math.min(cur,Math.max(0,shown.length-1));mark(false)}
function mark(scroll){document.querySelectorAll(".item").forEach(e=>e.classList.remove("cur"));const e=$("#it"+cur);if(e){e.classList.add("cur");if(scroll)e.scrollIntoView({block:"center",behavior:"smooth"})}}
function decide(n,v){const i=shown[n];if(!i)return;const note=(document.querySelector(`[data-note="${n}"]`)||{}).value||"";
 dec[i.id]={verdict:v,note};save();const keep=cur;render();cur=Math.min(keep+1,shown.length-1);mark(true)}
list.addEventListener("click",e=>{const t=e.target;if(t.dataset.v)decide(+t.dataset.n,t.dataset.v);
 if(t.dataset.t){const a=$("#it"+t.dataset.n+" audio");a.currentTime=Math.max(0,+t.dataset.t-1);a.play()}});
list.addEventListener("change",e=>{const n=e.target.dataset.note;if(n!=null){const i=shown[+n];dec[i.id]=Object.assign(dec[i.id]||{},{note:e.target.value});save()}});
document.addEventListener("keydown",e=>{if(e.target.tagName==="INPUT")return;const a=$("#it"+cur+" audio");
 if(e.key==="j"||e.key==="о"){cur=Math.min(cur+1,shown.length-1);mark(true)}else if(e.key==="k"||e.key==="л"){cur=Math.max(cur-1,0);mark(true)}
 else if(e.key===" "&&a){e.preventDefault();a.paused?a.play():a.pause()}else if(e.key==="a"||e.key==="ф")decide(cur,"accept");else if(e.key==="r"||e.key==="к")decide(cur,"reject")});
$("#flt").onchange=$("#spk").onchange=()=>{cur=0;render()};
$("#clr").onclick=()=>{if(confirm("Сбросить решения на этой странице?")){dec={};save();render()}};
$("#exp").onclick=()=>{const b=new Blob([JSON.stringify({release:DATA.release,decisions:dec},null,1)],{type:"application/json"});
 const a=document.createElement("a");a.href=URL.createObjectURL(b);a.download="decisions-"+DATA.release+".json";a.click()};
render();
</script></body></html>
"""
