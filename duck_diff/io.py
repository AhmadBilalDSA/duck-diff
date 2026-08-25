"""Universal file & source loading for :mod:`duck_diff`.

Every supported source is registered as an in-memory DuckDB *view* so the
diff engine can reference it with a plain identifier while DuckDB streams the
underlying file — datasets never need to be materialised in Python RAM.

Supported forms
---------------
* ``*.parquet``                    -> ``read_parquet(?)``
* ``*.csv``                        -> ``read_csv_auto(?, header=true)``
* ``*.tsv``                        -> ``read_csv_auto(?, header=true, delim='\\t')``
* ``*.json`` / ``.jsonl`` / ``.ndjson`` -> ``read_json_auto(?)``
* ``sqlite://<db_path>#<table>``   -> DuckDB SQLite attach, with an automatic
  chunked stdlib-:mod:`sqlite3` fallback ingest when the SQLite extension is
  unavailable (keeps tests and air-gapped CI fully offline-capable).
"""

from __future__ import annotations

import os
import re
import sqlite3 as _stdlib_sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

__all__ = [
    "LoadedSource",
    "SourceError",
    "detect_kind",
    "load_source",
    "parse_sqlite_uri",
    "quote_identifier",
    "sql_literal",
]

_SQL_CHUNK_ROWS = 50_000

_FORMAT_BY_SUFFIX: Dict[str, str] = {
    ".parquet": "parquet",
    ".csv": "csv",
    ".tsv": "tsv",
    ".tab": "tsv",
    ".json": "json",
    ".jsonl": "json",
    ".ndjson": "json",
}

_SQLITE_SCHEME_RE = re.compile(r"^sqlite3?://", re.IGNORECASE)


class SourceError(RuntimeError):
    """Raised when a dataset source cannot be resolved or opened."""


def sql_literal(value: str) -> str:
    """Escape *value* for safe interpolation as a DuckDB single-quoted literal."""
    return "'" + str(value).replace("'", "''") + "'"


def quote_identifier(name: str) -> str:
    """Escape *name* as a quoted SQL identifier (handles quotes/reserved words)."""
    return '"' + str(name).replace('"', '""') + '"'


def _posix(path: str) -> str:
    """Normalise a filesystem path to forward slashes (Windows friendly)."""
    return str(Path(path)).replace("\\", "/")


@dataclass(frozen=True)
class LoadedSource:
    """A dataset registered inside a DuckDB connection."""

    relation_sql: str
    description: str
    warnings: Tuple[str, ...] = field(default_factory=tuple)


def detect_kind(source: str) -> str:
    """Classify *source* as one of ``parquet|csv|tsv|json|sqlite``.

    Raises:
        SourceError: If the format cannot be determined from the path/URI.
    """
    text = str(source)
    if _SQLITE_SCHEME_RE.match(text):
        return "sqlite"
    suffix = Path(text).suffix.lower()
    kind = _FORMAT_BY_SUFFIX.get(suffix)
    if kind is None:
        supported = ", ".join(sorted(set(_FORMAT_BY_SUFFIX.values())))
        raise SourceError(
            f"Cannot detect format for {text!r} (suffix {suffix!r}). "
            f"Supported: parquet, csv, tsv, json/jsonl/ndjson, sqlite:// URIs ({supported})."
        )
    if not os.path.exists(text):
        raise SourceError(f"Dataset file does not exist: {text!r}")
    return kind


