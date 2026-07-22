"""Steam installation discovery through environment, registry, and standard fallback."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

from vigil_overlay.contracts.games import GameDiscoveryContext
from vigil_overlay.providers.steam.filesystem import SteamFileSystem

RegistryRoots = Callable[[], tuple[str, ...]]


class SteamInstallationLocator:
    """Locate the Steam client root without assuming the standard install path first."""

    def __init__(
        self,
        filesystem: SteamFileSystem,
        *,
        registry_roots: RegistryRoots | None = None,
    ) -> None:
        self._filesystem = filesystem
        self._registry_roots = registry_roots or _windows_registry_roots

    def locate(self, context: GameDiscoveryContext) -> str | None:
        candidates: list[str] = []
        override = os.environ.get("VIGIL_STEAM_ROOT")
        if override:
            candidates.append(override)
        candidates.extend(self._registry_roots())
        candidates.append(r"C:\Program Files (x86)\Steam")

        seen: set[str] = set()
        for candidate in candidates:
            normalized = candidate.rstrip("\\/")
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


def _windows_registry_roots() -> tuple[str, ...]:
    if sys.platform != "win32":
        return ()
    try:
        import winreg
    except ImportError:
        return ()

    values: list[str] = []
    locations = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam", "InstallPath"),
    )
    for hive, key_path, value_name in locations:
        try:
            with winreg.OpenKey(hive, key_path) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue
        if isinstance(value, str) and value.strip():
            values.append(value.strip().replace("/", "\\"))
    return tuple(values)


__all__ = ["SteamInstallationLocator"]
