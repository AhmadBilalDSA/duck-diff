"""Air-gapped Web Studio dashboard (HTML/CSS/JS assets).

All assets are embedded as Python string constants so the dashboard works
fully offline — no CDN, no external fonts, no network requests.  The
HTTP server in :mod:`duck_diff.server` serves these directly from memory.

The dashboard provides:
* Interactive side-by-side data diff with search, filter and pagination.
* Executive summary cards (row counts, drift badge, schema drift status).
* Column statistical drift table with live filtering.
* Schema drift detail panel.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import __version__

__all__ = ["STUDIO_HTML", "studio_page", "PRICING_HTML", "pricing_page"]

# ---------------------------------------------------------------------------
# Web Studio main dashboard
# ---------------------------------------------------------------------------

_STUDIO_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>duck-diff Web Studio</title>
<style>
:root{--bg:#0f172a;--surface:#1e293b;--card:#fff;--ink:#e2e8f0;--mut:#94a3b8;
--ok:#22c55e;--bad:#ef4444;--warn:#f59e0b;--acc:#38bdf8;--line:#334155}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font:14px/1.6 "Inter","Segoe UI",system-ui,sans-serif}
.wrap{max-width:1200px;margin:0 auto;padding:20px 16px 60px}
header{display:flex;align-items:center;justify-content:space-between;padding:16px 0;border-bottom:1px solid var(--line);margin-bottom:24px}
header h1{font-size:20px;font-weight:700}
header .badge{background:#38bdf822;color:var(--acc);padding:3px 10px;border-radius:999px;font-size:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px}
.card .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut)}
.card .v{font-size:26px;font-weight:700;margin-top:4px}
.card.ok .v{color:var(--ok)}.card.bad .v{color:var(--bad)}.card.warn .v{color:var(--warn)}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:24px 0 10px}
table{border-collapse:collapse;width:100%;background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:8px 12px;border-bottom:1px solid var(--line);text-align:left}
th{background:#0f172a;color:#fff;font-weight:600;font-size:12px}
tr:last-child td{border-bottom:none}
.old{color:#f87171;text-decoration:line-through}.new{color:#4ade80;font-weight:600}
.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:10px 0}
input[type=search],select{padding:7px 12px;border:1px solid var(--line);border-radius:8px;background:var(--surface);color:var(--ink);font:inherit}
label{display:flex;align-items:center;gap:6px;font-size:13px}
button{padding:6px 14px;border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:8px;cursor:pointer;font:inherit}
button:disabled{opacity:.4;cursor:default}
.pager{display:flex;gap:8px;align-items:center;margin-top:12px;font-size:13px;color:var(--mut)}
.empty{text-align:center;padding:40px;color:var(--mut)}
footer{margin-top:40px;text-align:center;font-size:12px;color:var(--mut)}
a{color:var(--acc)}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>duck-diff Web Studio</h1>
  <span class="badge">v__VERSION__</span>
</header>
<div id="upload">
  <p style="color:var(--mut);margin-bottom:12px">Load a diff JSON report or paste URL parameters to visualise results.</p>
  <div class="controls">
    <input type="file" id="fileInput" accept=".json" style="color:var(--mut)">
    <input type="search" id="jsonUrl" placeholder="Or paste JSON report URL…" style="min-width:280px">
    <button id="loadUrl">Load</button>
  </div>
</div>
<div id="dashboard" style="display:none">
  <div class="cards" id="cards"></div>
  <h2>Column statistical drift</h2>
  <div class="controls"><input type="search" id="statSearch" placeholder="Filter columns…"></div>
  <div id="statsWrap"></div>
  <h2>Data diff preview</h2>
  <div class="controls">
    <input type="search" id="search" placeholder="Search key or values…" style="min-width:240px">
    <label><input type="checkbox" id="onlyDrift" checked> mismatches only</label>
    <select id="statusFilter">
      <option value="">all statuses</option><option value="modified">modified</option>
      <option value="added">added</option><option value="deleted">deleted</option>
    </select>
    <select id="pageSize"><option>25</option><option selected>50</option><option>100</option>
    <option value="100000000">all</option></select>
  </div>
  <table id="diffTable"><thead><tr><th style="width:90px">Status</th><th style="width:22%">Key</th>
  <th>Changes (old &rarr; new)</th></tr></thead><tbody id="diffBody"></tbody></table>
  <div class="pager">
    <button id="prevPage">&lsaquo; Prev</button><span id="pageInfo"></span><button id="nextPage">Next &rsaquo;</button>
  </div>
</div>
<div id="empty" class="empty">
  <p>No data loaded.  Use the file picker above or run <code>duck-diff diff --format json -o report.json</code> then load the file.</p>
</div>
<footer>duck-diff Web Studio &mdash; fully offline, zero external assets</footer>
</div>
<script>
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
let REPORT=null,view=[],page=1;
function show(d){
 REPORT=d;document.getElementById("upload").style.display="none";
 document.getElementById("dashboard").style.display="block";
 document.getElementById("empty").style.display="none";
 const s=REPORT.summary;
 document.getElementById("cards").innerHTML=[
  ["Total rows",`${s.total_rows_a.toLocaleString()} / ${s.total_rows_b.toLocaleString()}`,""],
  ["Unchanged",s.identical_rows_count.toLocaleString(),"ok"],
  ["Modified",s.modified_rows_count.toLocaleString(),"warn"],
  ["Added",s.added_rows_count.toLocaleString(),"ok"],
  ["Deleted",s.deleted_rows_count.toLocaleString(),"bad"],
  ["Schema drift",REPORT.schema_drift?"yes":"no",REPORT.schema_drift?"bad":"ok"],
  ["Duration",REPORT.duration_seconds.toFixed(2)+"s",""]
 ].map(c=>`<div class="card ${c[2]}"><div class="k">${c[0]}</div><div class="v">${c[1]}</div></div>`).join("");
 view=REPORT.preview||[];page=1;render();
}
function filtered(){
 const q=document.getElementById("search").value.toLowerCase();
 const only=document.getElementById("onlyDrift").checked;
 const st=document.getElementById("statusFilter").value;
 return view.filter(r=>{if(st&&r.status!==st)return false;if(!only&&r.status==="identical")return true;if(!q)return true;return(r.key+" "+r.cells.map(c=>c.join(" ")).join(" ")).toLowerCase().includes(q)});
}
function render(){
 const size=parseInt(document.getElementById("pageSize").value,10);
 const rows=filtered();const pages=Math.max(1,Math.ceil(rows.length/size));
 page=Math.min(page,pages);
 const slice=rows.slice((page-1)*size,page*size);
 document.getElementById("diffBody").innerHTML=slice.length?slice.map(r=>{
  const cls=r.status==="added"?"background:#052e16":(r.status==="deleted"?"background:#450a0a":"");
  const cells=r.cells.map(c=>{
   if(c[1]===null&&c[2]===null)return"";if(r.status==="added")return`<td style="background:#052e16"><span class="new">${esc(c[2])}</span></td>`;
   if(r.status==="deleted")return`<td style="background:#450a0a"><span class="old">${esc(c[1])}</span></td>`;
   return`<td style="background:#422006"><span class="old">${esc(c[1])}</span> &rarr; <span class="new">${esc(c[2])}</span></td>`;
  }).join("");
  return`<tr style="${cls}"><td>${r.status}</td><td><code>${esc(r.key)}</code></td>${cells}</tr>`;
 }).join(""):`<tr><td colspan="3" class="empty">No matching rows.</td></tr>`;
 document.getElementById("pageInfo").textContent=`page ${page} / ${pages} \u00b7 ${rows.length} rows`;
 document.getElementById("prevPage").disabled=page<=1;
 document.getElementById("nextPage").disabled=page>=pages;
}
["search","onlyDrift","statusFilter","pageSize"].forEach(id=>
 document.getElementById(id).addEventListener("input",()=>{page=1;render()}));
document.getElementById("prevPage").onclick=()=>{page--;render()};
document.getElementById("nextPage").onclick=()=>{page++;render()};
document.getElementById("statSearch").addEventListener("input",e=>{
 const q=e.target.value.toLowerCase();
 document.querySelectorAll("#statsTable tbody tr").forEach(tr=>{
  tr.style.display=tr.textContent.toLowerCase().includes(q)?"":"none";});
});
document.getElementById("fileInput").addEventListener("change",e=>{
 const f=e.target.files[0];if(!f)return;
 const r=new FileReader();r.onload=()=>{try{show(JSON.parse(r.result))}catch(ex){alert("Invalid JSON")}};
 r.readAsText(f);
});
document.getElementById("loadUrl").onclick=()=>{
 const url=document.getElementById("jsonUrl").value.trim();if(!url)return;
 fetch(url).then(r=>r.json()).then(show).catch(()=>alert("Failed to load URL"));
};
</script>
</body>
</html>
"""


