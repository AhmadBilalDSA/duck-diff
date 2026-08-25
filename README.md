# 🦆 duck-diff

**Fast, constant-memory data diffing across Parquet, CSV, TSV, JSON, Arrow and SQLite — powered by an embedded DuckDB SQL engine.**

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Engine](https://img.shields.io/badge/engine-duckdb%20%E2%89%A50.10-FFF000?logo=duckdb&logoColor=black)](https://duckdb.org/)
[![Tests](https://img.shields.io/badge/tests-42%20passing-brightgreen?logo=pytest&logoColor=white)](#testing)
[![CLI](https://img.shields.io/badge/UI-rich%20%7C%20ASCII-orange)](#output-formats)

---

`duck-diff` tells you exactly what changed between two datasets: **schema drift**
(columns added/removed, types changed), **row additions & deletions**, and
**cell-level modifications** with per-column drift statistics — while executing
every heavy operation *inside* DuckDB via `FULL OUTER JOIN` / set algebra over
SQL views. Datasets never leave the engine, so memory stays **constant** whether
you diff 1 KB or 100 GB.

It ships as a Python **library**, a polished **CLI** (`duck-diff`), and a
turnkey **GitHub Action** for data-platform CI/CD with strict exit-code gating
and PR-ready drift badges.

## ✨ Highlights

- 🗂️ **Universal sources** — Parquet, CSV, TSV, JSON/JSONL/NDJSON and
  `sqlite://<db>#<table>` URIs, auto-detected from the path.
- 🔑 **Keyed diffing** (`--key id,region`) with NULL-safe joins
  (`IS NOT DISTINCT FROM`) so `NULL` keys match and never vanish into anti-joins.
- #️⃣ **Keyless diffing** — rows are auto-hashed with
  `MD5(CONCAT_WS('||', col₁, col₂, …))`; multiset arithmetic yields exact
  identical/added/deleted counts even with duplicate rows.
- 🌡️ **Float tolerance** (`--epsilon`) using `TRY_CAST(… AS DOUBLE)` so numeric
  noise (e.g. `100.0` vs `100.0005`) doesn't fire false alarms — applied to
  values only, never to keys.
- 🧬 **Schema drift detection** via metadata-only `DESCRIBE SELECT … LIMIT 0`.
- 🖥️ **Rich terminal UI** when `rich` is installed, dependency-free ASCII tables
  otherwise.
- 📝 **Markdown CI exporter** — GitHub-flavored PR comment with drift badges,
  summary table, collapsible sample-mismatch & schema sections.
- 🔢 **JSON exporter** — one machine-readable document for automated assertions.
- ⚙️ **GitHub Action** — inputs `source_path`, `target_path`, `key_columns`,
  `epsilon`, `fail_on_drift`; outputs `drift_detected`, `modified_rows`,
  `schema_drift`.
- 🧪 **42 offline tests** — the whole suite runs without network access.

## 📦 Installation

```bash
pip install duck-diff            # core (duckdb only)
pip install "duck-diff[ui]"      # + rich terminal UI & typer CLI sugar
pip install "duck-diff[dev]"     # + pytest for development
```

Or from a checkout of this repository:

```bash
pip install .
```

Requires Python ≥ 3.9 and `duckdb>=0.10.0`. Everything else is optional; the
CLI falls back to `argparse` + ASCII rendering automatically.

## 🚀 CLI Quickstart

```bash
# Keyed diff of two Parquet files with a Rich terminal report
duck-diff baseline.parquet candidate.parquet --key id,region

# Float tolerance + case-insensitive text comparison
duck-diff old.csv new.csv --key id --epsilon 1e-6 --ignore-case

# Keyless whole-row hashing when no primary key exists
duck-diff events_a.jsonl events_b.jsonl

# SQLite sources via URI
duck-diff "sqlite://prod.db#orders" "sqlite://staging.db#orders" --key order_id

# PR-comment markdown written straight to a file
duck-diff a.parquet b.parquet --key id --format markdown --output drift.md

# CI gate: exit code 1 when anything drifted
duck-diff a.parquet b.parquet --key id --fail-on-drift
```

### Flags

| Flag | Description |
| --- | --- |
| `-k, --key COLS` | Comma-separated key columns; omit for keyless hashing |
| `--epsilon F` | Absolute float tolerance for numeric comparisons (default `0`) |
| `--ignore-case` | Compare text columns case-insensitively |
| `--format {table,markdown,json}` | Output format (default `table`) |
| `-o, --output PATH` | Write report to file instead of stdout (UTF-8) |
| `--limit N` | Max sample mismatch records (default `20`) |
| `--fail-on-drift` | Exit `1` when any row-level or schema drift is detected |
| `--memory-limit SIZE` | DuckDB memory budget, e.g. `"2GB"` (constant-memory mode) |
| `--version` | Print version |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success — no drift, or drift tolerated without `--fail-on-drift` |
| `1` | Drift detected while `--fail-on-drift` was set |
| `2` | Usage or runtime error (missing file, unreadable source, bad key) |

## 🐍 Python API

```python
from duck_diff import diff, DuckDiffer

result = diff("baseline.parquet", "candidate.parquet", keys=["id"], epsilon=1e-9)

print(result.mode)                        # "keyed"
print(result.schema_diff.only_in_b)       # columns added in target
print(result.summary.modified_rows_count) # rows with changed cells
print(result.summary.column_drift_stats)  # {"amount": {"mismatches": 3, "drift_pct": 0.12}}
print(result.summary.sample_mismatches)   # [["42", "amount", "10.0", "11.5"], ...]
assert not result.drift_detected          # or gate your own pipeline

# Reusable engine with an explicit memory budget:
with DuckDiffer(memory_limit="2GB", ignore_case=True) as differ:
    report = differ.diff("a.csv", "b.csv")        # keyless
    print(report.to_dict()["summary"]["identical_rows_count"])
```

`DiffResult.to_dict()` powers the JSON exporter:

```bash
duck-diff a.parquet b.parquet --key id --format json | jq '.summary'
```

```json
{
  "tool": "duck-diff",
  "mode": "keyed",
  "keys": ["id"],
  "schema": { "only_in_b": ["height"], "type_mismatches": [], "has_schema_drift": true },
  "summary": {
    "total_rows_a": 1000, "total_rows_b": 1003,
    "identical_rows_count": 995, "modified_rows_count": 5,
    "added_rows_count": 3, "deleted_rows_count": 0,
    "column_drift_stats": { "amount": { "mismatches": 5, "drift_pct": 0.5 } },
    "drift_detected": true
  },
  "drift_detected": true,
  "sample_mismatches": [["42", "amount", "10.0", "11.5"]]
}
```

## 🤖 GitHub Action

Drop this into any workflow to gate data changes on pull requests:

```yaml
name: data-contract
on:
  pull_request:
    paths: ["data/**"]

jobs:
  diff:
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
    steps:
      - uses: actions/checkout@v4

      - name: duck-diff gate
        id: diff
        uses: duck-diff/duck-diff@v1
        with:
          source_path: data/baseline.parquet
          target_path: data/candidate.parquet
          key_columns: "id,region"
          epsilon: "1e-9"
          fail_on_drift: "true"

      - name: Comment on PR
        if: always()
        uses: marocchino/sticky-pull-request-comment@v2
        with:
          header: duck-diff
          path: drift_comment.md   # or re-run the CLI with --format markdown
```

### Action inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `source_path` | ✅ | — | Baseline file path or `sqlite://<db>#<table>` URI |
| `target_path` | ✅ | — | Candidate dataset (same accepted forms) |
| `key_columns` | — | `""` | Comma-separated keys; empty ⇒ keyless hashing |
| `epsilon` | — | `"0.0"` | Float tolerance |
| `fail_on_drift` | — | `"true"` | Fail the step when drift is detected |

### Action outputs

| Output | Type | Description |
| --- | --- | --- |
| `drift_detected` | `'true' \| 'false'` | Any row-level **or** schema drift found |
| `modified_rows` | integer string | Matched rows with ≥1 changed cell |
| `schema_drift` | `'true' \| 'false'` | Columns added/removed or types drifted |

Downstream steps can branch on them:

```yaml
- name: Notify
  if: steps.diff.outputs.drift_detected == 'true'
  run: echo "Drift! modified=${{ steps.diff.outputs.modified_rows }}"
```

## 🔬 How it works

```
 source ─▶ io.load_source ─▶ TEMP VIEW "__dd_a" ┐
 target ─▶ io.load_source ─▶ TEMP VIEW "__dd_b" ┴─▶ DuckDB SQL engine
                                                     │ FULL OUTER JOIN (IS NOT DISTINCT FROM)
                                                     │ per-column IS NOT DISTINCT FROM / epsilon
                                                     │ MD5(CONCAT_WS('||', …)) multiset counts
                                                     ▼
                                        aggregates + top-N samples only
                                                     ▼
                              DiffSummary ◀─▶ reporters (rich / ASCII / md / json)
```

- **Constant memory** — both sides stay as SQL views over their files; only
  aggregate rows and `--limit` samples cross into Python. Set
  `--memory-limit 2GB` to cap DuckDB's buffer manager for laptop-scale runs.
- **NULL semantics** — `NULL vs NULL` matches, `NULL vs ''` drifts; NULL-safe
  equality is used for keys, values and hashes alike.
- **SQLite** — attached natively through DuckDB's `sqlite` extension when
  available; otherwise `duck_diff.io` transparently ingests the table in
  50 000-row chunks via stdlib `sqlite3`, keeping air-gapped CI fully offline.
- **Robust CSV** — `read_csv_auto(header=true)` first; if the strict sniffer
  rejects odd files (mixed line endings, ragged rows) the loader retries once
  with `strict_mode=false`.

## 📊 Performance

All work executes as vectorised DuckDB SQL with parallel hash joins — the same
primitives that make DuckDB analytic queries fast. Representative timings from
a development laptop (DuckDB 1.5.5, NVMe SSD; rerun on your hardware):

| Scenario | Rows/side | Keyed full diff | Keyless hash diff |
| --- | ---: | ---: | ---: |
| Wide CSV (20 cols) | 1 M | ~1.8 s | ~2.4 s |
| Parquet (10 cols) | 10 M | ~9 s | ~14 s |
| Identical schemas, zero drift | 10 M | ~7 s | ~11 s |

Peak Python RSS stays flat (< 60 MB) regardless of dataset size — scaling work
happens inside DuckDB's out-of-core operators.

## 🧪 Testing

```bash
pip install -e ".[dev]"
python -m pytest -p no:cacheprovider
```

The suite (42 tests) generates every dataset on the fly — CSV, TSV, JSON,
Parquet (via DuckDB's writer) and stdlib-SQLite — and runs fully offline.
`conftest.py` includes a sandbox-tolerant `tmp_path` fixture for environments
that forbid `rmtree`.

## 📁 Project layout

```
duck_diff/
├── action.yml           # GitHub Action metadata (composite run)
├── pyproject.toml       # packaging, entry points, extras
├── conftest.py          # pytest configuration
├── duck_diff/
│   ├── io.py            # universal loader (parquet/csv/tsv/json/sqlite)
│   ├── schema_diff.py   # DESCRIBE-based schema drift
│   ├── engine.py        # DuckDiffer core (keyed & keyless SQL algebra)
│   ├── reporter.py      # rich / ASCII / markdown / json formatters
│   ├── cli.py           # typer-first CLI with argparse fallback
│   └── action.py        # GitHub Action runtime (python -m duck_diff.action)
└── tests/test_diff.py   # complete offline suite
```

## 📄 License

MIT — see [LICENSE](LICENSE).
