"""Native read-only GOG provider backed by launcher-owned Windows records."""

from __future__ import annotations

from vigil_overlay.contracts.games import (
    GameDiscoveryContext,
    GameIconKind,
    GameIconReference,
    GameIdentity,
    GameLaunchTarget,
    GameLaunchTargetKind,
    GameProviderDescriptor,
    GameProviderSnapshot,
    GameRecord,
)
from vigil_overlay.providers.gog.filesystem import GOGFileSystem, LocalGOGFileSystem
from vigil_overlay.providers.gog.installed_games import GOGInstalledGameScanner
from vigil_overlay.providers.gog.registry import GOGRegistry, LocalGOGRegistry


class GOGProvider:
    """Discover GOG GALAXY and offline-installer games without launcher mutation."""

    descriptor = GameProviderDescriptor("gog", "GOG")

    def __init__(
        self,
        *,
        registry: GOGRegistry | None = None,
        filesystem: GOGFileSystem | None = None,
    ) -> None:
        selected_registry = registry or LocalGOGRegistry()
        selected_filesystem = filesystem or LocalGOGFileSystem()
        self._scanner = GOGInstalledGameScanner(selected_registry, selected_filesystem)

    def discover_games(self, context: GameDiscoveryContext) -> GameProviderSnapshot:
        installed_games, warnings = self._scanner.scan(context)
        games: list[GameRecord] = []
        for installed in installed_games:
            executable = installed.executable_path
            launch_target = None
            icon = None
            if executable is not None:
                launch_target = GameLaunchTarget(
                    GameLaunchTargetKind.EXECUTABLE,
                    executable,
                    arguments=installed.arguments,
                    working_directory=installed.working_directory,
                )
                icon = GameIconReference(GameIconKind.EXECUTABLE, executable)
            games.append(
                GameRecord(
                    identity=GameIdentity(
                        self.descriptor.provider_id,
                        installed.provider_game_id,
                    ),
                    title=installed.title,
                    is_installed=True,
                    is_available=launch_target is not None,
                    launch_target=launch_target,
                    install_directory=installed.install_directory,
                    icon=icon,
                    recency=None,
                )
            )
        return GameProviderSnapshot(
            provider=self.descriptor,
            games=tuple(games),
            complete=not context.is_cancelled(),
            warnings=warnings,
        )


__all__ = ["GOGProvider"]
