"""Pytest configuration for duck_diff.

Two jobs:

1. Make the project root importable regardless of where pytest was launched.
2. Stay tolerant of sandboxed CI environments that deny
   ``os.rmdir``/``shutil.rmtree`` while still allowing directory creation.
   pytest's stock ``tmp_path`` factory wipes its base directory between
   sessions, which fails hard there, so this conftest provides a drop-in
   ``tmp_path`` replacement that hands every test a unique freshly-created
   directory and never deletes anything at teardown (leftovers are
   git-ignored). pytest's dead-symlink sweep over temp roots is disabled too.
"""

from __future__ import annotations

import pathlib
import sys
import uuid

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TMP_BASE = _ROOT / "_pytest_tmp"


@pytest.fixture
def tmp_path() -> pathlib.Path:
    """Unique per-test temporary directory inside the project tree."""
    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    directory = _TMP_BASE / f"t_{uuid.uuid4().hex[:16]}"
    directory.mkdir()
    return directory


try:  # pragma: no cover - environment-dependent safety net
    import _pytest.tmpdir as _tmpdir_plugin

    _tmpdir_plugin.cleanup_dead_symlinks = lambda *args, **kwargs: None  # noqa: E731
except Exception:  # noqa: BLE001 - other pytest layouts are fine without it
    pass