def studio_page(report_json: Optional[str] = None) -> str:
    """Return the Web Studio HTML page.

    Args:
        report_json: Optional pre-loaded JSON string to inject into the page.
            When ``None`` the page starts empty and waits for file upload.
    """
    html = _STUDIO_TEMPLATE.replace("__VERSION__", __version__)
    if report_json:
        html = html.replace(
            'document.getElementById("empty").style.display="block"',
            f"show({report_json})",
        )
    return html


STUDIO_HTML = _STUDIO_TEMPLATE


# ---------------------------------------------------------------------------
# Pricing page
# ---------------------------------------------------------------------------

_PRICING_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>duck-diff Pricing</title>
<style>
:root{--bg:#0f172a;--surface:#1e293b;--card:#fff;--ink:#e2e8f0;--mut:#94a3b8;
--ok:#22c55e;--acc:#38bdf8;--line:#334155;--accent:#f59e0b}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font:15px/1.6 "Inter","Segoe UI",system-ui,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:40px 16px 80px}
h1{font-size:28px;font-weight:700;text-align:center;margin-bottom:6px}
.sub{text-align:center;color:var(--mut);margin-bottom:36px;font-size:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:20px}
.plan{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:28px 24px;display:flex;flex-direction:column}
.plan.featured{border-color:var(--accent);box-shadow:0 0 24px #f59e0b33}
.plan .name{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut)}
.plan .price{font-size:32px;font-weight:700;margin:8px 0 4px}
.plan .price span{font-size:14px;font-weight:400;color:var(--mut)}
.plan .desc{color:var(--mut);font-size:13px;margin-bottom:16px}
.plan ul{list-style:none;margin:0 0 20px;padding:0}
.plan li{padding:5px 0;font-size:14px}
.plan li::before{content:"\\2713 ";color:var(--ok);font-weight:700}
.plan .cta{display:block;text-align:center;padding:10px 0;border:none;border-radius:10px;
background:var(--accent);color:#000;font-weight:600;font-size:14px;cursor:pointer;text-decoration:none}
.plan .cta.secondary{background:var(--surface);color:var(--ink);border:1px solid var(--line)}
footer{text-align:center;color:var(--mut);font-size:12px;margin-top:40px}
</style>
</head>
<body>
<div class="wrap">
<h1>duck-diff Pricing</h1>
<p class="sub">Fast, constant-memory data diffing for modern data teams</p>
<div class="grid">
 <div class="plan">
  <div class="name">Community</div>
  <div class="price">Free <span>forever</span></div>
  <div class="desc">Core diffing engine, open-source CLI</div>
  <ul>
   <li>Schema diff &amp; row diff</li>
   <li>Parquet, CSV, TSV, JSON, SQLite</li>
   <li>Console terminal summary</li>
   <li>GitHub Action CI gate</li>
   <li>MCP server for AI agents</li>
  </ul>
  <a class="cta secondary" href="https://github.com/AhmadBilalDSA/duck-diff">Get Started</a>
 </div>
 <div class="plan featured">
  <div class="name">Pro Annual</div>
  <div class="price">$29 <span>/year</span></div>
  <div class="desc">Interactive dashboard &amp; visual diffs</div>
  <ul>
   <li>Everything in Community</li>
   <li>Interactive Web Studio dashboard</li>
   <li>Visual SQL AST diff</li>
   <li>CSV &amp; HTML report export</li>
   <li>Column statistical drift</li>
  </ul>
  <a class="cta" href="https://polar.sh/AhmadBilalDSA/duck-diff">Subscribe</a>
 </div>
 <div class="plan">
  <div class="name">Founder Lifetime</div>
  <div class="price">$49 <span>one-time</span></div>
  <div class="desc">All Pro features, forever</div>
  <ul>
   <li>Everything in Pro</li>
   <li>Priority updates &amp; roadmap access</li>
   <li>Founder badge in Web Studio</li>
   <li>Lifetime license key</li>
   <li>Direct support channel</li>
  </ul>
  <a class="cta" href="https://polar.sh/AhmadBilalDSA/duck-diff">Buy Lifetime</a>
 </div>
</div>
<footer>duck-diff v__VERSION__ &mdash; by Ahmad Bilal (AhmadBilalDSA)</footer>
</div>
</body>
</html>
"""


def pricing_page() -> str:
    """Return the pricing HTML page with the current version injected."""
    return _PRICING_TEMPLATE.replace("__VERSION__", __version__)


PRICING_HTML = _PRICING_TEMPLATE
