"""Locate the Epic Games Launcher's local installed-item manifest store."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import PureWindowsPath

from vigil_overlay.contracts.games import GameDiscoveryContext
from vigil_overlay.providers.epic.filesystem import EpicFileSystem

ManifestRootSupplier = Callable[[], tuple[str, ...]]


class EpicManifestInstallationLocator:
    """Resolve the local launcher manifest directory without reading launcher databases."""

    def __init__(
        self,
        filesystem: EpicFileSystem,
        *,
        candidate_roots: ManifestRootSupplier | None = None,
    ) -> None:
        self._filesystem = filesystem
        self._candidate_roots = candidate_roots or _default_manifest_roots

    def locate(self, context: GameDiscoveryContext) -> str | None:
        candidates: list[str] = []
        override = os.environ.get("VIGIL_EPIC_MANIFEST_ROOT")
        if override:
            candidates.append(override)
        candidates.extend(self._candidate_roots())

        seen: set[str] = set()
        for candidate in candidates:
            normalized = str(PureWindowsPath(candidate))
            key = normalized.casefold()
            if not normalized or key in seen:
                continue
            seen.add(key)
            try:
                if self._filesystem.is_dir(normalized, context):
                    return normalized
            except (OSError, TimeoutError):
                continue
        return None


def _default_manifest_roots() -> tuple[str, ...]:
    program_data = os.environ.get("PROGRAMDATA")
    roots: list[str] = []
    if program_data:
        roots.append(
            str(
                PureWindowsPath(program_data)
                / "Epic"
                / "EpicGamesLauncher"
                / "Data"
                / "Manifests"
            )
        )
    roots.append(r"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests")
    return tuple(roots)


__all__ = ["EpicManifestInstallationLocator"]
