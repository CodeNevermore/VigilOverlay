"""Native read-only EA app provider backed by Windows installed-app inventory."""

from __future__ import annotations

from vigil_overlay.contracts.games import (
    GameDiscoveryContext,
    GameProviderDescriptor,
    GameProviderSnapshot,
)
from vigil_overlay.providers.ea.filesystem import EAFileSystem, LocalEAFileSystem
from vigil_overlay.providers.ea.installed_apps import EAInstalledAppScanner
from vigil_overlay.providers.ea.registry import EARegistry, LocalEARegistry
from vigil_overlay.providers.windows_inventory import build_installed_executable_snapshot


class EAAppProvider:
    """Discover EA app-managed games without reading private launcher databases."""

    descriptor = GameProviderDescriptor("ea", "EA app")

    def __init__(
        self,
        *,
        registry: EARegistry | None = None,
        filesystem: EAFileSystem | None = None,
    ) -> None:
        selected_registry = registry or LocalEARegistry()
        selected_filesystem = filesystem or LocalEAFileSystem()
        self._scanner = EAInstalledAppScanner(selected_registry, selected_filesystem)

    def discover_games(self, context: GameDiscoveryContext) -> GameProviderSnapshot:
        installed_games, warnings = self._scanner.scan(context)
        return build_installed_executable_snapshot(
            descriptor=self.descriptor,
            installed_games=installed_games,
            warnings=warnings,
            context=context,
        )


__all__ = ["EAAppProvider"]
