"""duck_diff — fast, constant-memory data diffing powered by an embedded DuckDB engine.

Public API::

    from duck_diff import DuckDiffer, diff

    result = diff("baseline.parquet", "candidate.parquet", keys=["id"])
    print(result.summary.modified_rows_count)
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from .engine import DiffResult, DiffSummary, DuckDiffer
from .io import SourceError
from .schema_diff import SchemaDiffResult

__version__ = "1.1.0"

__all__ = [
    "DuckDiffer",
    "DiffResult",
    "DiffSummary",
    "SchemaDiffResult",
    "SourceError",
    "diff",
    "__version__",
]


def diff(
    source: str,
    target: str,
    keys: Optional[Sequence[str]] = None,
    *,
    epsilon: float = 0.0,
    ignore_case: bool = False,
    sample_limit: int = 20,
) -> DiffResult:
    """One-shot convenience wrapper around :class:`DuckDiffer`.

    Args:
        source: Baseline dataset path or ``sqlite://<db>#<table>`` URI.
        target: Candidate dataset to compare against ``source``.
        keys: Primary-key columns; ``None`` selects keyless whole-row hashing.
        epsilon: Absolute float tolerance for numeric comparisons.
        ignore_case: Compare text columns case-insensitively.
        sample_limit: Maximum number of sample mismatch records to collect.

    Returns:
        A fully populated :class:`DiffResult`.
    """
    differ = DuckDiffer(
        epsilon=epsilon,
        ignore_case=ignore_case,
        sample_limit=sample_limit,
    )
    try:
        return differ.diff(source, target, keys=keys)
    finally:
        differ.close()
