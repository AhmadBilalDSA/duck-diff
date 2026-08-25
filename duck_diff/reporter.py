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

import io as _io
import json
import sys
from typing import Any, List, Optional, Sequence
from urllib.parse import quote as _urlquote

from . import __version__
from .engine import DiffResult

__all__ = ["FORMATS", "render", "render_ascii", "render_terminal", "to_json", "to_markdown"]

FORMATS = ("table", "markdown", "json")

_FORMAT_ALIASES = {
    "table": "table",
    "terminal": "table",
    "ascii": "table",
    "rich": "table",
    "markdown": "markdown",
    "md": "markdown",
    "json": "json",
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
# Dispatcher
# ---------------------------------------------------------------------------


def render(result: DiffResult, fmt: str = "table", color: Optional[bool] = None) -> str:
    """Render *result* in the requested format.

    Args:
        result: The diff outcome.
        fmt: One of ``table``, ``markdown``, ``json`` (aliases accepted).
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
    return render_terminal(result, color=color)
