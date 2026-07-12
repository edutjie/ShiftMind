"""Q&A agent router — `/api/qa.html` page + `POST /api/qa/ask`.

Lets an employee ask free-form business-insight questions over the collected
conversation history. The agent (qa_service) calls run_sql / vector_search
tools and returns an answer + tool-call trace + source sessions.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from auth import active_login
from doubao_asr import DoubaoSeedAsrStream
import qa_service

router = APIRouter()


class QaFilters(BaseModel):
    location_id: str | None = None
    employee_id: str | None = None
    topic: str | None = None
    date_from: str | None = None      # YYYY-MM-DD
    date_to: str | None = None
    simulated: str = "all"            # all | real | sim


class Turn(BaseModel):
    role: str
    content: str


class AskPayload(BaseModel):
    question: str
    filters: QaFilters | None = None
    history: list[Turn] | None = None


def _filters_from_query(
    location_id: str | None, employee_id: str | None, topic: str | None,
    date_from: str | None, date_to: str | None, simulated: str,
) -> dict | None:
    f = {
        "location_id": location_id, "employee_id": employee_id, "topic": topic,
        "date_from": date_from, "date_to": date_to, "simulated": simulated or "all",
    }
    f = {k: v for k, v in f.items() if v not in (None, "", "all")}
    return f or None


@router.get("/api/qa/filters")
async def filter_options() -> dict:
    return await asyncio.to_thread(qa_service.get_filter_options)


@router.post("/api/qa/ask")
async def ask(payload: AskPayload) -> dict:
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question must not be empty")
    # qa_service.answer_question is blocking (sync HTTP + SQLite + Milvus).
    login = active_login.read()
    filters = payload.filters.model_dump() if payload.filters else None
    history = [t.model_dump() for t in payload.history] if payload.history else None
    return await asyncio.to_thread(
        qa_service.answer_question,
        question,
        employee_id=login.employee_id if login else None,
        filters=filters,
        history=history,
    )


@router.post("/api/qa/ask-audio")
async def ask_audio(
    audio: UploadFile = File(...),
    location_id: str | None = None,
    employee_id: str | None = None,
    topic: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    simulated: str = "all",
) -> dict:
    """Glasses-side Q&A: accept a spoken WAV, transcribe, then run the agent.

    Returns the same shape as /api/qa/ask plus `question` (the transcribed text)
    and `transcribe_status`. Glasses render `short_answer` (≤140 chars); the full
    `answer` is logged server-side and surfaced via /api/qa/last-glasses.
    """
    audio_bytes = await audio.read()
    login = active_login.read()
    filters = _filters_from_query(location_id, employee_id, topic, date_from, date_to, simulated)
    return await asyncio.to_thread(
        qa_service.answer_question_from_audio,
        audio_bytes,
        employee_id=login.employee_id if login else None,
        filters=filters,
    )


@router.get("/api/qa/last-glasses")
async def last_glasses(limit: int = 20) -> dict:
    """Recent glasses Q&A turns (newest first) for the dashboard panel."""
    return {"items": await asyncio.to_thread(qa_service.recent_glasses_qa, limit)}


@router.websocket("/api/qa/audio-stream")
async def qa_audio_stream(websocket: WebSocket) -> None:
    """Glasses-side Q&A live ASR stream.

    Same wire shape as /api/session/{id}/audio-stream but decoupled from any
    customer session: relays 16kHz mono PCM to Doubao Seed ASR and streams back
    partial/final transcripts so the glasses can show the spoken question live.
    On ``eof`` it flushes Doubao for the final text. No DB writes — the gathered
    question is then POSTed to /api/qa/ask by the glasses.
    """
    await websocket.accept()
    asr = DoubaoSeedAsrStream()
    if not asr.configured:
        await websocket.send_text(json.dumps({"type": "asr_status", "status": "missing_doubao_env"}))
        await _qa_drain(websocket)
        return
    try:
        async with asr:
            await websocket.send_text(json.dumps({"type": "asr_status", "status": "connected"}))
            recv_task = asyncio.create_task(_qa_recv_loop(websocket, asr))
            try:
                while True:
                    message = await websocket.receive()
                    if message.get("bytes") is not None:
                        await asr.send_audio(message["bytes"])
                    elif message.get("text") == "eof":
                        await asr.send_audio(b"", final=True)
                        await asyncio.sleep(2.0)  # let Doubao flush final transcripts
                        break
            except WebSocketDisconnect:
                with contextlib.suppress(Exception):
                    await asr.send_audio(b"", final=True)
            finally:
                recv_task.cancel()
                with contextlib.suppress(Exception):
                    await recv_task
    except Exception as exc:
        await websocket.send_text(json.dumps({"type": "asr_status", "status": f"error:{type(exc).__name__}"}))


async def _qa_recv_loop(websocket: WebSocket, asr: DoubaoSeedAsrStream) -> None:
    final_parts: list[str] = []
    partial = ""
    while True:
        item = await asr.recv_text()
        if not item:
            continue
        text, final = item
        if final:
            final_parts.append(text)
            partial = ""
        else:
            partial = text
        final_text = " ".join(p for p in final_parts if p.strip()).strip()
        await websocket.send_text(json.dumps({
            "type": "transcript",
            "text": text,
            "final": final,
            "final_text": final_text,
            "partial_text": partial,
        }, ensure_ascii=False))


async def _qa_drain(websocket: WebSocket) -> None:
    try:
        while True:
            message = await websocket.receive()
            if message.get("text") == "eof":
                break
    except WebSocketDisconnect:
        return


@router.get("/api/qa/suggestions")
async def suggestions(
    creative: bool = False,
    location_id: str | None = None,
    employee_id: str | None = None,
    topic: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    simulated: str = "all",
) -> dict:
    filters = _filters_from_query(location_id, employee_id, topic, date_from, date_to, simulated)
    return await asyncio.to_thread(qa_service.get_suggestions, creative=creative, filters=filters)


@router.get("/api/qa/snapshot")
async def snapshot(
    narrative: bool = False,
    location_id: str | None = None,
    employee_id: str | None = None,
    topic: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    simulated: str = "all",
) -> dict:
    filters = _filters_from_query(location_id, employee_id, topic, date_from, date_to, simulated)
    return await asyncio.to_thread(qa_service.get_snapshot, narrative=narrative, filters=filters)


@router.get("/api/qa.html")
async def qa_html() -> HTMLResponse:
    login = active_login.to_dict()
    if login.get("active"):
        login_line = (
            f"active employee: <b>{login['employee_name']}</b>"
            f" @ <b>{login.get('location_name') or '—'}</b>"
            f" · <a href='/api/login.html'>change</a>"
        )
    else:
        login_line = "<span class='idle'>no employee logged in</span> · <a href='/api/login.html'>log in</a>"

    html = r"""<!doctype html><html><head><title>ShiftMind Q&amp;A</title>
