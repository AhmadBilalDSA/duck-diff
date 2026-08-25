"""Model Context Protocol (MCP) server for :mod:`duck_diff`.

A zero-extra-dependency, stdio-based MCP server (newline-delimited JSON-RPC
2.0 per the MCP stdio transport) that lets AI agents — Claude Desktop,
Cursor, Windsurf, DeepSeek, etc. — drive ``duck_diff`` natively.

Exposed tools
-------------
* ``diff_datasets``       — full reconciliation summary (JSON payload).
* ``inspect_schema_drift``— column types, missing columns, type drift.
* ``get_column_stats``    — single-pass in-engine statistical drift metrics.

Run directly::

    python -m duck_diff.mcp_server

or via the installed console script ``duck-diff-mcp``. Register in an MCP
client (e.g. Claude Desktop ``claude_desktop_config.json``)::

    {
      "mcpServers": {
        "duck-diff": { "command": "duck-diff-mcp" }
      }
    }

All heavy work executes inside DuckDB through :class:`duck_diff.DuckDiffer`,
preserving constant-memory guarantees; connections are always released via
``try/finally``.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .cli import execute_diff
from .engine import DuckDiffer
from .io import load_source
from .schema_diff import diff_schemas

__all__ = ["TOOL_DEFINITIONS", "call_tool", "handle_line", "handle_message", "main"]

_PROTOCOL_VERSION = "2024-11-05"

_JSONRPC_PARSE_ERROR = -32700
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_PARAMS = -32602


# ---------------------------------------------------------------------------
# Tool implementations (also usable programmatically without stdio)
# ---------------------------------------------------------------------------


def _normalize_keys(raw: Any) -> Optional[List[str]]:
    """Accept ``"id,region"``, ``["id","region"]`` or ``None`` for *key*."""
    if raw is None:
        return None
    if isinstance(raw, str):
        keys = [part.strip() for part in raw.split(",") if part.strip()]
        return keys or None
    if isinstance(raw, Sequence):
        keys = [str(part).strip() for part in raw if str(part).strip()]
        return keys or None
    raise ValueError("key must be a comma-separated string or a list of column names")


def _tool_diff_datasets(args: Dict[str, Any]) -> Dict[str, Any]:
    source = args.get("source")
    target = args.get("target")
    if not source or not target:
        raise ValueError("Both 'source' and 'target' are required.")
    tolerance = float(args.get("tolerance", 0.0) or 0.0)
    result = execute_diff(
        str(source),
        str(target),
        keys=_normalize_keys(args.get("key")),
        epsilon=tolerance,
        sample_limit=int(args.get("sample_limit", 20) or 20),
    )
    return result.to_dict()


def _tool_inspect_schema_drift(args: Dict[str, Any]) -> Dict[str, Any]:
    source = args.get("source")
    target = args.get("target")
    if not source or not target:
        raise ValueError("Both 'source' and 'target' are required.")
    differ = DuckDiffer()
    try:
        conn = differ.connection
        loaded_a = load_source(conn, str(source), "__mcp_a")
        loaded_b = load_source(conn, str(target), "__mcp_b")
        schema = diff_schemas(conn, loaded_a.relation_sql, loaded_b.relation_sql)
        return {
            "source": loaded_a.description,
            "target": loaded_b.description,
            **schema.to_dict(),
        }
    finally:
        differ.close()


def _tool_get_column_stats(args: Dict[str, Any]) -> Dict[str, Any]:
    source = args.get("source")
    target = args.get("target")
    if not source or not target:
        raise ValueError("Both 'source' and 'target' are required.")
    result = execute_diff(
        str(source),
        str(target),
        keys=_normalize_keys(args.get("key")),
        epsilon=float(args.get("tolerance", 0.0) or 0.0),
        sample_limit=1,
    )
    return {
        "mode": result.mode,
        "keys": result.keys,
        "total_rows_a": result.summary.total_rows_a,
        "total_rows_b": result.summary.total_rows_b,
        "column_stats": result.summary.column_stats,
    }


_TOOL_HANDLERS = {
    "diff_datasets": _tool_diff_datasets,
    "inspect_schema_drift": _tool_inspect_schema_drift,
    "get_column_stats": _tool_get_column_stats,
}

_OBJECT = {"type": "object"}
_STR = {"type": "string"}

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "diff_datasets",
        "description": (
            "Reconcile two datasets (Parquet/CSV/TSV/JSON/SQLite URI) and return "
            "the full structured diff: schema drift, row counts, per-column drift "
            "statistics, statistical distribution deltas and sample mismatches."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {**_STR, "description": "Baseline path or sqlite://db#table URI"},
                "target": {**_STR, "description": "Candidate path or sqlite://db#table URI"},
                "key": {
                    "oneOf": [_STR, {"type": "array", "items": _STR}],
                    "description": "Comma-separated key columns or list; omit for keyless mode",
                },
                "tolerance": {"type": "number", "default": 0.0,
                              "description": "Absolute float tolerance for value columns"},
                "sample_limit": {"type": "integer", "default": 20},
                "format": {**_STR, "enum": ["json"], "default": "json"},
            },
            "required": ["source", "target"],
        },
    },
    {
        "name": "inspect_schema_drift",
        "description": (
            "Compare two dataset schemas metadata-only: shared columns with types, "
            "columns missing on either side, and type-mismatch strings."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"source": _STR, "target": _STR},
            "required": ["source", "target"],
        },
    },
    {
        "name": "get_column_stats",
        "description": (
            "Single-pass statistical drift per shared column computed inside DuckDB: "
            "numeric null-count/mean/min/max deltas + percentage shift; categorical "
            "null-count and distinct-count deltas."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": _STR,
                "target": _STR,
                "key": {"oneOf": [_STR, {"type": "array", "items": _STR}]},
                "tolerance": {"type": "number", "default": 0.0},
            },
            "required": ["source", "target"],
        },
    },
]


def call_tool(name: str, arguments: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Execute *name* with *arguments*, returning the JSON-serialisable payload."""
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        raise KeyError(f"Unknown tool: {name!r}. Available: {sorted(_TOOL_HANDLERS)}")
    return handler(dict(arguments or {}))


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------


