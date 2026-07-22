"""Normalize Battle.net-managed entries from Windows installed-app inventory."""

from __future__ import annotations

from dataclasses import dataclass

from vigil_overlay.contracts.games import GameDiscoveryContext
from vigil_overlay.providers.battlenet.filesystem import BattleNetFileSystem
from vigil_overlay.providers.battlenet.registry import BattleNetRegistry
from vigil_overlay.providers.windows_inventory import (
    optional_installed_app_int,
    optional_installed_app_text,
    required_absolute_install_directory,
    required_installed_app_text,
    resolve_display_icon_executable,
    stable_installed_game_id,
)

_BLOCKED_EXECUTABLE_NAMES = {
    "agent.exe",
    "battle.net.exe",
    "battle.net launcher.exe",
    "blizzard launcher.exe",
    "uninstall.exe",
}
_BLOCKED_TITLES = {
    "battle.net",
    "battle.net desktop app",
    "blizzard battle.net",
}


@dataclass(frozen=True, slots=True)
class BattleNetInstalledGame:
    """Validated Battle.net game data derived from Windows inventory."""

    provider_game_id: str
    title: str
    install_directory: str
    executable_path: str | None


class BattleNetInstalledAppScanner:
    """Accept only conservative Battle.net-managed game-shaped installed-app entries."""

    def __init__(
        self, registry: BattleNetRegistry, filesystem: BattleNetFileSystem
    ) -> None:
        self._registry = registry
        self._filesystem = filesystem

    def scan(
        self,
        context: GameDiscoveryContext,
    ) -> tuple[tuple[BattleNetInstalledGame, ...], tuple[str, ...]]:
        games: list[BattleNetInstalledGame] = []
        warnings: list[str] = []
        seen: set[str] = set()
        for entry in self._registry.entries(context):
            if context.is_cancelled():
                warnings.append("Battle.net installed-app discovery was cancelled.")
                break
            try:
                game = self._normalize(entry.values, context)
            except (OSError, TimeoutError, ValueError) as exc:
                warnings.append(
                    f"Battle.net installed-app entry {entry.source_id!r} was ignored: {exc}"
                )
                continue
            if game is None or game.provider_game_id in seen:
                continue
            seen.add(game.provider_game_id)
            games.append(game)
        return tuple(games), tuple(warnings)

    def has_managed_installation(self, context: GameDiscoveryContext) -> bool:
        for entry in self._registry.entries(context):
            if context.is_cancelled():
                return False
            if not _looks_battlenet_managed(entry.values):
                continue
            if optional_installed_app_int(entry.values, "SystemComponent") == 1:
                continue
            title = optional_installed_app_text(entry.values, "DisplayName")
            if title is None or title.casefold() in _BLOCKED_TITLES:
                continue
            return True
        return False

    def _normalize(
        self,
        values: dict[str, object],
        context: GameDiscoveryContext,
    ) -> BattleNetInstalledGame | None:
        if not _looks_battlenet_managed(values):
            return None
        if optional_installed_app_int(values, "SystemComponent") == 1:
            return None

        title = required_installed_app_text(values, "DisplayName", max_length=512)
        if title.casefold() in _BLOCKED_TITLES:
            return None

        install_directory = required_absolute_install_directory(
            values, "InstallLocation"
        )
        if not self._filesystem.is_dir(install_directory, context):
            raise ValueError("installed game directory does not exist")

        executable = resolve_display_icon_executable(
            values,
            install_directory,
            self._filesystem,
            context,
            blocked_executable_names=_BLOCKED_EXECUTABLE_NAMES,
        )
        return BattleNetInstalledGame(
            provider_game_id=stable_installed_game_id(title, install_directory),
            title=title,
            install_directory=install_directory,
            executable_path=executable,
        )


def _looks_battlenet_managed(values: dict[str, object]) -> bool:
    publisher = optional_installed_app_text(values, "Publisher")
    if publisher is None:
        return False
    normalized_publisher = publisher.casefold()
    if (
        "blizzard entertainment" not in normalized_publisher
        and "activision" not in normalized_publisher
    ):
        return False
    markers = " ".join(
        value.casefold()
        for key in ("UninstallString", "QuietUninstallString", "ModifyPath")
        if (value := optional_installed_app_text(values, key)) is not None
    )
    return "battle.net" in markers or "blizzard" in markers


__all__ = ["BattleNetInstalledAppScanner", "BattleNetInstalledGame"]
