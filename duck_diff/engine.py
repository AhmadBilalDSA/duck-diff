"""The :class:`DuckDiffer` core engine.

All heavy lifting happens **inside DuckDB** via ``FULL OUTER JOIN`` /
``EXCEPT``-style set algebra over SQL views, so memory stays constant no
matter how large the datasets are — only aggregates and small samples cross
the Python boundary.

Diff modes
----------
* **Keyed** (``--key id,region``): null-safe join on the primary key
  (``IS NOT DISTINCT FROM``), then per-column cell comparison with optional
  float epsilon tolerance.
* **Keyless**: every row is auto-hashed with
  ``MD5(CONCAT_WS('||', col1, col2, ...))``; multiset arithmetic yields exact
  identical/added/deleted counts even in the presence of duplicate rows.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import duckdb

from .io import SourceError, load_source, quote_identifier, sql_literal
from .schema_diff import SchemaDiffResult, describe_relation, diff_schemas

__all__ = ["DiffResult", "DiffSummary", "DuckDiffer"]

_TEXTUAL_TOKENS: Tuple[str, ...] = ("VARCHAR", "TEXT", "STRING", "CHAR", "ENUM")

_ALIAS_A = "__dd_a"
_ALIAS_B = "__dd_b"


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------


@dataclass
class DiffSummary:
    """Aggregated outcome of a dataset diff."""

    total_rows_a: int = 0
    total_rows_b: int = 0
    identical_rows_count: int = 0
    modified_rows_count: int = 0
    added_rows_count: int = 0
    deleted_rows_count: int = 0
    column_drift_stats: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    sample_mismatches: List[List[Any]] = field(default_factory=list)

    @property
    def drift_detected(self) -> bool:
        """True when any row-level drift (modified/added/deleted) exists."""
        return bool(
            self.modified_rows_count or self.added_rows_count or self.deleted_rows_count
        ) or any(int(v.get("mismatches", 0)) > 0 for v in self.column_drift_stats.values())

    def changed_row_count(self) -> int:
        """Rows that are not byte-for-byte matches: modified + added + deleted."""
        return (
            self.modified_rows_count + self.added_rows_count + self.deleted_rows_count
        )

    def to_dict(self) -> dict:
        """JSON-serialisable representation."""
        return {
            "total_rows_a": self.total_rows_a,
            "total_rows_b": self.total_rows_b,
            "identical_rows_count": self.identical_rows_count,
            "modified_rows_count": self.modified_rows_count,
            "added_rows_count": self.added_rows_count,
            "deleted_rows_count": self.deleted_rows_count,
            "column_drift_stats": {
                k: dict(v) for k, v in self.column_drift_stats.items()
            },
            "sample_mismatches": [list(rec) for rec in self.sample_mismatches],
            "drift_detected": self.drift_detected,
        }


@dataclass
class DiffResult:
    """Everything reporters need: schema drift, summary, parameters, timing."""

    source: str
    target: str
    mode: str
    keys: Optional[List[str]]
    params: Dict[str, Any]
    schema_diff: SchemaDiffResult
    summary: DiffSummary
    warnings: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def drift_detected(self) -> bool:
        """True when row-level *or* schema-level drift was detected."""
        return bool(self.summary.drift_detected or self.schema_diff.has_schema_drift)

    @property
    def schema_drift(self) -> bool:
        """True when the schemas themselves drifted."""
        return bool(self.schema_diff.has_schema_drift)

    def to_dict(self) -> dict:
        """Full machine-readable document used by the JSON exporter."""
        return {
            "tool": "duck-diff",
            "mode": self.mode,
            "source": self.source,
            "target": self.target,
            "keys": list(self.keys) if self.keys else None,
            "params": dict(self.params),
            "schema": self.schema_diff.to_dict(),
            "summary": self.summary.to_dict(),
            # Top-level mirror of summary.sample_mismatches for CI ergonomics.
            "sample_mismatches": [list(rec) for rec in self.summary.sample_mismatches],
            "drift_detected": self.drift_detected,
            "schema_drift": self.schema_drift,
            "warnings": list(self.warnings),
            "duration_seconds": round(self.duration_seconds, 6),
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class DuckDiffer:
    """Constant-memory dataset differ running on an embedded DuckDB engine.

    Args:
        epsilon: Absolute tolerance for numeric comparisons (``<=`` is equal).
        ignore_case: Compare text columns case-insensitively (also applies to
            keyed joins and keyless hashing).
        sample_limit: Maximum number of sample mismatch records returned.
        memory_limit: Optional DuckDB memory budget, e.g. ``"2GB"``.
        threads: Optional DuckDB thread count.
        check_key_uniqueness: Warn when keyed datasets contain duplicate keys.
    """

    def __init__(
        self,
        epsilon: float = 0.0,
        ignore_case: bool = False,
        sample_limit: int = 20,
        memory_limit: Optional[str] = None,
        threads: Optional[int] = None,
        check_key_uniqueness: bool = True,
    ) -> None:
        eps = float(epsilon)
        if not math.isfinite(eps) or eps < 0.0:
            raise ValueError(f"epsilon must be a finite, non-negative float, got {epsilon!r}")
        self._epsilon = eps
        self._eps_literal = repr(eps)
        self._ignore_case = bool(ignore_case)
        self._sample_limit = max(int(sample_limit), 0)
        self._check_key_uniqueness = bool(check_key_uniqueness)

        config: Dict[str, Any] = {}
        if memory_limit is not None:
            config["memory_limit"] = str(memory_limit)
        if threads is not None:
            config["threads"] = int(threads)
        self._conn = (
            duckdb.connect(":memory:", config=config) if config else duckdb.connect(":memory:")
        )

    # -- lifecycle ---------------------------------------------------------

    @property
    def connection(self) -> "duckdb.DuckDBPyConnection":
        """The underlying DuckDB connection (for advanced queries)."""
        return self._conn

    def close(self) -> None:
        """Release the embedded DuckDB connection."""
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - closing twice must never raise
            pass

    def __enter__(self) -> "DuckDiffer":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - best effort cleanup
        try:
            self.close()
        except Exception:
            pass

    # -- public API ----------------------------------------------------------

    def diff(
        self,
        source_a: str,
        source_b: str,
        keys: Optional[Sequence[str]] = None,
    ) -> DiffResult:
        """Diff two datasets and return a :class:`DiffResult`.

        Args:
            source_a: Baseline path or ``sqlite://<db>#<table>`` URI.
            source_b: Candidate path or URI.
            keys: Key columns for keyed matching; ``None`` => keyless hashing.
        """
        start = time.perf_counter()
        loaded_a = load_source(self._conn, str(source_a), _ALIAS_A)
        loaded_b = load_source(self._conn, str(source_b), _ALIAS_B)
        warnings: List[str] = [*loaded_a.warnings, *loaded_b.warnings]

        schema = diff_schemas(self._conn, loaded_a.relation_sql, loaded_b.relation_sql)
        resolved_keys = self._resolve_keys(keys, schema) if keys else None

        if resolved_keys is not None:
            summary = self._diff_keyed(resolved_keys, schema, warnings)
        else:
            summary = self._diff_keyless(warnings)

        summary.total_rows_a = self._count_rows(_ALIAS_A)
        summary.total_rows_b = self._count_rows(_ALIAS_B)

        return DiffResult(
            source=loaded_a.description,
            target=loaded_b.description,
            mode="keyed" if resolved_keys else "keyless",
            keys=list(resolved_keys) if resolved_keys else None,
            params={
                "epsilon": self._epsilon,
                "ignore_case": self._ignore_case,
                "sample_limit": self._sample_limit,
            },
            schema_diff=schema,
            summary=summary,
            warnings=warnings,
            duration_seconds=time.perf_counter() - start,
        )

    # -- helpers -------------------------------------------------------------

    def _count_rows(self, alias: str) -> int:
        row = self._conn.execute(f"SELECT COUNT(*) FROM {quote_identifier(alias)}").fetchone()
        return int(row[0])

    def _resolve_keys(
        self, keys: Sequence[str], schema: SchemaDiffResult
    ) -> List[str]:
        available = {c.lower(): c for c, _, _ in schema.shared_columns}
        resolved: List[str] = []
        missing: List[str] = []
        for key in keys:
            name = str(key)
            canonical = available.get(name.strip().lower())
            if canonical is None:
                missing.append(name)
            elif canonical not in resolved:
                resolved.append(canonical)
        if missing:
            raise SourceError(
                f"Key column(s) {missing!r} not present on both sides of the diff. "
                f"Shared columns: {[c for c, _, _ in schema.shared_columns]!r}"
            )
        if not resolved:
            raise SourceError("At least one usable key column is required for a keyed diff.")
        return resolved

    @staticmethod
    def _is_textual(type_str: str) -> bool:
        upper = type_str.upper()
        return any(token in upper for token in _TEXTUAL_TOKENS)

    def _maybe_lower(self, ref: str, type_str: str) -> str:
        if self._ignore_case and self._is_textual(type_str):
            return f"LOWER({ref})"
        return ref

    def _qualified(self, alias: str, column: str) -> str:
        return f"{alias}.{quote_identifier(column)}"

    def _equality_expr(self, ref_a: str, type_a: str, ref_b: str, type_b: str) -> str:
        """NULL-safe equality expression; optionally epsilon-tolerant numerics."""
        left = self._maybe_lower(ref_a, type_a)
        right = self._maybe_lower(ref_b, type_b)
        base = f"({left} IS NOT DISTINCT FROM {right})"
        if self._epsilon > 0.0:
            cast_l = f"TRY_CAST({left} AS DOUBLE)"
            cast_r = f"TRY_CAST({right} AS DOUBLE)"
            return (
                f"{base} OR ({cast_l} IS NOT NULL AND {cast_r} IS NOT NULL "
                f"AND ABS({cast_l} - {cast_r}) <= {self._eps_literal})"
            )
        return base

    def _join_predicate(self, ref_a: str, type_a: str, ref_b: str, type_b: str) -> str:
        """Strict NULL-safe equality used for keyed joins (never epsilon-tolerant)."""
        left = self._maybe_lower(ref_a, type_a)
        right = self._maybe_lower(ref_b, type_b)
        return f"({left} IS NOT DISTINCT FROM {right})"

    # -- keyed diff -----------------------------------------------------------

    def _diff_keyed(
        self,
        keys: List[str],
        schema: SchemaDiffResult,
        warnings: List[str],
    ) -> DiffSummary:
        qa, qb = f'"{_ALIAS_A}"', f'"{_ALIAS_B}"'
        shared_map: Dict[str, Tuple[str, str]] = {
            c: (ta, tb) for c, ta, tb in schema.shared_columns
        }
        keys_lower = {k.lower() for k in keys}
        nonkeys = [c for c in shared_map if c.lower() not in keys_lower]

        if self._check_key_uniqueness:
            key_list_sql = ", ".join(quote_identifier(k) for k in keys)
            for rel, label in ((qa, "source"), (qb, "target")):
                dup_groups = int(
                    self._conn.execute(
                        f"SELECT COUNT(*) FROM (SELECT {key_list_sql} FROM {rel} "
                        f"GROUP BY {key_list_sql} HAVING COUNT(*) > 1)"
                    ).fetchone()[0]
                )
                if dup_groups:
                    warnings.append(
                        f"Key {keys!r} contains {dup_groups} duplicate group(s) in the "
                        f"{label} dataset; duplicate-key multiplicity inflates matched counts."
                    )

        # Strict null-safe FULL OUTER JOIN on the key tuple (no epsilon on keys).
        join_pred = " AND ".join(
            self._join_predicate(
                self._qualified("ea", k), shared_map[k][0],
                self._qualified("eb", k), shared_map[k][1],
            )
            for k in keys
        )
        proj: List[str] = ["ea.__dd_rid AS rid_a", "eb.__dd_rid AS rid_b"]
        for j, k in enumerate(keys):
            proj.append(f"ea.{quote_identifier(k)} AS ak{j}")
            proj.append(f"eb.{quote_identifier(k)} AS bk{j}")
        for i, c in enumerate(nonkeys):
            proj.append(f"ea.{quote_identifier(c)} AS a{i}")
            proj.append(f"eb.{quote_identifier(c)} AS b{i}")

        cte = (
            "WITH ea AS (SELECT ROW_NUMBER() OVER () AS __dd_rid, * FROM " + qa + "),\n"
            "     eb AS (SELECT ROW_NUMBER() OVER () AS __dd_rid, * FROM " + qb + "),\n"
            "j AS (SELECT\n  " + ",\n  ".join(proj)
            + "\n      FROM ea FULL OUTER JOIN eb ON " + join_pred + "\n)"
        )

        matched = "rid_a IS NOT NULL AND rid_b IS NOT NULL"
        eqs = [
            self._equality_expr(f'"a{i}"', shared_map[c][0], f'"b{i}"', shared_map[c][1])
            for i, c in enumerate(nonkeys)
        ]
        drift_any = " OR ".join(f"(NOT ({e}))" for e in eqs)

        select_list = [
            "COUNT(*) FILTER (WHERE rid_a IS NULL) AS added",
            "COUNT(*) FILTER (WHERE rid_b IS NULL) AS deleted",
            f"COUNT(*) FILTER (WHERE {matched}) AS matched",
        ]
        select_list.append(
            f"COUNT(*) FILTER (WHERE {matched} AND ({drift_any})) AS modified"
            if drift_any
            else "0 AS modified"
        )
        for i, e in enumerate(eqs):
            select_list.append(
                f"COALESCE(SUM(CASE WHEN {matched} AND NOT ({e}) THEN 1 ELSE 0 END), 0) AS d{i}"
            )

        stats_row = self._conn.execute(
            cte + "\nSELECT\n  " + ",\n  ".join(select_list) + "\nFROM j"
        ).fetchone()
        added = int(stats_row[0])
        deleted = int(stats_row[1])
        matched_n = int(stats_row[2])
        modified = int(stats_row[3])
        drift_counts = [int(stats_row[4 + i]) for i in range(len(eqs))]

        column_stats: Dict[str, Dict[str, Any]] = {}
        for i, c in enumerate(nonkeys):
            mismatches = drift_counts[i]
            pct = round(100.0 * mismatches / matched_n, 4) if matched_n else 0.0
            column_stats[c] = {"mismatches": mismatches, "drift_pct": pct}

        samples: List[List[Any]] = []
        if nonkeys and self._sample_limit > 0:
            key_expr = self._sample_key_expr(len(keys))
            parts = [
                (
                    f"SELECT {key_expr} AS key, {sql_literal(c)} AS column_name, "
                    f'CAST("a{i}" AS VARCHAR) AS val_a, CAST("b{i}" AS VARCHAR) AS val_b '
                    f"FROM j WHERE {matched} AND (NOT ({eqs[i]}))"
                )
                for i, c in enumerate(nonkeys)
            ]
            samples_sql = (
                cte
                + "\nSELECT * FROM (\n  "
                + "\n  UNION ALL\n  ".join(parts)
                + f"\n) ORDER BY column_name, key LIMIT {int(self._sample_limit)}"
            )
            samples = [
                [rec[0], rec[1], rec[2], rec[3]]
                for rec in self._conn.execute(samples_sql).fetchall()
            ]

        return DiffSummary(
            identical_rows_count=matched_n - modified,
            modified_rows_count=modified,
            added_rows_count=added,
            deleted_rows_count=deleted,
            column_drift_stats=column_stats,
            sample_mismatches=samples,
        )

    def _sample_key_expr(self, n_keys: int) -> str:
        parts = [
            f"COALESCE(CAST(\"ak{j}\" AS VARCHAR), 'NULL')" for j in range(n_keys)
        ]
        if len(parts) == 1:
            return parts[0]
        return "CONCAT_WS(', ', " + ", ".join(parts) + ")"

    # -- keyless diff ----------------------------------------------------------

    def _concat_expr(self, cols: Sequence[Tuple[str, str]], alias: str) -> str:
        args = ", ".join(
            "COALESCE(CAST(" + self._maybe_lower(self._qualified(alias, n), t) +
            " AS VARCHAR), '__NULL__')"
            for n, t in cols
        )
        return f"CONCAT_WS('||', {args})"

    def _diff_keyless(self, warnings: List[str]) -> DiffSummary:
        qa, qb = f'"{_ALIAS_A}"', f'"{_ALIAS_B}"'
        cols_a = describe_relation(self._conn, qa)
        cols_b = describe_relation(self._conn, qb)
        if not cols_a or not cols_b:
            raise SourceError("Keyless diff requires at least one column on each side.")

        hash_a, hash_b = self._concat_expr(cols_a, qa), self._concat_expr(cols_b, qb)
        counts_sql = (
            f"WITH ha AS (SELECT MD5({hash_a}) AS h FROM {qa}),\n"
            f"     hb AS (SELECT MD5({hash_b}) AS h FROM {qb}),\n"
            "m AS (SELECT COALESCE(x.h, y.h) AS h, COALESCE(x.ca, 0) AS ca, "
            "COALESCE(y.cb, 0) AS cb "
            "FROM (SELECT h, COUNT(*) AS ca FROM ha GROUP BY h) x "
            "FULL OUTER JOIN (SELECT h, COUNT(*) AS cb FROM hb GROUP BY h) y "
            "ON x.h = y.h)\n"
            "SELECT COALESCE(SUM(LEAST(ca, cb)), 0), "
            "COALESCE(SUM(GREATEST(cb - ca, 0)), 0), "
            "COALESCE(SUM(GREATEST(ca - cb, 0)), 0) FROM m"
        )
        row = self._conn.execute(counts_sql).fetchone()
        identical, added, deleted = int(row[0]), int(row[1]), int(row[2])

        samples: List[List[Any]] = []
        if self._sample_limit > 0:
            limit = int(self._sample_limit)
            repr_a = self._concat_expr(cols_a, qa)
            repr_b = self._concat_expr(cols_b, qb)
            added_sql = (
                f"WITH ha AS (SELECT MD5({hash_a}) AS h FROM {qa}),\n"
                f"     hb AS (SELECT MD5({hash_b}) AS h, {repr_b} AS repr FROM {qb})\n"
                "SELECT hb.h, hb.repr FROM hb LEFT JOIN ha ON hb.h = ha.h "
                f"WHERE ha.h IS NULL LIMIT {limit}"
            )
            deleted_sql = (
                f"WITH ha AS (SELECT MD5({hash_a}) AS h, {repr_a} AS repr FROM {qa}),\n"
                f"     hb AS (SELECT MD5({hash_b}) AS h FROM {qb})\n"
                "SELECT ha.h, ha.repr FROM ha LEFT JOIN hb ON ha.h = hb.h "
                f"WHERE hb.h IS NULL LIMIT {limit}"
            )
            for record in self._conn.execute(added_sql).fetchall():
                samples.append([str(record[0])[:12], "__added_row__", None, record[1]])
            for record in self._conn.execute(deleted_sql).fetchall():
                samples.append([str(record[0])[:12], "__deleted_row__", record[1], None])
            samples = samples[:limit]

        return DiffSummary(
            identical_rows_count=identical,
            modified_rows_count=0,
            added_rows_count=added,
            deleted_rows_count=deleted,
            column_drift_stats={},
            sample_mismatches=samples,
        )