def _result(request_id: Any, payload: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_message(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Handle one decoded JSON-RPC message; ``None`` means "send nothing"."""
    method = message.get("method")
    request_id = message.get("id")

    if method is None:
        # A response to a server-initiated request — nothing to do.
        return None

    params = message.get("params") or {}

    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "duck-diff-mcp", "version": __version__},
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": TOOL_DEFINITIONS})
    if method == "tools/call":
        name = str(params.get("name", ""))
        arguments = params.get("arguments") or {}
        try:
            payload = call_tool(name, arguments)
        except KeyError as exc:
            content = [{"type": "text", "text": str(exc)}]
            return _result(request_id, {"content": content, "isError": True})
        except Exception as exc:  # noqa: BLE001 - never leak tracebacks to clients
            content = [
                {
                    "type": "text",
                    "text": f"duck-diff error ({exc.__class__.__name__}): {exc}",
                }
            ]
            return _result(request_id, {"content": content, "isError": True})
        text = json.dumps(payload, ensure_ascii=False, default=str)
        return _result(request_id, {"content": [{"type": "text", "text": text}],
                                    "isError": False})

    if request_id is None:
        # Notification we do not specifically handle (e.g. initialized).
        return None
    return _error(request_id, _JSONRPC_METHOD_NOT_FOUND, f"Unknown method: {method!r}")


def handle_line(line: str) -> Optional[str]:
    """Process one raw stdin line; returns the response line or ``None``."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        message = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return json.dumps(
            _error(None, _JSONRPC_PARSE_ERROR, f"Parse error: {exc}")
        ) + "\n"
    if not isinstance(message, dict):
        return json.dumps(
            _error(None, _JSONRPC_INVALID_PARAMS, "Request must be a JSON object")
        ) + "\n"
    response = handle_message(message)
    if response is None:
        return None
    return json.dumps(response, ensure_ascii=False, default=str) + "\n"


def serve(stdin: Any = None, stdout: Any = None) -> int:
    """Serve MCP requests until EOF. Never writes logs to stdout (protocol only)."""
    input_stream = stdin if stdin is not None else sys.stdin
    output_stream = stdout if stdout is not None else sys.stdout
    for raw_line in input_stream:
        response = handle_line(raw_line)
        if response is not None:
            output_stream.write(response)
            output_stream.flush()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console-script entry point (``duck-diff-mcp``)."""
    del argv  # The stdio server takes no options by design.
    try:
        return serve()
    except BrokenPipeError:  # Client closed the stream — exit cleanly.
        return 0
    except KeyboardInterrupt:  # pragma: no cover
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
