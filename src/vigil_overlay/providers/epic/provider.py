"""Native read-only Epic Games Launcher provider."""

from __future__ import annotations

from vigil_overlay.contracts.games import (
    GameDiscoveryContext,
    GameIconKind,
    GameIconReference,
    GameIdentity,
    GameProviderDescriptor,
    GameProviderSnapshot,
    GameRecord,
)
from vigil_overlay.providers.epic.filesystem import EpicFileSystem, LocalEpicFileSystem
from vigil_overlay.providers.epic.installation import EpicManifestInstallationLocator
from vigil_overlay.providers.epic.launch import EpicLaunchTargetFactory
from vigil_overlay.providers.epic.manifest import EpicManifestScanner


class EpicGamesProvider:
    """Discover installed Epic games from launcher-owned local manifests without mutation."""

    descriptor = GameProviderDescriptor("epic", "Epic Games")

    def __init__(
        self,
        *,
        filesystem: EpicFileSystem | None = None,
        locator: EpicManifestInstallationLocator | None = None,
    ) -> None:
        self._filesystem = filesystem or LocalEpicFileSystem()
        self._locator = locator or EpicManifestInstallationLocator(self._filesystem)
        self._manifests = EpicManifestScanner(self._filesystem)
        self._launch_targets = EpicLaunchTargetFactory()

    def discover_games(self, context: GameDiscoveryContext) -> GameProviderSnapshot:
        manifest_root = self._locator.locate(context)
        if manifest_root is None:
            return GameProviderSnapshot(provider=self.descriptor, games=())

        manifests, manifest_warnings = self._manifests.scan(manifest_root, context)
        warnings = list(manifest_warnings)
        games: list[GameRecord] = []
        for manifest in manifests:
            if context.is_cancelled():
                warnings.append("Epic Games game normalization was cancelled.")
                break
            icon = None
            if manifest.executable_path is not None:
                icon = GameIconReference(
                    GameIconKind.EXECUTABLE, manifest.executable_path
                )
            games.append(
                GameRecord(
                    identity=GameIdentity(
                        self.descriptor.provider_id,
                        manifest.provider_game_id,
                    ),
                    title=manifest.title,
                    is_installed=True,
                    is_available=True,
                    launch_target=self._launch_targets.create(
                        manifest.sandbox_id,
                        manifest.catalog_id,
                        manifest.artifact_id,
                    ),
                    install_directory=manifest.install_directory,
                    icon=icon,
                    recency=None,
                )
            )

        return GameProviderSnapshot(
            provider=self.descriptor,
            games=tuple(games),
            complete=not context.is_cancelled(),
            warnings=tuple(warnings),
        )


__all__ = ["EpicGamesProvider"]
