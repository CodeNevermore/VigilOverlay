"""Native read-only Ubisoft Connect provider backed by Windows installed-app inventory."""

from __future__ import annotations

from vigil_overlay.contracts.games import (
    GameDiscoveryContext,
    GameProviderDescriptor,
    GameProviderSnapshot,
)
from vigil_overlay.providers.ubisoft.filesystem import LocalUbisoftFileSystem, UbisoftFileSystem
from vigil_overlay.providers.ubisoft.installed_apps import UbisoftInstalledAppScanner
from vigil_overlay.providers.ubisoft.registry import LocalUbisoftRegistry, UbisoftRegistry
from vigil_overlay.providers.windows_inventory import build_installed_executable_snapshot


class UbisoftConnectProvider:
    """Discover Ubisoft Connect-managed games without reading private launcher databases."""

    descriptor = GameProviderDescriptor("ubisoft", "Ubisoft Connect")

    def __init__(
        self,
        *,
        registry: UbisoftRegistry | None = None,
        filesystem: UbisoftFileSystem | None = None,
    ) -> None:
        selected_registry = registry or LocalUbisoftRegistry()
        selected_filesystem = filesystem or LocalUbisoftFileSystem()
        self._scanner = UbisoftInstalledAppScanner(
            selected_registry, selected_filesystem
        )

    def discover_games(self, context: GameDiscoveryContext) -> GameProviderSnapshot:
        installed_games, warnings = self._scanner.scan(context)
        return build_installed_executable_snapshot(
            descriptor=self.descriptor,
            installed_games=installed_games,
            warnings=warnings,
            context=context,
        )


__all__ = ["UbisoftConnectProvider"]