<style>
body{margin:0;background:#000;color:#0f0;font:14px monospace;padding:16px;max-width:900px;margin:0 auto}
h3{margin:0 0 8px}
a{color:#0f0}
.idle{color:#666;font-style:italic}
textarea{width:100%;box-sizing:border-box;background:#012;color:#eee;border:1px solid #0f0;
  padding:10px;font:14px monospace;border-radius:3px;min-height:54px}
.row{display:flex;gap:8px;margin-top:8px;align-items:center}
button{background:#0f0;color:#000;border:0;padding:9px 18px;font:14px monospace;font-weight:bold;
  cursor:pointer;border-radius:3px}
button:disabled{opacity:.5;cursor:wait}
.ghost{background:transparent;color:#6f6;border:1px solid #065;font-weight:normal;padding:7px 12px}
.ghost:hover{background:#014}
.snapshot{border:1px solid #0f0;padding:12px;margin-top:14px;border-radius:3px}
.cards{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.card{border:1px solid #065;padding:8px 12px;border-radius:3px;min-width:90px}
.card .k{color:#6f6;font-size:10px;text-transform:uppercase;letter-spacing:1px}
.card .v{color:#efe;font-size:18px;font-weight:bold}
.topicrow{display:flex;align-items:center;gap:6px;margin:2px 0;font-size:12px;color:#cfc}
.topicrow .bar{height:8px;background:#0f0;border-radius:2px}
.narrative{color:#efe;margin:8px 0;line-height:1.5}
.chips{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px}
.chip{border:1px solid #065;padding:3px 8px;border-radius:10px;color:#6f6;cursor:pointer;font-size:12px}
.chip:hover{background:#014}
.filters{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;align-items:center}
.filters select,.filters input[type=date]{background:#012;color:#eee;border:1px solid #065;
  padding:5px 6px;border-radius:3px;font:12px monospace}
.fchips{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.fchip{background:#034;border:0;color:#9f9;padding:2px 6px;border-radius:8px;font-size:10px}
.fchip b{color:#efe}
.panel{border:1px solid #0f0;padding:12px;margin-top:14px;border-radius:3px}
.gq{border-top:1px dashed #044;padding:8px 0}
.gq:first-child{border-top:0}
.gq-q{color:#9f9;font-weight:bold;margin-bottom:4px;word-break:break-word}
.answer{color:#efe;line-height:1.55}
.answer p{margin:.3em 0}
.answer h4,.answer h5,.answer h6{margin:.5em 0 .2em;color:#9f9}
.answer ul,.answer ol{margin:.3em 0 .3em 1.4em;padding:0}
.answer li{margin:.1em 0}
.answer code{background:#012;padding:0 3px;border-radius:2px;color:#efe}
.answer hr{border:0;border-top:1px dashed #044;margin:.5em 0}
.muted{color:#666;font-size:11px;margin-top:8px}
details{margin-top:6px;border-top:1px dashed #044;padding-top:6px}
summary{cursor:pointer;color:#6f6;font-size:12px}
.step{margin:4px 0;color:#9d9;font-size:12px}
.step .args{color:#cfc}
.srcs{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.src{border:1px solid #065;padding:3px 8px;border-radius:3px;color:#cfc;font-size:12px}
.err{color:#f66}
</style></head>
<body>
<h3>shiftmind Q&amp;A agent</h3>
<p>@@LOGIN@@</p>
<p class="muted">Ask anything about past customer interactions. The agent queries the session DB and
semantic memory, then answers with sources.</p>
<textarea id="q" placeholder="e.g. Who had the longest conversations? / Best conversion rate this week? / What do clients usually ask about?"></textarea>
<div class="filters" id="filters">
  <select id="f-loc"><option value="">all locations</option></select>
  <select id="f-emp"><option value="">all employees</option></select>
  <select id="f-topic"><option value="">all topics</option></select>
  <select id="f-range">
    <option value="all">all time</option>
    <option value="7">last 7d</option>
    <option value="30">last 30d</option>
    <option value="custom">custom…</option>
  </select>
  <span id="f-custom" style="display:none">
    <input type="date" id="f-from"/> – <input type="date" id="f-to"/>
  </span>
  <select id="f-sim">
    <option value="all">all data</option>
    <option value="real">real only</option>
    <option value="sim">sim only</option>
  </select>
  <button id="clearf" type="button" class="ghost">clear</button>
</div>
<div class="fchips" id="fchips"></div>
<div class="row">
  <button id="ask">Ask</button>
  <button id="surprise" type="button" class="ghost">🎲 surprise me</button>
  <button id="overview" type="button" class="ghost">📊 overview</button>
  <button id="clearchat" type="button" class="ghost">↺ clear chat</button>
  <span id="status" class="muted"></span>
</div>
<div class="chips" id="chips"></div>
<div id="result"></div>
<div class="panel" id="glasses-panel">
  <h3>recent glasses questions</h3>
  <p class="muted">spoken on the glasses &rarr; full answer here. auto-refreshes every 4s.</p>
  <div id="glasses-log"><p class="muted">none yet</p></div>
</div>
<p class="muted"><a href="/api/preview.html">preview</a> · <a href="/api/faces.html">faces debug</a> · <a href="/api/qa.html">Q&A</a></p>

<script>
const chips = document.getElementById("chips");
function renderChips(list){
  chips.innerHTML = "";
  (list||[]).forEach(ex => {
    const b = document.createElement("span");
    b.className = "chip";
    b.textContent = ex;
    b.onclick = () => { document.getElementById("q").value = ex; document.getElementById("q").focus(); };
    chips.appendChild(b);
  });
}
function filtersToQuery(extra){
  const f = buildFilters();
  const p = new URLSearchParams();
  if(extra) for(const k in extra) p.set(k, extra[k]);
  for(const k of ["location_id","employee_id","topic","date_from","date_to","simulated"]){
    if(f[k] && f[k] !== "all") p.set(k, f[k]);
  }
  const s = p.toString();
  return s ? "?"+s : "";
}
async function loadChips(creative){
  try{
    const r = await fetch("/api/qa/suggestions"+filtersToQuery({creative: creative?1:0}));
    const d = await r.json();
    renderChips(d.suggestions);
  }catch(e){ /* keep last render */ }
}
loadChips(false);

// ── glasses Q&A log (spoken questions → full answers) ──
async function loadGlassesLog(){
  try{
    const r = await fetch("/api/qa/last-glasses?limit=15");
    const d = await r.json();
    const box = document.getElementById("glasses-log");
    const items = d.items || [];
    if(!items.length){ box.innerHTML = '<p class="muted">none yet</p>'; return; }
    box.innerHTML = items.map(it => {
      const ans = (it.answer || "").split("\n").map(p => '<p>'+esc(p)+'</p>').join("");
      const meta = esc(it.ts || '') + (it.employee_id ? ' · '+esc(it.employee_id) : '')
        + (it.elapsed_ms!=null ? ' · '+it.elapsed_ms+'ms' : '');
      return '<div class="gq">'
        + '<div class="gq-q">'+esc(it.question || '(empty)')+'</div>'
        + '<div class="answer">'+(ans || '<p class="muted">(no answer)</p>')+'</div>'
        + '<div class="muted">'+meta+'</div>'
        + '</div>';
    }).join("");
  }catch(e){ /* keep last render */ }
}
loadGlassesLog();
setInterval(loadGlassesLog, 4000);

// ── filters ──
const fLoc = document.getElementById("f-loc");
const fEmp = document.getElementById("f-emp");
const fTopic = document.getElementById("f-topic");
const fRange = document.getElementById("f-range");
const fCustom = document.getElementById("f-custom");
const fSim = document.getElementById("f-sim");
fRange.addEventListener("change", () => { fCustom.style.display = fRange.value === "custom" ? "" : "none"; });
document.getElementById("clearf").onclick = () => {
  [fLoc, fEmp, fTopic].forEach(s => s.value = "");
  fRange.value = "all"; fCustom.style.display = "none";
  document.getElementById("f-from").value = ""; document.getElementById("f-to").value = "";
  fSim.value = "all"; renderScope();
};

async function loadFilters(){
  try{
    const d = await (await fetch("/api/qa/filters")).json();
    const opt = (v,l) => '<option value="'+esc(v)+'">'+esc(l)+'</option>';
    (d.locations||[]).forEach(l => fLoc.insertAdjacentHTML("beforeend", opt(l.id, l.name)));
    (d.employees||[]).forEach(e => fEmp.insertAdjacentHTML("beforeend", opt(e.id, e.name)));
    (d.topics||[]).forEach(t => fTopic.insertAdjacentHTML("beforeend", opt(t.topic, t.topic+" ("+t.n+")")));
    if(d.date_range){
      document.getElementById("f-from").min = d.date_range.min;
      document.getElementById("f-from").max = d.date_range.max;
      document.getElementById("f-to").min = d.date_range.min;
      document.getElementById("f-to").max = d.date_range.max;
    }
  }catch(e){ /* filters optional */ }
}
loadFilters();

function buildFilters(){
  const f = {simulated: fSim.value || "all"};
  if(fLoc.value) f.location_id = fLoc.value;
  if(fEmp.value) f.employee_id = fEmp.value;
  if(fTopic.value) f.topic = fTopic.value;
  const today = new Date();
  const iso = d => d.toISOString().slice(0,10);
  const daysAgo = n => { const d = new Date(today); d.setDate(d.getDate()-n); return iso(d); };
  if(fRange.value === "7"){ f.date_from = daysAgo(7); }
  else if(fRange.value === "30"){ f.date_from = daysAgo(30); }
  else if(fRange.value === "custom"){
    if(document.getElementById("f-from").value) f.date_from = document.getElementById("f-from").value;
    if(document.getElementById("f-to").value) f.date_to = document.getElementById("f-to").value;
  }
  return f;
}
function activeFilterCount(f){
  let n = 0;
  if(f.location_id) n++; if(f.employee_id) n++; if(f.topic) n++;
  if(f.date_from) n++; if(f.date_to) n++; if(f.simulated && f.simulated !== "all") n++;
  return n;
}
function renderScope(){
  const f = buildFilters();
  const box = document.getElementById("fchips");
  box.innerHTML = activeFilterCount(f)
    ? "<span class='fchip'>scope: <b>"+Object.entries(f).filter(([k,v])=>v && !(k==="simulated"&&v==="all")).map(([k,v])=>esc(k+"="+v)).join("</b></span>  <span class='fchip'><b>").trim()+"</b></span>"
    : "";
}
[fLoc, fEmp, fTopic, fRange, fSim].forEach(s => s.addEventListener("change", renderScope));
renderScope();

function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
// Render a constrained markdown subset to HTML. XSS-safe: esc() runs first so
// no input-controlled markup survives; only our own literals become tags.
function inlineMd(s){
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
}
function renderMd(md){
  const t = esc(md);
  const lines = t.split(/\r?\n/);
  const out = [];
  let listType = null, para = [];
  const flushPara = () => { if(para.length){ out.push("<p>"+inlineMd(para.join("<br>"))+"</p>"); para=[]; } };
  const closeList = () => { if(listType){ out.push("</"+listType+">"); listType=null; } };
  for(const raw of lines){
    const line = raw.replace(/\s+$/,"");
    const trim = line.trim();
    if(!trim){ flushPara(); closeList(); continue; }
    if(/^---+$/.test(trim)){ flushPara(); closeList(); out.push("<hr>"); continue; }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if(h){ flushPara(); closeList(); const lvl = h[1].length<=1?4:(h[1].length===2?5:6); out.push("<h"+lvl+">"+inlineMd(h[2])+"</h"+lvl+">"); continue; }
    if(/^[-*]\s+/.test(line)){ flushPara(); if(listType!=='ul'){ closeList(); out.push("<ul>"); listType='ul'; } out.push("<li>"+inlineMd(line.replace(/^[-*]\s+/,""))+"</li>"); continue; }
    const ol = line.match(/^\d+\.\s+(.*)$/);
    if(ol){ flushPara(); if(listType!=='ol'){ closeList(); out.push("<ol>"); listType='ol'; } out.push("<li>"+inlineMd(ol[1])+"</li>"); continue; }
    para.push(line);
  }
  flushPara(); closeList();
  return out.join("");
}
function fmtArgs(t){
  if(t.tool==="run_sql") return "<code>"+esc(t.args)+"</code>";
  return esc(t.args);
}

let chatHistory = [];
async function run(){
  const q = document.getElementById("q").value.trim();
  if(!q) return;
  const btn = document.getElementById("ask");
  const status = document.getElementById("status");
  const out = document.getElementById("result");
  btn.disabled = true;
  status.textContent = "thinking…";
  out.innerHTML = "";
  try{
    const r = await fetch("/api/qa/ask", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({question: q, filters: buildFilters(), history: chatHistory})
    });
    const d = await r.json();
    if(!d.configured){
      out.innerHTML = "<div class='panel err'>"+esc(d.answer)+"</div>";
      return;
    }
    let html = "<div class='panel'><div class='answer'>"+renderMd(d.answer)+"</div>";
    if(d.short_answer){
      html += "<div class='muted'>HUD: "+esc(d.short_answer)+"</div>";
    }
    html += "<div class='muted'>"+d.iterations+" tool-round"+(d.iterations===1?"":"s")+" · "+d.elapsed_ms+" ms</div>";
    if(d.sources && d.sources.length){
      html += "<div class='srcs'>";
      d.sources.forEach(s => {
        html += "<span class='src'>"+esc(s.session_id)+" · "+esc(s.customer_name||s.customer_id)+(s.outcome?" · "+esc(s.outcome):"")+"</span>";
      });
      html += "</div>";
    }
    if(d.trace && d.trace.length){
      html += "<details><summary>tool trace ("+d.trace.length+")</summary>";
      d.trace.forEach(t => {
        html += "<div class='step'>🔧 "+esc(t.tool)+" · "+fmtArgs(t)+" → "+esc(t.summary)+(t.rows!=null?" · "+t.rows+" rows":"")+" · "+t.ms+" ms</div>";
      });
      html += "</details>";
    }
    html += "</div>";
    out.innerHTML = html;
    chatHistory.push({role:"user", content:q}, {role:"assistant", content:d.answer});
    if(chatHistory.length > 12) chatHistory = chatHistory.slice(-12);
  }catch(e){
    out.innerHTML = "<div class='panel err'>request failed: "+esc(e.message||e)+"</div>";
  }finally{
    btn.disabled = false;
    status.textContent = "";
  }
}
document.getElementById("ask").onclick = run;
document.getElementById("surprise").onclick = () => loadChips(true);
document.getElementById("overview").onclick = overview;
document.getElementById("clearchat").onclick = () => {
  chatHistory = [];
  document.getElementById("result").innerHTML = "";
};
document.getElementById("q").addEventListener("keydown", e => { if((e.metaKey||e.ctrlKey)&&e.key==="Enter") run(); });

async function overview(){
  const out = document.getElementById("result");
  out.innerHTML = "<div class='panel muted'>loading overview…</div>";
  try{
    const r = await fetch("/api/qa/snapshot"+filtersToQuery({narrative:1}));
    const d = await r.json();
    out.innerHTML = renderSnapshot(d);
  }catch(e){ out.innerHTML = "<div class='panel err'>overview failed: "+esc(e.message||e)+"</div>"; }
}
function renderSnapshot(d){
  const conv = Math.round((d.conversion_rate||0)*100);
  const avgm = Math.round((d.avg_duration_seconds||0)/60);
  const sb = d.simulated_breakdown||{};
  const cards = [
    ["sessions", d.ended_sessions],
    ["conversion", conv+"%"],
    ["avg len", avgm+"m"],
    ["real / sim", sb.real+" / "+sb.sim],
  ].map(([k,v]) => "<div class='card'><div class='k'>"+k+"</div><div class='v'>"+esc(v)+"</div></div>").join("");
  const maxn = Math.max(1, ...((d.top_topics||[]).map(t=>t.n)));
  const topics = (d.top_topics||[]).slice(0,8).map(t =>
    "<div class='topicrow'>"+esc(t.topic)+" <span class='bar' style='width:"+Math.round(t.n/maxn*140)+"px'></span> "+t.n+"</div>"
  ).join("");
  const emps = (d.top_employees||[]).slice(0,5).map(e =>
    "<li>"+esc(e.name)+" — "+e.n+" sessions, "+Math.round((e.conv||0)*100)+"% conv</li>").join("");
  const locs = (d.top_locations||[]).map(l =>
    "<li>"+esc(l.name)+" — "+l.n+" sessions, "+Math.round((l.conv||0)*100)+"% conv</li>").join("");
  const narr = d.narrative ? "<div class='narrative'>"+esc(d.narrative)+"</div>" : "";
  return "<div class='snapshot'>"+
    narr +
    "<div class='cards'>"+cards+"</div>"+
    (topics?"<div class='k'>top topics</div>"+topics:"")+
    (emps?"<div class='k' style='margin-top:8px'>top employees</div><ul style='margin:4px 0 0;padding-left:18px;color:#cfc;font-size:12px'>"+emps+"</ul>":"")+
    (locs?"<div class='k' style='margin-top:8px'>locations</div><ul style='margin:4px 0 0;padding-left:18px;color:#cfc;font-size:12px'>"+locs+"</ul>":"")+
    "</div>";
}
</script>
</body></html>"""
    return HTMLResponse(html.replace("@@LOGIN@@", login_line))
