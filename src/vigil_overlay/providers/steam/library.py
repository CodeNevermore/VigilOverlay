"""Read Steam libraryfolders.vdf and validate discovered library roots."""

from __future__ import annotations

from pathlib import PureWindowsPath

from vigil_overlay.contracts.games import GameDiscoveryContext
from vigil_overlay.providers.steam.filesystem import SteamFileSystem
from vigil_overlay.providers.steam.vdf import (
    ValveKeyValuesParser,
    VdfObject,
    child_object,
    string_value,
)


class SteamLibraryReader:
    """Read and validate Steam library roots from local configuration."""

    def __init__(
        self,
        filesystem: SteamFileSystem,
        parser: ValveKeyValuesParser | None = None,
    ) -> None:
        self._filesystem = filesystem
        self._parser = parser or ValveKeyValuesParser()

    def read_libraries(
        self,
        steam_root: str,
        context: GameDiscoveryContext,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        candidates = [steam_root]
        warnings: list[str] = []
        config_path = str(PureWindowsPath(steam_root) / "config" / "libraryfolders.vdf")
        try:
            text = self._filesystem.read_text(config_path, context)
            parsed = self._parser.parse(text)
            library_root = child_object(parsed, "libraryfolders") or parsed
            candidates.extend(_library_paths(library_root))
        except FileNotFoundError:
            pass
        except (OSError, TimeoutError, ValueError) as exc:
            warnings.append(f"Steam library list could not be fully read: {exc}")

        libraries: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if context.is_cancelled():
                warnings.append("Steam library discovery was cancelled.")
                break
            normalized = candidate.rstrip("\\/")
            key = normalized.casefold()
            if not normalized or key in seen:
                continue
            seen.add(key)
            steamapps = str(PureWindowsPath(normalized) / "steamapps")
            try:
                if self._filesystem.is_dir(steamapps, context):
                    libraries.append(normalized)
            except (OSError, TimeoutError) as exc:
                warnings.append(f"Steam library {normalized!r} was skipped: {exc}")
        return tuple(libraries), tuple(warnings)


def _library_paths(root: VdfObject) -> tuple[str, ...]:
    paths: list[str] = []
    for key, value in root.items():
        if not key.isdigit():
            continue
        candidate = value if isinstance(value, str) else string_value(value, "path")
        if candidate:
            paths.append(candidate.replace("/", "\\"))
    return tuple(paths)


__all__ = ["SteamLibraryReader"]
