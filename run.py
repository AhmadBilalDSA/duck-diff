"""Top-level launcher for duck-diff.

Prevents relative import failures during PyInstaller builds and
provides a single entry point for ``python run.py`` execution.
"""

from duck_diff.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
