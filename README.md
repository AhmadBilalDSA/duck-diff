# 🦆 duck-diff

**Fast, constant-memory data reconciliation across Parquet, CSV, TSV, JSON, Arrow and SQLite — powered by an embedded DuckDB SQL engine. CLI · Python library · GitHub Action · MCP server for AI agents.**

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Engine](https://img.shields.io/badge/engine-duckdb%20%E2%89%A50.10-FFF000?logo=duckdb&logoColor=black)](https://duckdb.org/)
[![Tests](https://img.shields.io/badge/tests-61%20passing-brightgreen?logo=pytest&logoColor=white)](#testing)
[![MCP](https://img.shields.io/badge/MCP-server-8A2BE2)](#-mcp-server-ai-agents)

---

`duck-diff` answers one question precisely: **what changed between two datasets?**

- **Schema drift** — columns added/removed, type changes (metadata-only `DESCRIBE SELECT … LIMIT 0`, O(1)).
- **Row drift** — additions and deletions via composite primary keys.
- **Cell drift** — per-column mismatch counts + drift %, with sample records `[key, column, old, new]`.
- **Statistical drift** — single-pass in-engine distribution metrics per column (means, min/max, null & distinct deltas).

Every heavy operation executes *inside* DuckDB (null-safe `FULL OUTER JOIN`, hash aggregation, set algebra). Datasets never materialize in Python RAM — memory stays **constant** from 1 KB to 100 GB.

## 📦 Installation

```bash
pip install duck-diff            # core (duckdb only)
pip install "duck-diff[ui]"      # + rich terminal UI & typer CLI
pip install "duck-diff[dev]"     # + pytest
```

Requires Python ≥ 3.9. Everything beyond `duckdb>=0.10.0` is optional; the CLI degrades gracefully (argparse parser, ASCII rendering).

## 🚀 CLI

```bash
# Keyed diff with the interactive terminal report
duck-diff baseline.parquet candidate.parquet --key id,region

# Float tolerance + case-insensitive text comparison
duck-diff old.csv new.csv --key id --epsilon 1e-6 --ignore-case

# Keyless whole-row hashing when no primary key exists
duck-diff events_a.jsonl events_b.jsonl

# SQLite sources via URI
duck-diff "sqlite://prod.db#orders" "sqlite://staging.db#orders" --key order_id

# Standalone interactive HTML report (air-gapped: zero external assets)
duck-diff a.parquet b.parquet --key id --format html --output-file report.html

# CI gate: exit code 1 when anything drifted
duck-diff a.parquet b.parquet --key id --fail-on-drift
```

### Options

| Flag | Description |
| --- | --- |
| `-k, --key COLS` | Comma-separated key columns; omit for keyless hashing |
| `--epsilon F` | Absolute float tolerance for value columns (default `0`) |
| `--ignore-case` | Compare text columns case-insensitively |
| `--format {table,markdown,json,html}` | Output format |
| `-o, --output, --output-file PATH` | Write report to file instead of stdout (UTF-8) |
| `--limit N` | Max sample records (default `20`) |
| `--fail-on-drift` | Exit `1` on any row-level or schema drift |
| `--memory-limit SIZE` | DuckDB buffer cap, e.g. `"2GB"` |
| `--version` | Print version |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success — no drift (or drift tolerated) |
| `1` | Drift detected while `--fail-on-drift` was set |
| `2` | Usage or runtime error (human-readable message, never a traceback) |

## 🖼️ Interactive HTML reports

`--format html` emits a **single self-contained `.html` file** — embedded CSS + vanilla JS, no CDNs, works air-gapped. See [`samples/report.html`](samples/report.html) for a generated example.

- **Executive summary cards** — total rows, unchanged, modified, added, deleted, schema changes, duration.
- **Column statistical drift table** — numeric mean/min/max shifts, null deltas, distinct-count deltas; instant client-side column filter.
- **Side-by-side diff preview** — 🟩 added rows, 🟥 deleted rows, 🟨 modified cells rendered `old → new`.
- **Controls** — instant search across keys/values, status filter (modified/added/deleted), mismatches-only toggle, page-size selector with pagination.

## 🤖 MCP server (AI agents)

`duck_diff` ships a dependency-free **Model Context Protocol** stdio server so Claude Desktop, Cursor, Windsurf or any MCP client can drive reconciliations natively:

```bash
duck-diff-mcp          # console script … or:
python -m duck_diff.mcp_server
```

| Tool | Arguments | Returns |
| --- | --- | --- |
| `diff_datasets` | `source`, `target`, `key?`, `tolerance=0.0`, `sample_limit=20`, `format="json"` | Full structured diff incl. sample mismatches |
| `inspect_schema_drift` | `source`, `target` | Column types, missing columns, type mismatches |
| `get_column_stats` | `source`, `target`, `key?`, `tolerance?` | Per-column statistical drift metrics |

Register it in Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "duck-diff": {
      "command": "duck-diff-mcp"
    }
  }
}
```

For Cursor / Windsurf use the same `command`/`args` shape in their MCP settings. The server speaks newline-delimited JSON-RPC 2.0 over stdio, never leaks tracebacks (errors surface as structured `isError` tool results), and releases every DuckDB connection deterministically.

## 🐍 Python API

```python
from duck_diff import diff, DuckDiffer

result = diff("baseline.parquet", "candidate.parquet", keys=["id"], epsilon=1e-9)

print(result.summary.modified_rows_count)
print(result.summary.column_drift_stats["amount"])   # {"mismatches": 3, "drift_pct": 0.12}
print(result.summary.column_stats["amount"])         # single-pass distribution metrics:
# {"kind": "numeric", "null_count_diff": 1, "mean_diff": 10.0,
#  "min_diff": 0.0, "max_diff": 20.0, "mean_pct_shift": 50.0, ...}

with DuckDiffer(memory_limit="2GB") as differ:
    report = differ.diff("a.csv", "b.csv")           # keyless mode

open("report.html", "w", encoding="utf-8").write(
    __import__("duck_diff.reporter", fromlist=["to_html"]).to_html(report)
)
```

JSON document (`--format json` / `to_json`) nests everything machine-consumably: `schema`, `summary` (incl. `column_stats`), top-level `sample_mismatches`, `warnings`, timing.

## 🏗️ Architecture & rule enforcement matrix

```
source ─▶ io.load_source ─▶ TEMP VIEW "__dd_a" ┐
target ─▶ io.load_source ─▶ TEMP VIEW "__dd_b" ┴▶ DuckDB engine
                    │   null-safe FULL OUTER JOIN (IS NOT DISTINCT FROM)
                    │   per-column equality (+epsilon on values only)
                    │   MD5(CONCAT_WS('||', …)) multiset hashing
                    │   single-pass AVG/MIN/MAX/DISTINCT statistics
                    ▼      aggregates + ≤ limit samples only
        DiffSummary ─▶ reporters: rich/ASCII · markdown · json · html
                     └▶ action.py (GitHub outputs) · mcp_server.py (stdio JSON-RPC)
```

| Architectural rule | How it is enforced |
| --- | --- |
| Constant memory — no dataset ever crosses into Python | All comparisons/hashing/stats are SQL aggregates; Python receives scalars and ≤ `--limit` sample rows |
| Epsilon applies to values, never keys | Join predicate is strict `IS NOT DISTINCT FROM`; epsilon branch exists only in `_equality_expr` used on non-key cells |
| NULL correctness | `IS NOT DISTINCT FROM` for joins/cells; `COALESCE` sentinels inside hashes; `NULL=NULL` matches, `NULL≠''` drifts (tested) |
| Windows file-lock resilience | `DuckDiffer.close()` in `finally` everywhere; SQLite fallback cursor closed; sandbox-tolerant `tmp_path` fixture never deletes at teardown |
| Console safety on legacy codepages | `emit_stdout` reconfigures streams w/ replacement + backslash fallback — cp1252 cannot crash the CLI |
| Zero missing dependencies | Single hard dep `duckdb>=0.10.0`; extras `[ui]`, `[dev]`; MCP server is stdlib-only |
| No leaked tracebacks | CLI maps every failure to actionable stderr message + exit code; MCP wraps tool errors into `isError` payloads |

## 🤝 GitHub Action

```yaml
- uses: duck-diff/duck-diff@v1
  id: diff
  with:
    source_path: data/baseline.parquet
    target_path: data/candidate.parquet
    key_columns: "id,region"
    epsilon: "1e-9"
    fail_on_drift: "true"
# outputs: steps.diff.outputs.drift_detected / modified_rows / schema_drift
```

Inputs: `source_path`*, `target_path`*, `key_columns`, `epsilon`, `fail_on_drift` (* required). The step prints the Markdown badge report to the job log for PR comments.

## 🧪 Testing

```bash
pip install -e ".[dev]"
python -m pytest tests/test_diff.py -v -p no:cacheprovider
python -m compileall .
```

61 offline tests cover: schema drift, epsilon boundaries, keyless multisets, NULL semantics, statistical drift against known shifts/null injections, HTML structure & script-injection neutralisation, MCP handshake/tools/stdin round-trips, CLI exit codes and the Action's output contract. `conftest.py` provides a sandbox-tolerant `tmp_path` (no-delete teardown) for locked-down environments.

## 📁 Project layout

```
duck_diff/
├── action.yml              # GitHub Action metadata (composite)
├── pyproject.toml          # packaging; entry points duck-diff, duck-diff-mcp
├── conftest.py             # sandbox-tolerant pytest fixtures
├── samples/report.html     # generated example of the HTML reporter
├── duck_diff/
│   ├── io.py               # universal loader (parquet/csv/tsv/json/sqlite)
│   ├── schema_diff.py      # DESCRIBE-based schema drift
│   ├── engine.py           # DuckDiffer: keyed/keyless SQL algebra + stats
│   ├── reporter.py         # rich/ASCII · markdown · json · interactive html
│   ├── cli.py              # typer-first CLI (argparse fallback), exit gating
│   ├── action.py           # GitHub Action runtime
│   └── mcp_server.py       # stdio MCP server (diff_datasets, inspect_schema_drift, get_column_stats)
└── tests/test_diff.py      # complete offline suite
```

## 📄 License

MIT — see [LICENSE](LICENSE).
