"""Command-line interface for :mod:`duck_diff`.

Uses **Typer** when installed and falls back to a dependency-free
**argparse** parser otherwise. Both paths funnel into the same
:func:`_run` executor, so behaviour (flags, formats, exit codes) is
identical regardless of which backend parsed the command line.

Exit codes
----------
* ``0`` — success without drift (or drift tolerated).
* ``1`` — drift detected while ``--fail-on-drift`` was set.
* ``2`` — usage or runtime error (bad path, unreadable data, ...).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .engine import DiffResult, DuckDiffer
from .io import SourceError
from .reporter import FORMATS, render

__all__ = ["build_arg_parser", "emit_stdout", "execute_diff", "main"]

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2

try:  # Optional CLI sugar; the argparse fallback keeps us dependency-free.
    import typer as _typer  # type: ignore

    _TYPER_AVAILABLE = True
except Exception:  # noqa: BLE001
    _typer = None  # type: ignore[assignment]
    _TYPER_AVAILABLE = False


def execute_diff(
    source: str,
    target: str,
    keys: Optional[Sequence[str]] = None,
    epsilon: float = 0.0,
    ignore_case: bool = False,
    sample_limit: int = 20,
    memory_limit: Optional[str] = None,
) -> DiffResult:
    """Run one diff with a fresh embedded DuckDB engine and release it."""
    differ = DuckDiffer(
        epsilon=epsilon,
        ignore_case=ignore_case,
        sample_limit=sample_limit,
        memory_limit=memory_limit,
    )
    try:
        return differ.diff(source, target, keys=list(keys) if keys else None)
    finally:
        differ.close()


def parse_keys(spec: Optional[str]) -> Optional[List[str]]:
    """Split ``"id,region"`` into ``["id", "region"]`` (``None`` when empty)."""
    if not spec:
        return None
    keys = [part.strip() for part in str(spec).split(",") if part.strip()]
    return keys or None


# ---------------------------------------------------------------------------
# Shared execution core
# ---------------------------------------------------------------------------


def emit_stdout(text: str) -> None:
    """Print *text* without dying on non-encodable glyphs (emoji, box-drawing).

    Legacy Windows consoles default to legacy codepages (e.g. cp1252); we
    reconfigure the stream to UTF-8 with replacement fallbacks so reports
    always print, whatever the active codepage.
    """
    stream = sys.stdout
    try:
        stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - not a reconfigurable TextIOWrapper
        pass
    try:
        stream.write(text)
        if not text.endswith("\n"):
            stream.write("\n")
        stream.flush()
    except UnicodeEncodeError:
        stream.buffer.write(  # type: ignore[union-attr]
            (text + "\n").encode("utf-8", errors="backslashreplace")
        )
        stream.flush()


def _run(ns: Any) -> int:
    """Execute a parsed-namespace CLI invocation. Never raises on bad input."""
    try:
        result = execute_diff(
            ns.source,
            ns.target,
            keys=parse_keys(getattr(ns, "key", None)),
            epsilon=float(ns.epsilon),
            ignore_case=bool(ns.ignore_case),
            sample_limit=int(ns.limit),
            memory_limit=getattr(ns, "memory_limit", None),
        )
        payload = render(result, ns.format)
        output_path = getattr(ns, "output", None)
        if output_path:
            with open(output_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                if not payload.endswith("\n"):
                    handle.write("\n")
            print(
                f"duck-diff: wrote {ns.format} report to {output_path}",
                file=sys.stderr,
            )
        else:
            emit_stdout(payload)
        return EXIT_DRIFT if (ns.fail_on_drift and result.drift_detected) else EXIT_OK
    except SourceError as exc:
        print(f"duck-diff: error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - a CLI must fail cleanly, not traceback
        print(
            f"duck-diff: error: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_ERROR


# ---------------------------------------------------------------------------
# argparse backend (always available)
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser mirroring the documented flags."""
    parser = argparse.ArgumentParser(
        prog="duck-diff",
        description=(
            "Fast, constant-memory dataset diffing (Parquet/CSV/TSV/JSON/SQLite) "
            "on an embedded DuckDB SQL engine."
        ),
    )
    parser.add_argument("source", help="Baseline dataset path or sqlite://<db>#<table> URI.")
    parser.add_argument("target", help="Candidate dataset to compare against.")
    parser.add_argument(
        "-k",
        "--key",
        default=None,
        metavar="COLS",
        help="Comma-separated key columns, e.g. 'id,region'. Omit for keyless hashing.",
    )
    parser.add_argument(
        "--epsilon",
        type=float,
        default=0.0,
        metavar="F",
        help="Absolute float tolerance for numeric comparisons (default: 0).",
    )
    parser.add_argument(
        "--ignore-case",
        action="store_true",
        help="Compare text columns case-insensitively.",
    )
    parser.add_argument(
        "--format",
        choices=list(FORMATS),
        default="table",
        help="Output format: table (Rich/ASCII), markdown or json.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        metavar="PATH",
        help="Write the report to PATH instead of stdout.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        metavar="N",
        help="Maximum number of sample mismatch records (default: 20).",
    )
    parser.add_argument(
        "--fail-on-drift",
        action="store_true",
        help="Exit with code 1 when any drift (row-level or schema) is detected.",
    )
    parser.add_argument(
        "--memory-limit",
        default=None,
        metavar="SIZE",
        help='DuckDB memory budget for constant-memory operation, e.g. "2GB".',
    )
    parser.add_argument("--version", action="version", version=f"duck-diff {__version__}")
    return parser


