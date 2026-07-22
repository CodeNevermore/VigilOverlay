"""Filesystem boundary for native Xbox / Microsoft Store game discovery."""

from __future__ import annotations

import glob as glob_module
from pathlib import Path
from typing import Protocol

from vigil_overlay.contracts.games import GameDiscoveryContext


class XboxFileSystem(Protocol):
    """Minimal read-only filesystem operations used by the Xbox provider."""

    def read_text(self, path: str, context: GameDiscoveryContext) -> str: ...

    def is_dir(self, path: str, context: GameDiscoveryContext) -> bool: ...

    def is_file(self, path: str, context: GameDiscoveryContext) -> bool: ...

    def glob(self, pattern: str, context: GameDiscoveryContext) -> tuple[str, ...]: ...


class LocalXboxFileSystem:
    """Read local Xbox game files without modifying package or launcher state."""

    def read_text(self, path: str, context: GameDiscoveryContext) -> str:
        if context.is_cancelled():
            raise TimeoutError("Xbox game discovery was cancelled")
        candidate = Path(path)
        try:
            return candidate.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            return candidate.read_text(encoding="utf-16")

    def is_dir(self, path: str, context: GameDiscoveryContext) -> bool:
        if context.is_cancelled():
            return False
        return Path(path).is_dir()

    def is_file(self, path: str, context: GameDiscoveryContext) -> bool:
        if context.is_cancelled():
            return False
        return Path(path).is_file()

    def glob(self, pattern: str, context: GameDiscoveryContext) -> tuple[str, ...]:
        if context.is_cancelled():
            return ()
        return tuple(sorted(glob_module.glob(pattern)))


__all__ = ["LocalXboxFileSystem", "XboxFileSystem"]
