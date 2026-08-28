"""Test-suite conftest — PYTEST_DEBUG_TEMPROOT isolation for Windows.

Sets the environment variable **before** pytest is imported so that
pytest's internal temp-root lives inside the project tree instead of
the system %TEMP% directory, preventing PermissionError [WinError 5]
on locked-down CI machines.
"""

from __future__ import annotations

import os

os.environ["PYTEST_DEBUG_TEMPROOT"] = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), ".pytest_tmp"
)