# ---------------------------------------------------------------------------
# Typer backend (used automatically when typer is installed)
# ---------------------------------------------------------------------------


def _typer_main(argv: Optional[Sequence[str]]) -> int:
    import click  # guaranteed present when typer is

    app = _typer.Typer(add_completion=False, no_args_is_help=True)

    def _version_callback(value: bool) -> None:
        if value:
            _typer.echo(f"duck-diff {__version__}")
            raise _typer.Exit()

    @app.command()
    def diff(  # noqa: ANN001 - typer inspects signatures at runtime
        source: str = _typer.Argument(..., help="Baseline dataset path or sqlite:// URI."),
        target: str = _typer.Argument(..., help="Candidate dataset to compare against."),
        key: Optional[str] = _typer.Option(
            None, "--key", "-k", help="Comma-separated key columns."
        ),
        epsilon: float = _typer.Option(
            0.0, "--epsilon", help="Absolute float tolerance."
        ),
        ignore_case: bool = _typer.Option(False, "--ignore-case"),
        format_: str = _typer.Option("table", "--format", help="table|markdown|json"),
        output: Optional[str] = _typer.Option(None, "--output", "-o"),
        limit: int = _typer.Option(20, "--limit"),
        fail_on_drift: bool = _typer.Option(False, "--fail-on-drift"),
        memory_limit: Optional[str] = _typer.Option(None, "--memory-limit"),
        version: bool = _typer.Option(
            False, "--version", callback=_version_callback, is_eager=True
        ),
    ) -> int:
        """Diff SOURCE against TARGET and emit a report."""
        from types import SimpleNamespace

        namespace = SimpleNamespace(
            source=source,
            target=target,
            key=key,
            epsilon=epsilon,
            ignore_case=ignore_case,
            format=format_,
            output=output,
            limit=limit,
            fail_on_drift=fail_on_drift,
            memory_limit=memory_limit,
        )
        return _run(namespace)

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        result_value = app(args, standalone_mode=False, prog_name="duck-diff")
    except SystemExit as exc:  # --version via eager callback under some click versions
        code = exc.code
        return code if isinstance(code, int) else EXIT_OK
    except _typer.Exit as exc:  # type: ignore[attr-defined]
        return int(exc.exit_code or 0)
    except click.exceptions.ClickException as exc:
        print(f"duck-diff: error: {exc.format_message()}", file=sys.stderr)
        return EXIT_ERROR
    except click.exceptions.Abort:
        print("duck-diff: aborted.", file=sys.stderr)
        return EXIT_ERROR
    if isinstance(result_value, int):
        return result_value
    return EXIT_OK


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point (also referenced by the ``duck-diff`` console script).

    Prefers the Typer backend when available; set
    ``DUCK_DIFF_CLI_BACKEND=argparse`` to force the fallback parser.
    """
    backend = os.environ.get("DUCK_DIFF_CLI_BACKEND", "auto").strip().lower()
    if _TYPER_AVAILABLE and backend != "argparse":
        try:
            return _typer_main(argv)
        except Exception:  # noqa: BLE001 - optional dependency must never break the CLI
            if backend == "typer":
                raise
    parser = build_arg_parser()
    namespace = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    return _run(namespace)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