def parse_sqlite_uri(source: str) -> Tuple[str, str]:
    """Split ``sqlite://<db_path>#<table>`` into ``(db_path, table_name)``.

    Accepts ``sqlite://`` and ``sqlite3://`` schemes, two or three slashes,
    and Windows drive letters (``sqlite:///C:/data.db#t``).
    """
    text = str(source)
    body = _SQLITE_SCHEME_RE.sub("", text, count=1)
    if "#" not in body:
        raise SourceError(
            f"Invalid SQLite URI {text!r}: expected sqlite://<db_path>#<table_name>."
        )
    db_path, _, table = body.partition("#")
    db_path = db_path.strip()
    table = table.strip()
    # Normalise a leading slash before a Windows drive letter: "/C:/x.db" -> "C:/x.db".
    if re.match(r"^/[A-Za-z]:/", db_path):
        db_path = db_path[1:]
    db_path = db_path.replace("\\", "/")
    if not db_path or not table:
        raise SourceError(
            f"Invalid SQLite URI {text!r}: both database path and table are required."
        )
    return db_path, table


def _reader_call(kind: str, path: str, extra: str = "") -> str:
    """Build a DuckDB table function call; *extra* injects additional kwargs."""
    lit = sql_literal(_posix(path))
    if kind == "parquet":
        return f"read_parquet({lit}{extra})"
    if kind == "csv":
        return f"read_csv_auto({lit}, header=true{extra})"
    if kind == "tsv":
        return f"read_csv_auto({lit}, header=true, delim='\\t'{extra})"
    if kind == "json":
        return f"read_json_auto({lit}{extra})"
    raise SourceError(f"Unsupported reader kind: {kind!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# SQLite support
# ---------------------------------------------------------------------------


def _quoted_sqlite_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _map_sqlite_type(declared: str) -> str:
    upper = (declared or "").upper()
    if "INT" in upper:
        return "BIGINT"
    if any(tok in upper for tok in ("REAL", "FLOA", "DOUB", "NUM", "DEC")):
        return "DOUBLE"
    return "VARCHAR"


def _list_sqlite_tables(sconn: "_stdlib_sqlite3.Connection") -> List[str]:
    rows = sconn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
    ).fetchall()
    return [str(r[0]) for r in rows]


def _attach_sqlite(conn: "object", alias: str, db_path: str) -> None:
    """Attach *db_path* through the DuckDB SQLite extension."""
    try:  # Best effort detach of a stale attach from a previous call.
        conn.execute(f"DETACH {quote_identifier(alias)}")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - missing attach is fine
        pass
    conn.execute(  # type: ignore[attr-defined]
        f"ATTACH {sql_literal(db_path)} AS {quote_identifier(alias)} (TYPE SQLITE)"
    )


def _ingest_sqlite_fallback(
    conn: "object", alias: str, db_path: str, table: str
) -> List[str]:
    """Chunked stdlib-sqlite3 ingest; keeps memory bounded by ``_SQL_CHUNK_ROWS``.

    Returns a list of human-readable warnings.
    """
    warnings: List[str] = []
    if not os.path.exists(db_path):
        raise SourceError(f"SQLite database does not exist: {db_path!r}")
    uri = f"file:{_posix(db_path)}?mode=ro"
    sconn = _stdlib_sqlite3.connect(uri, uri=True)
    try:
        info = sconn.execute(f"PRAGMA table_info({_quoted_sqlite_ident(table)})").fetchall()
        if not info:
            available = ", ".join(_list_sqlite_tables(sconn)) or "<none>"
            raise SourceError(
                f"Table {table!r} not found in {db_path!r}. Available tables: {available}."
            )
        columns: List[Tuple[str, str]] = [(str(r[1]), _map_sqlite_type(str(r[2]))) for r in info]
        col_defs = ", ".join(f"{quote_identifier(n)} {t}" for n, t in columns)
        col_list = ", ".join(quote_identifier(n) for n, _ in columns)
        conn.execute(  # type: ignore[attr-defined]
            f"CREATE OR REPLACE TEMP TABLE {quote_identifier(alias)} ({col_defs})"
        )
        placeholders = ", ".join("?" for _ in columns)
        insert_sql = (
            f"INSERT INTO {quote_identifier(alias)} ({col_list}) VALUES ({placeholders})"
        )
        cur = sconn.execute(f"SELECT {col_list} FROM {_quoted_sqlite_ident(table)}")
        total = 0
        while True:
            rows = cur.fetchmany(_SQL_CHUNK_ROWS)
            if not rows:
                break
            conn.executemany(insert_sql, rows)  # type: ignore[attr-defined]
            total += len(rows)
        warnings.append(
            "DuckDB SQLite extension unavailable; ingested via chunked stdlib "
            f"sqlite3 fallback ({total} rows, {_SQL_CHUNK_ROWS}/chunk)."
        )
    finally:
        sconn.close()
    return warnings


