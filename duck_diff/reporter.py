"""Output formatters for :mod:`duck_diff`.

Three presentation layers share one input (:class:`duck_diff.engine.DiffResult`):

* **Rich terminal UI** — summary cards and colored tables when ``rich`` is
  installed (green = match, red = mismatch).
* **ASCII fallback** — dependency-free aligned tables otherwise.
* **Markdown / JSON exporters** — GitHub-flavored PR comments with drift
  badges & collapsible sections, and a machine-readable document for CI
  assertions.
"""

from __future__ import annotations

import html as _htmlmod
import io as _io
import json
import sys
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote as _urlquote

from . import __version__
from .engine import DiffResult

__all__ = [
    "FORMATS",
    "render",
    "render_ascii",
    "render_terminal",
    "to_html",
    "to_json",
    "to_markdown",
]

FORMATS = ("table", "markdown", "json", "html")

_FORMAT_ALIASES = {
    "table": "table",
    "terminal": "table",
    "ascii": "table",
    "rich": "table",
    "markdown": "markdown",
    "md": "markdown",
    "json": "json",
    "html": "html",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _shield(label: str, value: Any, color: str) -> str:
    label_slug = _urlquote(str(label).replace(" ", "_"))
    value_slug = _urlquote(str(value))
    return f"![{label}](https://img.shields.io/badge/{label_slug}-{value_slug}-{color})"


def _try_import_rich() -> Optional[Any]:
    try:
        import rich  # noqa: F401

        return rich
    except Exception:  # noqa: BLE001 - any import failure means "no rich"
        return None


def _stdout_is_tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# JSON exporter
# ---------------------------------------------------------------------------


def to_json(result: DiffResult, indent: int = 2) -> str:
    """Machine-readable document for automated CI assertions."""
    return json.dumps(result.to_dict(), indent=indent, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Markdown exporter (GitHub-flavored, PR-comment ready)
# ---------------------------------------------------------------------------


def _md_cell(value: Any) -> str:
    if value is None:
        return "`NULL`"
    text = str(value)
    text = (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", "")
        .replace("\n", "␤")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f"`{text}`" if text else "`''`"


def _fmt_delta(value: Any) -> str:
    """Human formatting for stat deltas: em-dash for None, ints bare, floats compact."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:g}"


def _stat_row(stat: Dict[str, Any], column: str) -> List[str]:
    if stat.get("kind") == "numeric":
        return [
            column,
            "numeric",
            _fmt_delta(stat.get("null_count_diff")),
            _fmt_delta(stat.get("mean_diff")),
            _fmt_delta(stat.get("min_diff")),
            _fmt_delta(stat.get("max_diff")),
            (
                _fmt_delta(stat.get("mean_pct_shift")) + "%"
                if stat.get("mean_pct_shift") is not None
                else "—"
            ),
            "—",
        ]
    return [
        column,
        "categorical",
        _fmt_delta(stat.get("null_count_diff")),
        "—",
        "—",
        "—",
        "—",
        _fmt_delta(stat.get("distinct_count_diff")),
    ]


def to_markdown(result: DiffResult) -> str:
    """GitHub-flavored markdown with drift badges, tables and collapsible sections."""
    summary = result.summary
    schema = result.schema_diff
    drift = result.drift_detected

    badges = " ".join(
        [
            _shield("drift detected", "yes" if drift else "no", "red" if drift else "green"),
            _shield(
                "modified rows",
                str(summary.modified_rows_count),
                "yellow" if summary.modified_rows_count else "green",
            ),
            _shield("added rows", str(summary.added_rows_count), "blue"),
            _shield("deleted rows", str(summary.deleted_rows_count), "orange"),
            _shield(
                "schema drift",
                "yes" if schema.has_schema_drift else "no",
                "red" if schema.has_schema_drift else "green",
            ),
        ]
    )

    key_note = f" · key: `{', '.join(result.keys)}`" if result.keys else ""
    lines: List[str] = [
        "## 🦆 duck-diff report",
        "",
        f"**Source:** `{result.source}` → **Target:** `{result.target}` "
        f"· mode: `{result.mode}`{key_note}",
        "",
        badges,
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Rows (source / target) | {summary.total_rows_a:,} / {summary.total_rows_b:,} |",
        f"| Identical rows ✅ | {summary.identical_rows_count:,} |",
        f"| Modified rows 🔴 | {summary.modified_rows_count:,} |",
        f"| Added rows ➕ | {summary.added_rows_count:,} |",
        f"| Deleted rows ➖ | {summary.deleted_rows_count:,} |",
    ]

    lines += ["", "### Cell drift by column", ""]
    if summary.column_drift_stats:
        lines += ["| Column | Mismatches | Drift % |", "| --- | ---: | ---: |"]
        for column, stat in summary.column_drift_stats.items():
            marker = "🔴" if int(stat.get("mismatches", 0)) else "🟢"
            pct = float(stat.get("drift_pct", 0.0))
            lines.append(
                f"| {marker} {column} | {int(stat.get('mismatches', 0)):,} | {pct:.2f}% |"
            )
    elif result.mode == "keyless":
        lines.append("_Keyless mode: cell-level attribution requires `--key`._")

    if summary.column_stats:
        lines += [
            "",
            "### Column statistical drift",
            "",
            "| Column | Kind | Null Δ | Mean Δ | Min Δ | Max Δ | Shift % | Distinct Δ |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for column, stat in summary.column_stats.items():
            lines.append("| " + " | ".join(_stat_row(stat, column)) + " |")

    shown = len(summary.sample_mismatches)
    lines += ["", "<details>", f"<summary>🔍 <strong>Sample mismatches ({shown} shown)</strong></summary>", ""]
    if summary.sample_mismatches:
        lines += ["| Key | Column | Source value | Target value |", "| --- | --- | --- | --- |"]
        for record in summary.sample_mismatches:
            padded: Sequence[Any] = list(record) + [None] * (4 - len(record))
            lines.append(
                f"| {_md_cell(padded[0])} | {_md_cell(padded[1])} | "
                f"{_md_cell(padded[2])} | {_md_cell(padded[3])} |"
            )
    else:
        lines.append("_No cell-level mismatches recorded._")
    lines += ["", "</details>"]

    added_recs = [
        r for r in summary.sample_mismatches if len(r) > 1 and r[1] == "__added_row__"
    ]
    deleted_recs = [
        r for r in summary.sample_mismatches if len(r) > 1 and r[1] == "__deleted_row__"
    ]
    for title, icon, records, value_idx in (
        ("Added row previews", "➕", added_recs, 3),
        ("Deleted row previews", "➖", deleted_recs, 2),
    ):
        if not records:
            continue
        lines += [
            "",
            "<details>",
            f"<summary>{icon} <strong>{title} ({len(records)})</strong></summary>",
            "",
            "| Key | Row values (`col||col||…`) |",
            "| --- | --- |",
        ]
        for record in records:
            lines.append(f"| {_md_cell(record[0])} | {_md_cell(record[value_idx])} |")
        lines += ["", "</details>"]

    opener = "<details open>" if schema.has_schema_drift else "<details>"
    lines += ["", opener, "<summary>🧬 <strong>Schema drift</strong></summary>", ""]
    schema_bullets: List[str] = []
    if schema.only_in_a:
        schema_bullets.append(
            "- Columns only in **source**: " + ", ".join(f"`{c}`" for c in schema.only_in_a)
        )
    if schema.only_in_b:
        schema_bullets.append(
            "- Columns only in **target**: " + ", ".join(f"`{c}`" for c in schema.only_in_b)
        )
    if schema.type_mismatches:
        schema_bullets.append("- Type mismatches:")
        schema_bullets += [f"  - `{item}`" for item in schema.type_mismatches]
    if not schema_bullets:
        schema_bullets.append("_Schemas are identical._")
    lines += schema_bullets
    lines += ["", "</details>"]

    warnings_note = ""
    if result.warnings:
        warnings_note = f" · ⚠️ {len(result.warnings)} warning(s)"
    lines += [
        "",
        f"<sub>Generated by <a href='https://github.com/duck-diff/duck-diff'>duck-diff</a> "
        f"v{__version__} · {result.duration_seconds:.2f}s{warnings_note}</sub>",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ASCII fallback terminal renderer
# ---------------------------------------------------------------------------


def _ascii_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    rendered = [[("NULL" if v is None else str(v)) for v in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in rendered:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def sep(char: str) -> str:
        return "+" + "+".join(char * (w + 2) for w in widths) + "+"

    def fmt(row: Sequence[str]) -> str:
        return "|" + "|".join(
            f" {row[i]:<{widths[i]}} " for i in range(len(widths))
        ) + "|"

    out = [sep("="), fmt(list(headers)), sep("-")]
    out.extend(fmt(row) for row in rendered)
    out.append(sep("-"))
    return "\n".join(out)


def render_ascii(result: DiffResult) -> str:
    """Dependency-free terminal report."""
    s = result.summary
    keys_note = f" (key: {', '.join(result.keys)})" if result.keys else ""
    lines: List[str] = [
        "=" * 62,
        " duck-diff report",
        "=" * 62,
        f" source       : {result.source}",
        f" target       : {result.target}",
        f" mode         : {result.mode}{keys_note}",
        "-" * 62,
        f" rows A / B   : {s.total_rows_a:,} / {s.total_rows_b:,}",
        f" identical    : {s.identical_rows_count:,}",
        f" modified     : {s.modified_rows_count:,}",
        f" added        : {s.added_rows_count:,}",
        f" deleted      : {s.deleted_rows_count:,}",
        f" schema drift : {'YES' if result.schema_diff.has_schema_drift else 'no'}",
        "-" * 62,
    ]

    if s.column_drift_stats:
        lines += [
            "",
            " Cell drift by column:",
            _ascii_table(
                ["column", "mismatches", "drift %"],
                [
                    [c, int(st.get("mismatches", 0)), f"{float(st.get('drift_pct', 0.0)):.2f}"]
                    for c, st in s.column_drift_stats.items()
                ],
            ),
        ]
    if s.column_stats:
        stat_rows = []
        for c, st in s.column_stats.items():
            if st.get("kind") == "numeric":
                stat_rows.append([
                    c, "num",
                    _fmt_delta(st.get("null_count_diff")),
                    _fmt_delta(st.get("mean_diff")),
                    _fmt_delta(st.get("min_diff")),
                    _fmt_delta(st.get("max_diff")),
                    (
                        _fmt_delta(st.get("mean_pct_shift")) + "%"
                        if st.get("mean_pct_shift") is not None else "-"
                    ),
                    "-",
                ])
            else:
                stat_rows.append([
                    c, "cat",
                    _fmt_delta(st.get("null_count_diff")),
                    "-", "-", "-", "-",
                    _fmt_delta(st.get("distinct_count_diff")),
                ])
        lines += [
            "",
            " Column statistics (source vs target):",
            _ascii_table(
                ["column", "kind", "null d", "mean d", "min d", "max d", "shift", "distinct d"],
                stat_rows,
            ),
        ]
    if s.sample_mismatches:
        lines += ["", f" Sample mismatches (top {len(s.sample_mismatches)}):"]
        lines.append(
            _ascii_table(
                ["key", "column", "source", "target"],
                [list(rec) for rec in s.sample_mismatches],
            )
        )
    schema = result.schema_diff
    if schema.has_schema_drift:
        lines += ["", " Schema drift:"]
        if schema.only_in_a:
            lines.append(f"   only in source : {', '.join(schema.only_in_a)}")
        if schema.only_in_b:
            lines.append(f"   only in target : {', '.join(schema.only_in_b)}")
        for item in schema.type_mismatches:
            lines.append(f"   type           : {item}")
    for warning in result.warnings:
        lines.append(f" ! warning     : {warning}")
    lines += ["", f" completed in {result.duration_seconds:.2f}s", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rich terminal renderer
# ---------------------------------------------------------------------------


def render_terminal(result: DiffResult, color: Optional[bool] = None) -> str:
    """Render with Rich when available; transparent ASCII fallback otherwise."""
    if color is None:
        color = _stdout_is_tty()
    if _try_import_rich() is None:
        return render_ascii(result)

    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    buf = _io.StringIO()
    console = Console(file=buf, force_terminal=color, no_color=not color, width=100)
    s = result.summary

    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="right", style="bold")
    grid.add_column()

    def styled(value: int, bad_color: str = "red") -> Text:
        return Text(f"{value:,}", style=(bad_color if value else "green"))

    grid.add_row("rows A/B", Text(f"{s.total_rows_a:,} / {s.total_rows_b:,}"))
    grid.add_row("identical", Text(f"{s.identical_rows_count:,}", style="green"))
    grid.add_row("modified", styled(s.modified_rows_count))
    grid.add_row("added", styled(s.added_rows_count, "cyan"))
    grid.add_row("deleted", styled(s.deleted_rows_count, "magenta"))
    grid.add_row(
        "schema drift",
        Text("YES", style="bold red") if result.schema_diff.has_schema_drift else Text("no", style="green"),
    )
    keys_note = f" · key={', '.join(result.keys)}" if result.keys else ""
    console.print(
        Panel(
            grid,
            title=f"🦆 duck-diff [{result.mode}{keys_note}]",
            subtitle=f"{result.source} → {result.target} · {result.duration_seconds:.2f}s",
            expand=False,
        )
    )

    if s.column_drift_stats:
        table = Table(title="Column drift")
        table.add_column("Column")
        table.add_column("Mismatches", justify="right")
        table.add_column("Drift %", justify="right")
        for column, stat in s.column_drift_stats.items():
            mismatches = int(stat.get("mismatches", 0))
            style = "bold red" if mismatches else "green"
            table.add_row(
                Text(column, style=style),
                Text(str(mismatches), style=style),
                Text(f"{float(stat.get('drift_pct', 0.0)):.2f}", style=style),
            )
        console.print(table)

    if s.sample_mismatches:
        sample_table = Table(title=f"Sample mismatches (top {len(s.sample_mismatches)})")
        sample_table.add_column("Key")
        sample_table.add_column("Column")
        sample_table.add_column("Source", style="cyan")
        sample_table.add_column("Target", style="magenta")
        for record in s.sample_mismatches:
            padded: Sequence[Any] = list(record) + [None] * (4 - len(record))
            sample_table.add_row(*[Text("NULL", style="dim") if v is None else str(v) for v in padded])
        console.print(sample_table)

    schema = result.schema_diff
    if schema.has_schema_drift:
        parts: List[str] = []
        if schema.only_in_a:
            parts.append("[red]only in source[/red]: " + ", ".join(schema.only_in_a))
        if schema.only_in_b:
            parts.append("[green]only in target[/green]: " + ", ".join(schema.only_in_b))
        parts += [f"[yellow]{item}[/yellow]" for item in schema.type_mismatches]
        console.print(Panel("\n".join(parts), title="🧬 Schema drift", expand=False))

    for warning in result.warnings:
        console.print(Text(f"! warning: {warning}", style="yellow"))

    return buf.getvalue()


# ---------------------------------------------------------------------------
# Standalone interactive HTML report (fully offline, zero external assets)
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>duck-diff report</title>
<style>
:root{--bg:#f6f8fa;--card:#fff;--ink:#1e293b;--mut:#64748b;--ok:#16a34a;--bad:#dc2626;
--add:#dcfce7;--del:#fee2e2;--mod:#fef9c3;--line:#e2e8f0;--acc:#f59e0b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 "Segoe UI",system-ui,Arial,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px 18px 60px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mut);margin:0 0 18px;font-size:13px}
.chip{display:inline-block;background:#fff;border:1px solid var(--line);border-radius:999px;
padding:2px 10px;margin:2px 4px 2px 0;font-size:12px;color:#334155}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:14px 0 26px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.card .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut)}
.card .v{font-size:24px;font-weight:700;margin-top:4px}.card.ok .v{color:var(--ok)}
.card.bad .v{color:var(--bad)}.card.warn .v{color:#b45309}
h2{font-size:15px;margin:30px 0 8px;text-transform:uppercase;letter-spacing:.05em;color:#334155}
table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line);
border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:7px 11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:#0f172a;color:#fff;font-weight:600;font-size:12px}
tr:last-child td{border-bottom:none}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr.add td{background:var(--add)}tr.del td{background:var(--del)}td.modcell{background:var(--mod)}
.old{color:#9f1239;text-decoration:line-through;margin-right:6px}
.new{color:#166534;font-weight:600}.arr{color:var(--mut);margin:0 4px}
.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:10px 0}
input[type=search],select{padding:7px 10px;border:1px solid var(--line);border-radius:8px;
background:#fff;font:inherit}
label.toggle{display:flex;align-items:center;gap:6px;font-size:13px;color:#334155}
.pager{display:flex;gap:8px;align-items:center;margin-top:12px;font-size:13px;color:var(--mut)}
button{padding:6px 12px;border:1px solid var(--line);background:#fff;border-radius:8px;cursor:pointer;font:inherit}
button:disabled{opacity:.45;cursor:default}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:8px 0}
summary{cursor:pointer;font-weight:600}
ul.clean{margin:6px 0;padding-left:18px}.muted{color:var(--mut)}
code{background:#f1f5f9;border-radius:5px;padding:1px 5px;font-size:12px}
</style>
</head>
<body>
<div class="wrap">
<h1>🦆 duck-diff report</h1>
<p class="sub">__SUBTITLE__</p>
<div>__CHIPS__</div>

<h2>Executive summary</h2>
<div class="cards" id="cards"></div>

<h2>Schema</h2>
__SCHEMA__

<h2>Column statistical drift</h2>
<div class="controls"><input type="search" id="statSearch" placeholder="Filter columns…"></div>
__STATS__

<h2>Data diff preview <span class="muted" style="text-transform:none;font-size:12px">(sampled)</span></h2>
<div class="controls">
  <input type="search" id="search" placeholder="Search key or values…" style="min-width:240px">
  <label class="toggle"><input type="checkbox" id="onlyDrift" checked> mismatches only</label>
  <select id="statusFilter">
    <option value="">all statuses</option><option value="modified">modified</option>
    <option value="added">added</option><option value="deleted">deleted</option>
  </select>
  <select id="pageSize"><option>25</option><option selected>50</option><option>100</option>
  <option value="100000000">all</option></select>
</div>
<table id="diffTable"><thead><tr><th style="width:90px">Status</th><th style="width:22%">Key</th>
<th>Changes (old → new)</th></tr></thead><tbody id="diffBody"></tbody></table>
<div class="pager">
  <button id="prevPage">‹ Prev</button><span id="pageInfo"></span><button id="nextPage">Next ›</button>
</div>
<p class="muted" style="margin-top:26px;font-size:12px">Generated by duck-diff v__VERSION__ ·
standalone offline file — no external assets.</p>
</div>
<script>
const REPORT = __REPORT_JSON__;
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;")
 .replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
const s=REPORT.summary;
document.getElementById("cards").innerHTML=[
 ["Total rows",`${s.total_rows_a.toLocaleString()} / ${s.total_rows_b.toLocaleString()}`,""],
 ["Unchanged ✅",s.identical_rows_count.toLocaleString(),"ok"],
 ["Modified 🔴",s.modified_rows_count.toLocaleString(),"warn"],
 ["Added ➕",s.added_rows_count.toLocaleString(),"ok"],
 ["Deleted ➖",s.deleted_rows_count.toLocaleString(),"bad"],
 ["Schema changes",REPORT.schema_drift?"yes":"no",REPORT.schema_drift?"bad":"ok"],
 ["Duration",REPORT.duration_seconds.toFixed(2)+"s",""]
].map(c=>`<div class="card ${c[2]}"><div class="k">${c[0]}</div><div class="v">${c[1]}</div></div>`).join("");

let view=REPORT.preview.slice(), page=1;
function filtered(){
 const q=document.getElementById("search").value.toLowerCase();
 const only=document.getElementById("onlyDrift").checked;
 const st=document.getElementById("statusFilter").value;
 return view.filter(r=>{
   if(st && r.status!==st) return false;
   if(!only && r.status==="identical") return true;
   if(!q) return true;
   return (r.key+" "+r.cells.map(c=>c.join(" ")).join(" ")).toLowerCase().includes(q);
 });
}
function render(){
 const size=parseInt(document.getElementById("pageSize").value,10);
 const rows=filtered(); const pages=Math.max(1,Math.ceil(rows.length/size));
 page=Math.min(page,pages);
 const slice=rows.slice((page-1)*size,page*size);
 document.getElementById("diffBody").innerHTML = slice.length? slice.map(r=>{
   const cls=r.status==="added"?"add":(r.status==="deleted"?"del":"");
   const cells=r.cells.map(c=>{
     if(c[1]===null&&c[2]===null) return "";
     if(r.status==="added") return `<div><span class="new">${esc(c[2])}</span></div>`;
     if(r.status==="deleted") return `<div><span class="old">${esc(c[1])}</span></div>`;
     return `<div class="modcell"><span class="old">${esc(c[1])}</span>`+
            `<span class="arr">→</span><span class="new">${esc(c[2])}</span></div>`;
   }).join("");
   return `<tr class="${cls}"><td>${r.status}</td><td><code>${esc(r.key)}</code></td><td>${cells}</td></tr>`;
 }).join("") : `<tr><td colspan="3" class="muted">No matching rows.</td></tr>`;
 document.getElementById("pageInfo").textContent=`page ${page} / ${pages} · ${rows.length} rows`;
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
render();
</script>
</body>
</html>
"""


def _build_preview_rows(result: DiffResult) -> List[Dict[str, Any]]:
    """Group sample records into client-renderable preview rows.

    Cell-drift samples sharing a key collapse into one ``modified`` row with
    one entry per drifted column; sentinel records become ``added`` /
    ``deleted`` rows. Purely bounded by the engine's sample limit.
    """
    grouped: Dict[str, Dict[str, Any]] = {}
    ordered_keys: List[str] = []
    added_rows: List[Dict[str, Any]] = []
    deleted_rows: List[Dict[str, Any]] = []

    for record in result.summary.sample_mismatches:
        padded: List[Any] = list(record) + [None] * (4 - len(record))
        key, column, val_a, val_b = padded[:4]
        column = str(column)
        if column == "__added_row__":
            added_rows.append({"status": "added", "key": str(key),
                               "cells": [["row", "", "" if val_b is None else val_b]]})
        elif column == "__deleted_row__":
            deleted_rows.append({"status": "deleted", "key": str(key),
                                 "cells": [["row", "" if val_a is None else val_a, ""]]})
        else:
            bucket = grouped.get(str(key))
            if bucket is None:
                bucket = {"status": "modified", "key": str(key), "cells": []}
                grouped[str(key)] = bucket
                ordered_keys.append(str(key))
            bucket["cells"].append([column, "" if val_a is None else val_a,
                                    "" if val_b is None else val_b])

    return [grouped[k] for k in ordered_keys] + added_rows + deleted_rows


def to_html(result: DiffResult) -> str:
    """Build a fully standalone interactive HTML report (offline, no CDNs).

    Includes an executive summary card grid, the column statistical drift
    table with instant filtering, and a searchable/paginated side-by-side
    diff preview: green = added rows, red = deleted rows, yellow =
    modified cells rendered as ``old → new``.
    """
    summary = result.summary
    doc = result.to_dict()

    chips = [
        f"<span class='chip'><strong>{_htmlmod.escape(result.source)}</strong> → "
        f"<strong>{_htmlmod.escape(result.target)}</strong></span>",
        f"<span class='chip'>mode: {result.mode}</span>",
    ]
    if result.keys:
        chips.append(f"<span class='chip'>key: {_htmlmod.escape(', '.join(result.keys))}</span>")
    for warning in result.warnings:
        chips.append(f"<span class='chip'>⚠ { _htmlmod.escape(warning)}</span>")

    schema_bits: List[str] = []
    if result.schema_diff.has_schema_drift:
        if result.schema_diff.only_in_a:
            schema_bits.append(
                "<li>Only in source: "
                + ", ".join(_htmlmod.escape(c) for c in result.schema_diff.only_in_a)
                + "</li>"
            )
        if result.schema_diff.only_in_b:
            schema_bits.append(
                "<li>Only in target: "
                + ", ".join(_htmlmod.escape(c) for c in result.schema_diff.only_in_b)
                + "</li>"
            )
        for item in result.schema_diff.type_mismatches:
            schema_bits.append(f"<li>Type mismatch: {_htmlmod.escape(item)}</li>")
        schema_html = (
            "<details open><summary>Schema drift detected</summary><ul class='clean'>"
            + "".join(schema_bits)
            + "</ul></details>"
        )
    else:
        schema_html = "<p class='muted'>Schemas are identical. ✔</p>"

    stats_html = "<p class='muted'>No comparable columns.</p>"
    if summary.column_stats:
        head = "".join(
            f"<th{' class=num' if i > 1 else ''}>{h}</th>"
            for i, h in enumerate(
                ["Column", "Kind", "Null Δ", "Mean Δ", "Min Δ", "Max Δ", "Shift %", "Distinct Δ"]
            )
        )
        body_rows = []
        for column, stat in summary.column_stats.items():
            cells = _stat_row(stat, column)
            tds = "".join(
                f"<td{' class=num' if i > 1 else ''}>{_htmlmod.escape(v)}</td>"
                for i, v in enumerate(cells)
            )
            body_rows.append(f"<tr>{tds}</tr>")
        stats_html = (
            "<table id='statsTable'><thead><tr>" + head + "</tr></thead><tbody>"
            + "".join(body_rows) + "</tbody></table>"
        )

    subtitle = (
        f"{_htmlmod.escape(result.source)} vs {_htmlmod.escape(result.target)}"
        f" · {result.duration_seconds:.2f}s"
    )

    report_payload = {
        "summary": doc["summary"],
        "schema": doc["schema"],
        "schema_drift": doc["schema_drift"],
        "drift_detected": doc["drift_detected"],
        "mode": result.mode,
        "keys": result.keys,
        "duration_seconds": result.duration_seconds,
        "preview": _build_preview_rows(result),
    }
    report_json = json.dumps(report_payload, ensure_ascii=False, default=str).replace(
        "</", "<\\/"
    )  # keep embedded JSON from terminating the <script> block

    html_out = _HTML_TEMPLATE
    html_out = html_out.replace("__REPORT_JSON__", report_json)
    html_out = html_out.replace("__VERSION__", __version__)
    html_out = html_out.replace("__SUBTITLE__", subtitle)
    html_out = html_out.replace("__CHIPS__", "".join(chips))
    html_out = html_out.replace("__SCHEMA__", schema_html)
    html_out = html_out.replace("__STATS__", stats_html)
    return html_out


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def render(result: DiffResult, fmt: str = "table", color: Optional[bool] = None) -> str:
    """Render *result* in the requested format.

    Args:
        result: The diff outcome.
        fmt: One of ``table``, ``markdown``, ``json``, ``html`` (aliases accepted).
        color: Force ANSI colors on/off for the terminal format.

    Raises:
        ValueError: If *fmt* is not a recognised format.
    """
    normalized = _FORMAT_ALIASES.get(str(fmt).lower())
    if normalized is None:
        raise ValueError(
            f"Unknown format {fmt!r}; expected one of {', '.join(FORMATS)}."
        )
    if normalized == "json":
        return to_json(result)
    if normalized == "markdown":
        return to_markdown(result)
    if normalized == "html":
        return to_html(result)
    return render_terminal(result, color=color)
