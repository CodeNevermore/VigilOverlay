"""Read-only access to GOG-owned Windows installation records."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Protocol

from vigil_overlay.contracts.games import GameDiscoveryContext

_GOG_GAMES_KEY = r"Software\GOG.com\Games"


@dataclass(frozen=True, slots=True)
class GOGRegistryEntry:
    """One GOG installation record with a stable source identity."""

    source_id: str
    values: dict[str, object]


class GOGRegistry(Protocol):
    """Read-only source of GOG installation records."""

    def entries(
        self, context: GameDiscoveryContext
    ) -> tuple[GOGRegistryEntry, ...]: ...


class LocalGOGRegistry:
    """Enumerate per-game records written by GOG installers and GOG GALAXY."""

    def entries(self, context: GameDiscoveryContext) -> tuple[GOGRegistryEntry, ...]:
        if context.is_cancelled() or sys.platform != "win32":
            return ()
        try:
            import winreg
        except ImportError:
            return ()

        locations = (
            ("hklm64", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
            ("hklm32", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
            ("hkcu64", winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_64KEY),
            ("hkcu32", winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_32KEY),
        )
        discovered: list[GOGRegistryEntry] = []
        seen: set[str] = set()
        for label, hive, view in locations:
            if context.is_cancelled():
                break
            try:
                with winreg.OpenKey(
                    hive,
                    _GOG_GAMES_KEY,
                    0,
                    winreg.KEY_READ | view,
                ) as root:
                    subkey_count = winreg.QueryInfoKey(root)[0]
                    for index in range(subkey_count):
                        if context.is_cancelled():
                            break
                        try:
                            subkey_name = winreg.EnumKey(root, index)
                            with winreg.OpenKey(root, subkey_name) as subkey:
                                values = _read_registry_values(subkey, winreg)
                        except OSError:
                            continue
                        source_id = f"{label}:{subkey_name}"
                        dedupe_key = source_id.casefold()
                        if dedupe_key in seen:
                            continue
                        seen.add(dedupe_key)
                        discovered.append(GOGRegistryEntry(source_id, values))
            except OSError:
                continue
        return tuple(discovered)


def _read_registry_values(key: Any, winreg_module: Any) -> dict[str, object]:
    values: dict[str, object] = {}
    value_count = winreg_module.QueryInfoKey(key)[1]
    for index in range(value_count):
        try:
            name, value, _ = winreg_module.EnumValue(key, index)
        except OSError:
            continue
        if isinstance(name, str):
            values[name] = value
    return values


__all__ = ["GOGRegistry", "GOGRegistryEntry", "LocalGOGRegistry"]
