"""Command-line interface for :mod:`duck_diff`.

Uses **Typer** when installed and falls back to a dependency-free
**argparse** parser otherwise. Both paths funnel into the same
executors, so behaviour (flags, formats, exit codes) is
identical regardless of which backend parsed the command line.

Subcommands
-----------
* ``diff``    — full row + schema diff (the core command).
* ``schema``  — metadata-only schema drift report.
* ``serve``   — launch the Web Studio dashboard on port 8090.
* ``pricing`` — display tier comparison table in the terminal.
* ``activate``— activate a Pro or Founder license key.
* ``status``  — show engine version, tier, and license status.

Exit codes
----------
* ``0`` — success without drift (or drift tolerated).
* ``1`` — drift detected while ``--fail-on-drift`` was set.
* ``2`` — usage or runtime error (bad path, unreadable data, ...).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .engine import DiffResult, DuckDiffer
from .io import SourceError
from .reporter import FORMATS, render

__all__ = [
    "app",
    "build_arg_parser",
    "emit_stdout",
    "execute_diff",
    "main",
]

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


def _run_diff(ns: Any) -> int:
    """Execute a parsed-namespace CLI diff invocation."""
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


def _run_schema(args: Sequence[str]) -> int:
    """Execute the schema subcommand."""
    if len(args) < 2:
        print("duck-diff schema: requires SOURCE and TARGET arguments.", file=sys.stderr)
        return EXIT_ERROR
    source, target = args[0], args[1]
    fmt = "json"
    output_path = None
    i = 2
    while i < len(args):
        if args[i] == "--format" and i + 1 < len(args):
            fmt = args[i + 1]
            i += 2
        elif args[i] in ("-o", "--output") and i + 1 < len(args):
            output_path = args[i + 1]
            i += 2
        else:
            i += 1
    try:
        differ = DuckDiffer()
        try:
            from .io import load_source
            from .schema_diff import diff_schemas

            loaded_a = load_source(differ.connection, source, "__sch_a")
            loaded_b = load_source(differ.connection, target, "__sch_b")
            schema = diff_schemas(differ.connection, loaded_a.relation_sql, loaded_b.relation_sql)
        finally:
            differ.close()

        if fmt == "json":
            payload = json.dumps(schema.to_dict(), indent=2, ensure_ascii=False)
        else:
            lines = ["Schema drift report", "=" * 40]
            lines.append(f"Source : {loaded_a.description}")
            lines.append(f"Target : {loaded_b.description}")
            lines.append(f"Drift  : {'YES' if schema.has_schema_drift else 'no'}")
            if schema.only_in_a:
                lines.append(f"  only in source : {', '.join(schema.only_in_a)}")
            if schema.only_in_b:
                lines.append(f"  only in target : {', '.join(schema.only_in_b)}")
            for item in schema.type_mismatches:
                lines.append(f"  type mismatch  : {item}")
            if not schema.has_schema_drift:
                lines.append("  Schemas are identical.")
            payload = "\n".join(lines) + "\n"

        if output_path:
            with open(output_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
            print(f"duck-diff: wrote schema report to {output_path}", file=sys.stderr)
        else:
            emit_stdout(payload)
        return EXIT_OK
    except SourceError as exc:
        print(f"duck-diff schema: error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001
        print(f"duck-diff schema: error: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _run_serve(args: Sequence[str]) -> int:
    """Execute the serve subcommand."""
    host = "127.0.0.1"
    port = 8090
    i = 0
    while i < len(args):
        if args[i] == "--host" and i + 1 < len(args):
            host = args[i + 1]
            i += 2
        elif args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        else:
            i += 1
    try:
        from .server import serve_studio

        serve_studio(host=host, port=port)
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001
        print(f"duck-diff serve: error: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _run_pricing() -> int:
    """Print the pricing tier comparison table."""
    lines = [
        "",
        "  duck-diff Pricing",
        "  " + "=" * 54,
        "",
        "  Tier               Price          Features",
        "  " + "-" * 54,
        "  Community          Free forever   Core CLI schema & row diffing,",
        "                                     console terminal summary",
        "",
        "  Pro Annual         $29/year       Interactive Web Studio dashboard,",
        "                                     visual SQL AST diff, CSV/HTML",
        "                                     report export, column stats",
        "",
        "  Founder Lifetime   $49 one-time   All Pro features + priority",
        "                                     updates & roadmap access",
        "",
        "  " + "-" * 54,
        "  Purchase / activate: https://polar.sh/AhmadBilalDSA/duck-diff",
        "  Support: ahmadbilal.dsa@gmail.com",
        "",
        "  Copyright (c) 2026 Ahmad Bilal (AhmadBilalDSA). All Rights Reserved.",
        "",
    ]
    emit_stdout("\n".join(lines))
    return EXIT_OK


def _run_activate(args: Sequence[str]) -> int:
    """Execute the activate subcommand."""
    from .license import LicenseTier, activate_license, deactivate_license

    if not args:
        print("Usage: duck-diff activate <LICENSE_KEY>", file=sys.stderr)
        print("       duck-diff activate --deactivate", file=sys.stderr)
        return EXIT_ERROR

    if args[0] in ("--deactivate", "-d"):
        deactivate_license()
        emit_stdout("duck-diff: license deactivated (reverted to Community tier).")
        return EXIT_OK

    key = args[0]
    tier = LicenseTier.PRO_ANNUAL
    if "--tier" in args:
        idx = args.index("--tier")
        if idx + 1 < len(args):
            tier = args[idx + 1]

    info = activate_license(key, tier=tier)
    if info.is_valid:
        emit_stdout(
            f"duck-diff: license activated!\n"
            f"  Tier    : {info.tier}\n"
            f"  HWID    : {info.hwid[:16]}...\n"
            f"  Expires : {'never' if info.expires_at <= 0 else f'{info.days_remaining} days'}"
        )
        return EXIT_OK
    else:
        print("duck-diff: activation failed — invalid or tampered license key.", file=sys.stderr)
        return EXIT_ERROR


def _run_status() -> int:
    """Execute the status subcommand."""
    from .license import get_license_info

    lic = get_license_info()
    lines = [
        "",
        "  duck-diff status",
        "  " + "=" * 40,
        f"  Version : {__version__}",
        f"  Tier    : {lic.tier}",
        f"  Valid   : {'yes' if lic.is_valid else 'no'}",
        f"  HWID    : {lic.hwid[:16]}..." if lic.hwid else "  HWID    : (community default)",
    ]
    if lic.expires_at > 0:
        lines.append(f"  Expires : {lic.days_remaining} days remaining")
    else:
        lines.append("  Expires : never")
    lines.append("")
    emit_stdout("\n".join(lines))
    return EXIT_OK


# ---------------------------------------------------------------------------
# argparse backend (always available)
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="duck-diff",
        description=(
            "Fast, constant-memory dataset diffing (Parquet/CSV/TSV/JSON/SQLite) "
            "on an embedded DuckDB SQL engine."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    # --- diff ---
    p_diff = sub.add_parser("diff", help="Full row + schema diff between two datasets.")
    p_diff.add_argument("source", help="Baseline dataset path or sqlite://<db>#<table> URI.")
    p_diff.add_argument("target", help="Candidate dataset to compare against.")
    p_diff.add_argument("-k", "--key", default=None, metavar="COLS",
                        help="Comma-separated key columns, e.g. 'id,region'.")
    p_diff.add_argument("--epsilon", type=float, default=0.0, metavar="F",
                        help="Absolute float tolerance for numeric comparisons.")
    p_diff.add_argument("--ignore-case", action="store_true",
                        help="Compare text columns case-insensitively.")
    p_diff.add_argument("--format", choices=list(FORMATS), default="table",
                        help="Output format: table, markdown, json or html.")
    p_diff.add_argument("-o", "--output", "--output-file", dest="output", default=None,
                        metavar="PATH", help="Write report to PATH instead of stdout.")
    p_diff.add_argument("--limit", type=int, default=20, metavar="N",
                        help="Maximum number of sample mismatch records.")
    p_diff.add_argument("--fail-on-drift", action="store_true",
                        help="Exit with code 1 when drift is detected.")
    p_diff.add_argument("--memory-limit", default=None, metavar="SIZE",
                        help='DuckDB memory budget, e.g. "2GB".')

    # --- schema ---
    p_schema = sub.add_parser("schema", help="Metadata-only schema drift report.")
    p_schema.add_argument("source", help="Baseline dataset path or URI.")
    p_schema.add_argument("target", help="Candidate dataset path or URI.")
    p_schema.add_argument("--format", choices=["json", "text"], default="json",
                          help="Output format (default: json).")
    p_schema.add_argument("-o", "--output", dest="output", default=None,
                          help="Write report to file.")

    # --- serve ---
    p_serve = sub.add_parser("serve", help="Launch the Web Studio dashboard on port 8090.")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1).")
    p_serve.add_argument("--port", type=int, default=8090, help="Port number (default: 8090).")

    # --- pricing ---
    sub.add_parser("pricing", help="Display tier comparison table.")

    # --- activate ---
    p_activate = sub.add_parser("activate", help="Activate a Pro or Founder license key.")
    p_activate.add_argument("key", nargs="?", default=None, help="License key string.")
    p_activate.add_argument("--deactivate", action="store_true",
                            help="Remove the current license (revert to Community).")
    p_activate.add_argument("--tier", default="pro_annual",
                            choices=["pro_annual", "founder_lifetime"],
                            help="License tier (default: pro_annual).")

    # --- status ---
    sub.add_parser("status", help="Show engine version, tier, and license status.")

    # --- legacy: bare source target (no subcommand) ---
    parser.add_argument("legacy_source", nargs="?", default=None, help=argparse.SUPPRESS)
    parser.add_argument("legacy_target", nargs="?", default=None, help=argparse.SUPPRESS)
    parser.add_argument("-k", "--key", default=None, metavar="COLS", help=argparse.SUPPRESS)
    parser.add_argument("--epsilon", type=float, default=0.0, help=argparse.SUPPRESS)
    parser.add_argument("--ignore-case", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--format", choices=list(FORMATS), default="table", help=argparse.SUPPRESS)
    parser.add_argument("-o", "--output", "--output-file", dest="output", default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--limit", type=int, default=20, help=argparse.SUPPRESS)
    parser.add_argument("--fail-on-drift", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--memory-limit", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"duck-diff {__version__}")
    return parser


# ---------------------------------------------------------------------------
# Typer backend (used automatically when typer is installed)
# ---------------------------------------------------------------------------

app: Any = None  # Will be set to the Typer app when typer is available.


def _typer_main(argv: Optional[Sequence[str]]) -> int:
    import click  # guaranteed present when typer is

    typer_app = _typer.Typer(
        add_completion=False,
        no_args_is_help=True,
        help="Fast, constant-memory dataset diffing powered by DuckDB.",
    )

    def _version_callback(value: bool) -> None:
        if value:
            _typer.echo(f"duck-diff {__version__}")
            raise _typer.Exit()

    @typer_app.command()
    def diff(  # noqa: ANN001 - typer inspects signatures at runtime
        source: str = _typer.Argument(..., help="Baseline dataset path or sqlite:// URI."),
        target: str = _typer.Argument(..., help="Candidate dataset to compare against."),
        key: Optional[str] = _typer.Option(
            None, "--key", "-k", help="Comma-separated key columns."
        ),
        epsilon: float = _typer.Option(0.0, "--epsilon", help="Absolute float tolerance."),
        ignore_case: bool = _typer.Option(False, "--ignore-case"),
        format_: str = _typer.Option("table", "--format", help="table|markdown|json|html"),
        output: Optional[str] = _typer.Option(
            None, "--output", "--output-file", "-o", help="Report output file path."
        ),
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
            source=source, target=target, key=key, epsilon=epsilon,
            ignore_case=ignore_case, format=format_, output=output,
            limit=limit, fail_on_drift=fail_on_drift, memory_limit=memory_limit,
        )
        return _run_diff(namespace)

    @typer_app.command()
    def schema(  # noqa: ANN001
        source: str = _typer.Argument(..., help="Baseline dataset path or URI."),
        target: str = _typer.Argument(..., help="Candidate dataset path or URI."),
        format_: str = _typer.Option("json", "--format", help="json|text"),
        output: Optional[str] = _typer.Option(None, "--output", "-o"),
    ) -> int:
        """Metadata-only schema drift report."""
        args_list: List[str] = [source, target]
        if format_ != "json":
            args_list.extend(["--format", format_])
        if output:
            args_list.extend(["--output", output])
        return _run_schema(args_list)

    @typer_app.command()
    def serve(  # noqa: ANN001
        host: str = _typer.Option("127.0.0.1", "--host"),
        port: int = _typer.Option(8090, "--port"),
    ) -> int:
        """Launch the Web Studio dashboard."""
        return _run_serve(["--host", host, "--port", str(port)])

    @typer_app.command()
    def pricing() -> int:
        """Display tier comparison table."""
        return _run_pricing()

    @typer_app.command(name="activate")
    def activate_cmd(  # noqa: ANN001
        key: Optional[str] = _typer.Argument(None, help="License key string."),
        deactivate: bool = _typer.Option(False, "--deactivate", "-d"),
        tier: str = _typer.Option("pro_annual", "--tier"),
    ) -> int:
        """Activate a Pro or Founder license key."""
        args_list: List[str] = []
        if deactivate:
            args_list.append("--deactivate")
        elif key:
            args_list.append(key)
            args_list.extend(["--tier", tier])
        return _run_activate(args_list)

    @typer_app.command()
    def status() -> int:
        """Show engine version, tier, and license status."""
        return _run_status()

    global app  # noqa: PLW0603
    app = typer_app

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        result_value = typer_app(args, standalone_mode=False, prog_name="duck-diff")
    except SystemExit as exc:
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
    # argparse fallback — dispatch subcommands manually
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw:
        parser = build_arg_parser()
        parser.parse_args([])
        return EXIT_OK

    cmd = raw[0]
    if cmd == "diff":
        return _run_diff_raw(raw[1:])
    elif cmd == "schema":
        return _run_schema(raw[1:])
    elif cmd == "serve":
        return _run_serve(raw[1:])
    elif cmd == "pricing":
        return _run_pricing()
    elif cmd == "activate":
        return _run_activate(raw[1:])
    elif cmd == "status":
        return _run_status()
    elif cmd in ("--version", "-V"):
        print(f"duck-diff {__version__}")
        raise SystemExit(EXIT_OK)
    elif cmd in ("--help", "-h"):
        parser = build_arg_parser()
        parser.parse_args([cmd])
        return EXIT_OK
    else:
        # Legacy: bare "source target" without subcommand
        return _run_diff_raw(raw)


def _run_diff_raw(raw: List[str]) -> int:
    """Parse and execute diff from raw argv tokens."""
    parser = build_arg_parser()
    ns = parser.parse_args(["diff"] + raw)
    return _run_diff(ns)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
