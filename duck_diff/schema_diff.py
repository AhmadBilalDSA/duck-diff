"""Schema inspection & drift detection backed by ``DESCRIBE SELECT ... LIMIT 0``.

The schema probe is metadata-only: DuckDB never reads data rows, keeping the
check O(1) in dataset size.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

__all__ = ["SchemaDiffResult", "describe_relation", "diff_schemas"]


@dataclass
class SchemaDiffResult:
    """Result of comparing two dataset schemas.

    Attributes:
        shared_columns: ``(column_name, type_in_a, type_in_b)`` triples present
            on both sides, ordered by their position in the source dataset.
        only_in_a: Column names that exist only in the source dataset.
        only_in_b: Column names that exist only in the target dataset.
        type_mismatches: Human-readable strings of the form
            ``"<column>: <TYPE_A> != <TYPE_B>"`` for shared columns whose
            normalized types differ.
    """

    shared_columns: List[Tuple[str, str, str]] = field(default_factory=list)
    only_in_a: List[str] = field(default_factory=list)
    only_in_b: List[str] = field(default_factory=list)
    type_mismatches: List[str] = field(default_factory=list)

    @property
    def has_schema_drift(self) -> bool:
        """True when columns were added/removed or any shared type drifted."""
        return bool(self.only_in_a or self.only_in_b or self.type_mismatches)

    def to_dict(self) -> dict:
        """JSON-serialisable representation."""
        return {
            "shared_columns": [[c, ta, tb] for c, ta, tb in self.shared_columns],
            "only_in_a": list(self.only_in_a),
            "only_in_b": list(self.only_in_b),
            "type_mismatches": list(self.type_mismatches),
            "has_schema_drift": self.has_schema_drift,
        }


def describe_relation(conn, relation: str) -> List[Tuple[str, str]]:
    """Return ``[(column_name, column_type), ...]`` for *relation*.

    Uses ``DESCRIBE SELECT * FROM ... LIMIT 0`` so no data pages are touched.
    """
    rows = conn.execute(f"DESCRIBE SELECT * FROM {relation} LIMIT 0").fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def _index_columns(columns: List[Tuple[str, str]]) -> "Dict[str, Tuple[str, str]]":
    """Index by lower-cased name, preserving order and original spelling."""
    index: Dict[str, Tuple[str, str]] = {}
    for name, ctype in columns:
        key = name.lower()
        if key not in index:  # first spelling wins
            index[key] = (name, ctype)
    return index


def diff_schemas(conn, relation_a: str, relation_b: str) -> SchemaDiffResult:
    """Compare the schemas of two registered relations.

    Column-name matching is case-insensitive (DuckDB identifiers fold case);
    reported names keep their source-side spelling. Type comparison is done on
    normalised (upper-cased) type strings.
    """
    cols_a = describe_relation(conn, relation_a)
    cols_b = describe_relation(conn, relation_b)

    idx_a = _index_columns(cols_a)
    idx_b = _index_columns(cols_b)

    shared_keys = [k for k in idx_a if k in idx_b]
    result = SchemaDiffResult()
    for key in shared_keys:
        name_a, type_a = idx_a[key]
        _, type_b = idx_b[key]
        result.shared_columns.append((name_a, type_a, type_b))
        norm_a = " ".join(type_a.upper().split())
        norm_b = " ".join(type_b.upper().split())
        if norm_a != norm_b:
            result.type_mismatches.append(f"{name_a}: {type_a} != {type_b}")

    result.only_in_a = [idx_a[k][0] for k in idx_a if k not in idx_b]
    result.only_in_b = [idx_b[k][0] for k in idx_b if k not in idx_a]
    return result
