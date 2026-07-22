"""Canonical Vigil application and system-tray icon loading."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QIcon

from vigil_overlay.core.paths import ApplicationPaths

APPLICATION_ICON_RELATIVE_PATH = Path("icons") / "vigil_overlay.ico"


def application_icon_path(paths: ApplicationPaths) -> Path:
    """Return the packaged/source path for Vigil's canonical application icon."""

    return paths.resource_root / APPLICATION_ICON_RELATIVE_PATH


def load_application_icon(paths: ApplicationPaths) -> QIcon:
    """Load the canonical icon without making a missing asset fatal to startup."""

    return QIcon(str(application_icon_path(paths)))
