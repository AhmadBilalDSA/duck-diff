"""GitHub Action runtime for ``duck-diff`` (invoked as ``python -m duck_diff.action``).

Reads standard ``INPUT_*`` environment variables populated by ``action.yml``,
runs the diff, writes GitHub Action outputs (``GITHUB_OUTPUT``) and returns a
strict CI gate exit code:

* ``0`` — no drift (or drift allowed because ``fail_on_drift=false``).
* ``1`` — drift detected while ``fail_on_drift=true``.
* ``2`` — misconfiguration or runtime failure (missing inputs, unreadable data).
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Mapping, Optional, Tuple

from .cli import EXIT_DRIFT, EXIT_ERROR, emit_stdout, execute_diff
from .reporter import to_markdown

__all__ = ["main", "run_action"]

_TRUE_VALUES = {"1", "true", "yes", "y", "on"}

_INPUT_NAMES = ("source_path", "target_path", "key_columns", "epsilon", "fail_on_drift")

_FLAG_BY_ARG = {
    "--source-path": "source_path",
    "--target-path": "target_path",
    "--key-columns": "key_columns",
    "--epsilon": "epsilon",
    "--fail-on-drift": "fail_on_drift",
}


def _env_value(environ: Mapping[str, str], name: str) -> Optional[str]:
    return environ.get("INPUT_" + name.upper())


def _as_bool(raw: Optional[str], default: bool = True) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in _TRUE_VALUES


def _write_github_outputs(outputs: Mapping[str, str], environ: Mapping[str, str]) -> None:
    path = environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            for name, value in outputs.items():
                handle.write(f"{name}={value}\n")
    except OSError as exc:
        print(f"duck-diff action: cannot write GITHUB_OUTPUT: {exc}", file=sys.stderr)


def run_action(
    inputs: Optional[Mapping[str, str]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> Tuple[int, Dict[str, str]]:
    """Execute the action logic programmatically.

    Args:
        inputs: Explicit input overrides (same names as ``action.yml`` inputs).
        environ: Environment mapping; defaults to :data:`os.environ`.

    Returns:
        ``(exit_code, outputs_dict)`` where ``outputs_dict`` mirrors the
        GitHub Action outputs ``drift_detected``, ``modified_rows`` and
        ``schema_drift`` (empty when inputs were invalid).
    """
    env = dict(os.environ) if environ is None else dict(environ)
    merged: Dict[str, Optional[str]] = {name: _env_value(env, name) for name in _INPUT_NAMES}
    if inputs:
        for name, value in inputs.items():
            merged[str(name).lower()] = str(value)

    missing = [name for name in ("source_path", "target_path") if not merged.get(name)]
    if missing:
        print(
            f"duck-diff action: missing required input(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        return EXIT_ERROR, {}

    keys_raw = merged.get("key_columns") or ""
    keys = [part.strip() for part in str(keys_raw).split(",") if part.strip()] or None

    epsilon_raw = merged.get("epsilon") or "0"
    try:
        epsilon = float(epsilon_raw)
    except (TypeError, ValueError):
        print(
            f"duck-diff action: invalid epsilon {epsilon_raw!r}; expected a number.",
            file=sys.stderr,
        )
        return EXIT_ERROR, {}

    fail_on_drift = _as_bool(merged.get("fail_on_drift"), default=True)

    try:
        result = execute_diff(
            str(merged["source_path"]),
            str(merged["target_path"]),
            keys=keys,
            epsilon=epsilon,
            sample_limit=20,
        )
    except Exception as exc:  # noqa: BLE001 - surface infra failures as step failures
        print(
            f"duck-diff action: diff failed: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        failure_outputs = {"drift_detected": "false", "modified_rows": "0", "schema_drift": "false"}
        _write_github_outputs(failure_outputs, env)
        return EXIT_ERROR, failure_outputs

    outputs: Dict[str, str] = {
        "drift_detected": str(result.drift_detected).lower(),
        "modified_rows": str(result.summary.modified_rows_count),
        "schema_drift": str(result.schema_diff.has_schema_drift).lower(),
    }
    _write_github_outputs(outputs, env)
    emit_stdout(to_markdown(result))

    exit_code = EXIT_DRIFT if (fail_on_drift and result.drift_detected) else 0
    return exit_code, outputs


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point supporting explicit ``--input value`` args on top of INPUT_* env vars."""
    overrides: Dict[str, str] = {}
    tokens = list(sys.argv[1:] if argv is None else argv)
    iterator = iter(tokens)
    for token in iterator:
        name = _FLAG_BY_ARG.get(token)
        if name is None:
            print(f"duck-diff action: unknown argument {token!r}", file=sys.stderr)
            return EXIT_ERROR
        try:
            overrides[name] = next(iterator)
        except StopIteration:
            print(f"duck-diff action: {token} requires a value", file=sys.stderr)
            return EXIT_ERROR
    exit_code, _ = run_action(inputs=overrides or None)
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
