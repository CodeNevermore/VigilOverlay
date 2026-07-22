"""Native read-only Battle.net provider backed by Windows installed-app inventory."""

from __future__ import annotations

from vigil_overlay.contracts.games import (
    GameDiscoveryContext,
    GameProviderDescriptor,
    GameProviderSnapshot,
)
from vigil_overlay.providers.battlenet.filesystem import (
    BattleNetFileSystem,
    LocalBattleNetFileSystem,
)
from vigil_overlay.providers.battlenet.installed_apps import BattleNetInstalledAppScanner
from vigil_overlay.providers.battlenet.registry import BattleNetRegistry, LocalBattleNetRegistry
from vigil_overlay.providers.windows_inventory import build_installed_executable_snapshot


class BattleNetProvider:
    """Discover Battle.net-managed games without reading private launcher databases."""

    descriptor = GameProviderDescriptor("battlenet", "Battle.net")

    def __init__(
        self,
        *,
        registry: BattleNetRegistry | None = None,
        filesystem: BattleNetFileSystem | None = None,
    ) -> None:
        selected_registry = registry or LocalBattleNetRegistry()
        selected_filesystem = filesystem or LocalBattleNetFileSystem()
        self._scanner = BattleNetInstalledAppScanner(
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


__all__ = ["BattleNetProvider"]
