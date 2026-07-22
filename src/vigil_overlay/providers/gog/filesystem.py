"""Filesystem boundary for validating GOG installation records."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from vigil_overlay.contracts.games import GameDiscoveryContext


class GOGFileSystem(Protocol):
    """Filesystem checks used by GOG discovery."""

    def is_dir(self, path: str, context: GameDiscoveryContext) -> bool: ...

    def is_file(self, path: str, context: GameDiscoveryContext) -> bool: ...

    def game_info_files(
        self,
        install_directory: str,
        context: GameDiscoveryContext,
    ) -> tuple[str, ...]: ...

    def read_text(
        self,
        path: str,
        context: GameDiscoveryContext,
        *,
        max_bytes: int,
    ) -> str: ...


class LocalGOGFileSystem:
    """Validate GOG installation paths without modifying local files."""

    def is_dir(self, path: str, context: GameDiscoveryContext) -> bool:
        return not context.is_cancelled() and Path(path).is_dir()

    def is_file(self, path: str, context: GameDiscoveryContext) -> bool:
        return not context.is_cancelled() and Path(path).is_file()

    def game_info_files(
        self,
        install_directory: str,
        context: GameDiscoveryContext,
    ) -> tuple[str, ...]:
        if context.is_cancelled():
            return ()
        root = Path(install_directory)
        try:
            return tuple(
                str(path)
                for path in sorted(
                    root.glob("goggame-*.info"), key=lambda item: item.name
                )
                if path.is_file()
            )
        except OSError:
            return ()

    def read_text(
        self,
        path: str,
        context: GameDiscoveryContext,
        *,
        max_bytes: int,
    ) -> str:
        if context.is_cancelled():
            raise TimeoutError("GOG metadata read was cancelled")
        source = Path(path)
        if source.stat().st_size > max_bytes:
            raise ValueError(f"GOG metadata exceeds {max_bytes} bytes")
        return source.read_text(encoding="utf-8-sig")


__all__ = ["GOGFileSystem", "LocalGOGFileSystem"]
