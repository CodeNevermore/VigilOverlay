"""Normalize EA app-managed entries from Windows installed-app inventory."""

from __future__ import annotations

from dataclasses import dataclass

from vigil_overlay.contracts.games import GameDiscoveryContext
from vigil_overlay.providers.ea.filesystem import EAFileSystem
from vigil_overlay.providers.ea.registry import EARegistry
from vigil_overlay.providers.windows_inventory import (
    optional_installed_app_int,
    optional_installed_app_text,
    required_absolute_install_directory,
    required_installed_app_text,
    resolve_display_icon_executable,
    stable_installed_game_id,
)

_BLOCKED_EXECUTABLE_NAMES = {
    "ea app.exe",
    "ea background service.exe",
    "ea desktop.exe",
    "eabackgroundservice.exe",
    "eadesktop.exe",
    "ealauncher.exe",
    "ealocalhostsvc.exe",
    "origin.exe",
    "originthinsetupinternal.exe",
    "touchup.exe",
    "uninstall.exe",
}
_BLOCKED_TITLES = {
    "ea app",
    "ea background service",
    "ea desktop",
    "ea error reporter",
    "origin",
}
_BLOCKED_TITLE_PREFIXES = (
    "ea anticheat",
    "ea anti-cheat",
)
_EA_MANAGEMENT_MARKERS = (
    "ea desktop",
    "eadesktop",
    "ea installer",
    "eainstaller",
    "/ea games/",
    "/electronic arts/",
    "/origin games/",
    "origin",
)


@dataclass(frozen=True, slots=True)
class EAInstalledGame:
    """Validated EA-managed game data derived from Windows inventory."""

    provider_game_id: str
    title: str
    install_directory: str
    executable_path: str | None


class EAInstalledAppScanner:
    """Accept only conservative EA-managed game-shaped installed-app entries."""

    def __init__(self, registry: EARegistry, filesystem: EAFileSystem) -> None:
        self._registry = registry
        self._filesystem = filesystem

    def scan(
        self,
        context: GameDiscoveryContext,
    ) -> tuple[tuple[EAInstalledGame, ...], tuple[str, ...]]:
        games: list[EAInstalledGame] = []
        warnings: list[str] = []
        seen: set[str] = set()
        for entry in self._registry.entries(context):
            if context.is_cancelled():
                warnings.append("EA app installed-game discovery was cancelled.")
                break
            try:
                game = self._normalize(entry.values, context)
            except (OSError, TimeoutError, ValueError) as exc:
                warnings.append(
                    f"EA app installed-app entry {entry.source_id!r} was ignored: {exc}"
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
            try:
                if self._normalize(entry.values, context) is not None:
                    return True
            except (OSError, TimeoutError, ValueError):
                continue
        return False

    def _normalize(
        self,
        values: dict[str, object],
        context: GameDiscoveryContext,
    ) -> EAInstalledGame | None:
        if not _looks_ea_managed(values):
            return None
        if optional_installed_app_int(values, "SystemComponent") == 1:
            return None

        title = required_installed_app_text(values, "DisplayName", max_length=512)
        if _blocked_title(title):
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
        return EAInstalledGame(
            provider_game_id=stable_installed_game_id(title, install_directory),
            title=title,
            install_directory=install_directory,
            executable_path=executable,
        )


def _looks_ea_managed(values: dict[str, object]) -> bool:
    publisher = optional_installed_app_text(values, "Publisher")
    if publisher is None:
        return False
    normalized_publisher = publisher.casefold()
    if (
        "electronic arts" not in normalized_publisher
        and "ea swiss" not in normalized_publisher
    ):
        return False

    markers = " ".join(
        value.casefold().replace("\\", "/")
        for key in (
            "UninstallString",
            "QuietUninstallString",
            "ModifyPath",
            "InstallSource",
        )
        if (value := optional_installed_app_text(values, key)) is not None
    )
    return any(marker in markers for marker in _EA_MANAGEMENT_MARKERS)


def _blocked_title(title: str) -> bool:
    normalized = title.casefold()
    return normalized in _BLOCKED_TITLES or normalized.startswith(
        _BLOCKED_TITLE_PREFIXES
    )


__all__ = ["EAInstalledAppScanner", "EAInstalledGame"]
