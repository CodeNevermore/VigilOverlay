"""Locate local XboxGames roots used by modern Xbox / Microsoft Store PC packages."""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable
from pathlib import PureWindowsPath
from typing import Any

from vigil_overlay.contracts.games import GameDiscoveryContext
from vigil_overlay.providers.xbox.filesystem import XboxFileSystem

XboxGamesRootSupplier = Callable[[], tuple[str, ...]]

_DRIVE_REMOVABLE = 2
_DRIVE_FIXED = 3


class XboxGamesInstallationLocator:
    """Resolve accessible local ``XboxGames`` roots without launcher/database access."""

    def __init__(
        self,
        filesystem: XboxFileSystem,
        *,
        candidate_roots: XboxGamesRootSupplier | None = None,
    ) -> None:
        self._filesystem = filesystem
        self._candidate_roots = candidate_roots or _windows_xbox_games_roots

    def locate_roots(self, context: GameDiscoveryContext) -> tuple[str, ...]:
        roots: list[str] = []
        seen: set[str] = set()
        for candidate in self._candidate_roots():
            if context.is_cancelled():
                break
            normalized = str(PureWindowsPath(candidate))
            key = normalized.casefold()
            if key in seen or not self._filesystem.is_dir(normalized, context):
                continue
            seen.add(key)
            roots.append(normalized)
        return tuple(roots)


def _windows_xbox_games_roots() -> tuple[str, ...]:
    if sys.platform != "win32":
        return ()

    windll: Any = getattr(ctypes, "windll", None)
    if windll is None:
        return ()
    kernel32 = windll.kernel32
    size = kernel32.GetLogicalDriveStringsW(0, None)
    if not size:
        return ()
    buffer = ctypes.create_unicode_buffer(size)
    if not kernel32.GetLogicalDriveStringsW(size, buffer):
        return ()

    roots: list[str] = []
    for drive in buffer[:].split("\x00"):
        if not drive:
            continue
        drive_type = kernel32.GetDriveTypeW(drive)
        if drive_type not in {_DRIVE_REMOVABLE, _DRIVE_FIXED}:
            continue
        roots.append(str(PureWindowsPath(drive, "XboxGames")))
    return tuple(roots)


__all__ = ["XboxGamesInstallationLocator"]
