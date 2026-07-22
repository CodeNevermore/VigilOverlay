"""Native Steam provider assembled from isolated discovery components."""

from __future__ import annotations

from vigil_overlay.contracts.games import (
    GameDiscoveryContext,
    GameIdentity,
    GameProviderDescriptor,
    GameProviderSnapshot,
    GameRecord,
)
from vigil_overlay.providers.steam.filesystem import LocalSteamFileSystem, SteamFileSystem
from vigil_overlay.providers.steam.icons import SteamIconResolver
from vigil_overlay.providers.steam.installation import SteamInstallationLocator
from vigil_overlay.providers.steam.launch import SteamLaunchTargetFactory
from vigil_overlay.providers.steam.library import SteamLibraryReader
from vigil_overlay.providers.steam.manifest import SteamManifestScanner
from vigil_overlay.providers.steam.recency import SteamRecencyResolver


class SteamProvider:
    """Discover installed Steam games and provider-owned recent-play timestamps read-only."""

    descriptor = GameProviderDescriptor("steam", "Steam")

    def __init__(
        self,
        *,
        filesystem: SteamFileSystem | None = None,
        locator: SteamInstallationLocator | None = None,
    ) -> None:
        self._filesystem = filesystem or LocalSteamFileSystem()
        self._locator = locator or SteamInstallationLocator(self._filesystem)
        self._libraries = SteamLibraryReader(self._filesystem)
        self._manifests = SteamManifestScanner(self._filesystem)
        self._recency = SteamRecencyResolver(self._filesystem)
        self._icons = SteamIconResolver(self._filesystem)
        self._launch_targets = SteamLaunchTargetFactory()

    def discover_games(self, context: GameDiscoveryContext) -> GameProviderSnapshot:
        steam_root = self._locator.locate(context)
        if steam_root is None:
            return GameProviderSnapshot(provider=self.descriptor, games=())

        libraries, library_warnings = self._libraries.read_libraries(
            steam_root, context
        )
        manifests, manifest_warnings = self._manifests.scan(libraries, context)
        recency, recency_warnings = self._recency.resolve(steam_root, context)
        warnings = [*library_warnings, *manifest_warnings, *recency_warnings]
        games: list[GameRecord] = []
        for manifest in manifests:
            if context.is_cancelled():
                warnings.append("Steam game normalization was cancelled.")
                break
            icon = self._icons.resolve(
                steam_root,
                manifest.app_id,
                manifest.install_directory,
                context,
            )
            games.append(
                GameRecord(
                    identity=GameIdentity(self.descriptor.provider_id, manifest.app_id),
                    title=manifest.title,
                    is_installed=True,
                    is_available=True,
                    launch_target=self._launch_targets.create(manifest.app_id),
                    install_directory=manifest.install_directory,
                    icon=icon,
                    recency=recency.get(manifest.app_id),
                )
            )

        return GameProviderSnapshot(
            provider=self.descriptor,
            games=tuple(games),
            complete=not context.is_cancelled(),
            warnings=tuple(warnings),
        )


__all__ = ["SteamProvider"]