def _load_sqlite(conn, source: str, alias: str) -> LoadedSource:
    db_path, table = parse_sqlite_uri(source)
    attach_alias = "__dd_sq"
    warnings: List[str] = []
    attached = False
    try:
        _attach_sqlite(conn, attach_alias, db_path)
        attached = True
        conn.execute(
            f"SELECT * FROM {quote_identifier(attach_alias)}.{quote_identifier(table)} LIMIT 0"
        )
        relation = (
            f"{quote_identifier(attach_alias)}.{quote_identifier(table)}"
        )
        description = f"sqlite://{db_path}#{table}"
    except Exception as exc:  # noqa: BLE001 - fall back to stdlib ingest
        warnings.append(f"DuckDB SQLite attach failed ({exc.__class__.__name__}); using fallback.")
        if attached:
            try:
                conn.execute(f"DETACH {quote_identifier(attach_alias)}")
            except Exception:  # noqa: BLE001
                pass
        warnings.extend(_ingest_sqlite_fallback(conn, alias, db_path, table))
        relation = quote_identifier(alias)
        description = f"sqlite://{db_path}#{table} (fallback ingest)"
    return LoadedSource(relation_sql=relation, description=description, warnings=tuple(warnings))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def load_source(conn, source: str, alias: str) -> LoadedSource:
    """Register *source* in *conn* under temp view *alias*.

    Returns a :class:`LoadedSource` whose ``relation_sql`` is the quoted view
    name, ready to be embedded in SQL.
    """
    kind = detect_kind(source)
    warnings: List[str] = []
    if kind == "sqlite":
        loaded = _load_sqlite(conn, source, alias)
        warnings.extend(loaded.warnings)
        if loaded.relation_sql != quote_identifier(alias):
            # Attached catalog table: expose it through the stable alias too.
            conn.execute(
                f"CREATE OR REPLACE TEMP VIEW {quote_identifier(alias)} AS "
                f"SELECT * FROM {loaded.relation_sql}"
            )
            return LoadedSource(
                relation_sql=quote_identifier(alias),
                description=loaded.description,
                warnings=tuple(warnings),
            )
        loaded_warned = LoadedSource(
            relation_sql=loaded.relation_sql,
            description=loaded.description,
            warnings=tuple(warnings),
        )
        return loaded_warned

    if not os.path.exists(source):
        raise SourceError(f"Dataset file does not exist: {source!r}")
    create_view = "CREATE OR REPLACE TEMP VIEW {alias} AS SELECT * FROM {call}"
    alias_sql = quote_identifier(alias)
    if kind in ("csv", "tsv"):
        try:
            conn.execute(
                create_view.format(alias=alias_sql, call=_reader_call(kind, source))
            )
        except Exception:  # noqa: BLE001 - fall back to DuckDB's lenient CSV parser
            # Mixed line endings or ragged rows can defeat the strict sniffer;
            # retry once with strict_mode disabled before giving up.
            conn.execute(
                create_view.format(
                    alias=alias_sql,
                    call=_reader_call(kind, source, extra=", strict_mode=false"),
                )
            )
            warnings.append(
                "CSV auto-detection needed lenient parsing (strict_mode=false); "
                "verify delimiter/encoding if results look off."
            )
    else:
        conn.execute(create_view.format(alias=alias_sql, call=_reader_call(kind, source)))
    return LoadedSource(
        relation_sql=quote_identifier(alias), description=str(source)
    )
