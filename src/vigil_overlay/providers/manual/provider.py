"""Read-only manually configured game provider."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from typing import Any

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
from vigil_overlay.providers.windows_inventory import windows_file_exists

_MANUAL_SCHEMA_VERSION = 1
PathExists = Callable[[str], bool]


class ManualGameProvider:
    """Load explicitly configured executable games without recording play history."""

    descriptor = GameProviderDescriptor("manual", "Manual")

    def __init__(
        self,
        catalog_path: Path,
        *,
        path_exists: PathExists | None = None,
    ) -> None:
        self._catalog_path = catalog_path
        self._path_exists = path_exists or windows_file_exists

    @property
    def catalog_path(self) -> Path:
        return self._catalog_path

    def discover_games(self, context: GameDiscoveryContext) -> GameProviderSnapshot:
        if context.is_cancelled():
            return GameProviderSnapshot(
                provider=self.descriptor, games=(), complete=False
            )
        if not self._catalog_path.exists():
            return GameProviderSnapshot(provider=self.descriptor, games=())

        try:
            payload = json.loads(self._catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return GameProviderSnapshot(
                provider=self.descriptor,
                games=(),
                complete=False,
                warnings=(f"Manual game catalog could not be read: {exc}",),
            )
        if not isinstance(payload, dict):
            return _invalid_catalog("Manual game catalog root must be an object.")
        if payload.get("schema_version") != _MANUAL_SCHEMA_VERSION:
            return _invalid_catalog("Manual game catalog schema_version must be 1.")
        entries = payload.get("games")
        if not isinstance(entries, list):
            return _invalid_catalog("Manual game catalog games must be an array.")

        games: list[GameRecord] = []
        warnings: list[str] = []
        seen_ids: set[str] = set()
        for index, entry in enumerate(entries):
            if context.is_cancelled():
                return GameProviderSnapshot(
                    provider=self.descriptor,
                    games=tuple(games),
                    complete=False,
                    warnings=(*warnings, "Manual game discovery was cancelled."),
                )
            try:
                game = self._parse_entry(entry)
            except (TypeError, ValueError) as exc:
                warnings.append(f"Manual game entry {index} was ignored: {exc}")
                continue
            game_id = game.identity.provider_game_id
            if game_id in seen_ids:
                warnings.append(
                    f"Manual game entry {index} duplicated ID {game_id!r} and was ignored."
                )
                continue
            seen_ids.add(game_id)
            games.append(game)

        return GameProviderSnapshot(
            provider=self.descriptor,
            games=tuple(games),
            complete=True,
            warnings=tuple(warnings),
        )

    def _parse_entry(self, entry: Any) -> GameRecord:
        if not isinstance(entry, dict):
            raise TypeError("entry must be an object")
        allowed = {
            "id",
            "title",
            "executable",
            "arguments",
            "working_directory",
            "icon",
        }
        unknown = set(entry) - allowed
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
        game_id = _required_text(entry.get("id"), "id")
        title = _required_text(entry.get("title"), "title")
        executable = _absolute_windows_path(
            entry.get("executable"), "executable", suffix=".exe"
        )
        arguments = _arguments(entry.get("arguments", []))
        working_directory = _optional_windows_path(
            entry.get("working_directory"), "working_directory"
        )
        icon_path = _optional_windows_path(entry.get("icon"), "icon")
        installed = self._path_exists(executable)

        icon: GameIconReference | None = None
        if icon_path is not None and self._path_exists(icon_path):
            icon = GameIconReference(GameIconKind.LOCAL_IMAGE, icon_path)
        elif installed:
            icon = GameIconReference(GameIconKind.EXECUTABLE, executable)

        return GameRecord(
            identity=GameIdentity(self.descriptor.provider_id, game_id),
            title=title,
            is_installed=installed,
            is_available=installed,
            launch_target=(
                GameLaunchTarget(
                    GameLaunchTargetKind.EXECUTABLE,
                    executable,
                    arguments=arguments,
                    working_directory=working_directory,
                )
                if installed
                else None
            ),
            install_directory=(
                working_directory or str(PureWindowsPath(executable).parent)
                if installed
                else None
            ),
            icon=icon,
            recency=None,
        )


def _invalid_catalog(message: str) -> GameProviderSnapshot:
    return GameProviderSnapshot(
        provider=ManualGameProvider.descriptor,
        games=(),
        complete=False,
        warnings=(message,),
    )


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(
            f"{field_name} must be non-empty text without surrounding whitespace"
        )
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{field_name} contains an unsupported control character")
    return value


def _absolute_windows_path(
    value: Any, field_name: str, *, suffix: str | None = None
) -> str:
    text = _required_text(value, field_name)
    path = PureWindowsPath(text)
    if not path.is_absolute():
        raise ValueError(f"{field_name} must be an absolute Windows path")
    if suffix is not None and path.suffix.casefold() != suffix:
        raise ValueError(f"{field_name} must end in {suffix}")
    return text


def _optional_windows_path(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _absolute_windows_path(value, field_name)


def _arguments(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("arguments must be an array")
    arguments: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"arguments[{index}] must be a string")
        if any(character in item for character in ("\x00", "\r", "\n")):
            raise ValueError(
                f"arguments[{index}] contains an unsupported control character"
            )
        arguments.append(item)
    return tuple(arguments)


__all__ = ["ManualGameProvider"]
