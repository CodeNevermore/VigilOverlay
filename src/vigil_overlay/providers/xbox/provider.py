"""Native read-only Xbox / Microsoft Store PC game provider."""

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
from vigil_overlay.providers.xbox.filesystem import LocalXboxFileSystem, XboxFileSystem
from vigil_overlay.providers.xbox.installation import XboxGamesInstallationLocator
from vigil_overlay.providers.xbox.manifest import XboxGameManifestScanner


class XboxProvider:
    """Discover accessible GDK/MSIXVC XboxGames installs without launcher database access."""

    descriptor = GameProviderDescriptor("xbox", "Xbox / Microsoft Store")

    def __init__(
        self,
        *,
        filesystem: XboxFileSystem | None = None,
        locator: XboxGamesInstallationLocator | None = None,
    ) -> None:
        self._filesystem = filesystem or LocalXboxFileSystem()
        self._locator = locator or XboxGamesInstallationLocator(self._filesystem)
        self._manifests = XboxGameManifestScanner(self._filesystem)

    def discover_games(self, context: GameDiscoveryContext) -> GameProviderSnapshot:
        roots = self._locator.locate_roots(context)
        if not roots:
            return GameProviderSnapshot(provider=self.descriptor, games=())

        manifests, manifest_warnings = self._manifests.scan(roots, context)
        warnings = list(manifest_warnings)
        games: list[GameRecord] = []
        for manifest in manifests:
            if context.is_cancelled():
                break
            executable_exists = self._filesystem.is_file(
                manifest.executable_path, context
            )
            launch_target = None
            if executable_exists:
                launch_target = GameLaunchTarget(
                    GameLaunchTargetKind.EXECUTABLE,
                    manifest.executable_path,
                    working_directory=manifest.install_directory,
                )
            else:
                warnings.append(
                    f"Xbox game executable is missing for {manifest.title}: "
                    f"{manifest.executable_path}"
                )

            icon = None
            if manifest.icon_path is not None:
                icon = GameIconReference(GameIconKind.LOCAL_IMAGE, manifest.icon_path)
            elif executable_exists:
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
                    is_available=executable_exists,
                    launch_target=launch_target,
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


__all__ = ["XboxProvider"]
