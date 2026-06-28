"""Q&A agent router — `/api/qa.html` page + `POST /api/qa/ask`.

Lets an employee ask free-form business-insight questions over the collected
conversation history. The agent (qa_service) calls run_sql / vector_search
tools and returns an answer + tool-call trace + source sessions.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from auth import active_login
import qa_service

router = APIRouter()


class AskPayload(BaseModel):
    question: str


@router.post("/api/qa/ask")
async def ask(payload: AskPayload) -> dict:
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question must not be empty")
    # qa_service.answer_question is blocking (sync HTTP + SQLite + Milvus).
    login = active_login.read()
    return await asyncio.to_thread(
        qa_service.answer_question,
        question,
        employee_id=login.employee_id if login else None,
    )


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

    html = """<!doctype html><html><head><title>ShiftMind Q&amp;A</title>
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
.chips{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px}
.chip{border:1px solid #065;padding:3px 8px;border-radius:10px;color:#6f6;cursor:pointer;font-size:12px}
.chip:hover{background:#014}
.panel{border:1px solid #0f0;padding:12px;margin-top:14px;border-radius:3px}
.answer{white-space:pre-wrap;color:#efe;line-height:1.5}
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
<div class="row">
  <button id="ask">Ask</button>
  <span id="status" class="muted"></span>
</div>
<div class="chips" id="chips"></div>
<div id="result"></div>
<p class="muted"><a href="/api/preview.html">preview</a> · <a href="/api/faces.html">faces debug</a></p>

<script>
const EXAMPLES = [
  "Who had the longest conversations?",
  "Best conversion rate by employee",
  "Most popular topic clients talk about",
  "Find sessions about returns",
];
const chips = document.getElementById("chips");
EXAMPLES.forEach(ex => {
  const b = document.createElement("span");
  b.className = "chip";
  b.textContent = ex;
  b.onclick = () => { document.getElementById("q").value = ex; };
  chips.appendChild(b);
});

function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function fmtArgs(t){
  if(t.tool==="run_sql") return "<code>"+esc(t.args)+"</code>";
  return esc(t.args);
}

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
      body: JSON.stringify({question: q})
    });
    const d = await r.json();
    if(!d.configured){
      out.innerHTML = "<div class='panel err'>"+esc(d.answer)+"</div>";
      return;
    }
    let html = "<div class='panel'><div class='answer'>"+esc(d.answer)+"</div>";
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
  }catch(e){
    out.innerHTML = "<div class='panel err'>request failed: "+esc(e.message||e)+"</div>";
  }finally{
    btn.disabled = false;
    status.textContent = "";
  }
}
document.getElementById("ask").onclick = run;
document.getElementById("q").addEventListener("keydown", e => { if((e.metaKey||e.ctrlKey)&&e.key==="Enter") run(); });
</script>
</body></html>"""
    return HTMLResponse(html.replace("@@LOGIN@@", login_line))
