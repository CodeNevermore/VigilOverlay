"""Canonical runtime identity helpers for source and packaged Vigil executions."""

from __future__ import annotations

import sys
from pathlib import Path


def is_packaged_build() -> bool:
    """Return whether this module is running from a compiled/frozen application.

    Nuitka exposes ``__compiled__`` as a module attribute rather than setting
    ``sys.frozen``. Keep the legacy frozen check as a compatibility fallback for
    alternate packagers without coupling packaged identity to logging mode.
    """

    return globals().get("__compiled__") is not None or bool(
        getattr(sys, "frozen", False)
    )


def _is_windows() -> bool:
    return sys.platform == "win32"


def packaged_executable_path() -> Path | None:
    """Return the real packaged Windows executable, never a source interpreter path."""

    if not _is_windows() or not is_packaged_build():
        return None
    candidates = (sys.argv[0] if sys.argv else "", sys.executable)
    for raw_candidate in candidates:
        if not raw_candidate:
            continue
        candidate = Path(raw_candidate).expanduser().resolve()
        executable_name = candidate.name.casefold()
        is_source_interpreter = executable_name in {
            "py.exe",
            "python.exe",
            "pythonw.exe",
        } or executable_name.startswith(("python3.", "pythonw3."))
        if (
            candidate.suffix.casefold() == ".exe"
            and candidate.is_file()
            and not is_source_interpreter
        ):
            return candidate
    return None
