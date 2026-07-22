"""Normalize GOG-owned Windows installation records into safe launch data."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import PureWindowsPath

from vigil_overlay.contracts.games import GameDiscoveryContext
from vigil_overlay.providers.gog.filesystem import GOGFileSystem
from vigil_overlay.providers.gog.registry import GOGRegistry

_GAME_ID = re.compile(r"^[1-9][0-9]{0,19}$")
_MAX_GAME_INFO_BYTES = 1_048_576
_BLOCKED_EXECUTABLE_NAMES = frozenset(
    {
        "galaxyclient.exe",
        "galaxyclient helper.exe",
        "galaxyclientservice.exe",
        "galaxycommunication.exe",
        "goggalaxy.exe",
        "language_setup.exe",
        "setup.exe",
        "uninstall.exe",
    }
)


@dataclass(frozen=True, slots=True)
class GOGInstalledGame:
    """Validated GOG game data derived from a launcher-owned registry record."""

    provider_game_id: str
    title: str
    install_directory: str
    executable_path: str | None
    arguments: tuple[str, ...] = ()
    working_directory: str | None = None


@dataclass(frozen=True, slots=True)
class _GOGManifestLaunch:
    title: str
    executable_path: str | None
    arguments: tuple[str, ...]
    working_directory: str


class GOGInstalledGameScanner:
    """Read GOG installer records while rejecting stale or unsafe launch paths."""

    def __init__(self, registry: GOGRegistry, filesystem: GOGFileSystem) -> None:
        self._registry = registry
        self._filesystem = filesystem

    def scan(
        self,
        context: GameDiscoveryContext,
    ) -> tuple[tuple[GOGInstalledGame, ...], tuple[str, ...]]:
        games: dict[str, GOGInstalledGame] = {}
        warnings: list[str] = []
        for entry in self._registry.entries(context):
            if context.is_cancelled():
                warnings.append("GOG installed-game discovery was cancelled.")
                break
            try:
                game, entry_warnings = self._normalize(
                    entry.source_id,
                    entry.values,
                    context,
                )
            except (OSError, TimeoutError, ValueError) as exc:
                warnings.append(
                    f"GOG installation record {entry.source_id!r} was ignored: {exc}"
                )
                continue
            warnings.extend(entry_warnings)
            current = games.get(game.provider_game_id)
            if current is None or (
                current.executable_path is None and game.executable_path is not None
            ):
                games[game.provider_game_id] = game
        return tuple(games.values()), tuple(warnings)

    def has_managed_installation(self, context: GameDiscoveryContext) -> bool:
        for entry in self._registry.entries(context):
            if context.is_cancelled():
                return False
            try:
                self._normalize(entry.source_id, entry.values, context)
            except (OSError, TimeoutError, ValueError):
                continue
            return True
        return False

    def _normalize(
        self,
        source_id: str,
        values: dict[str, object],
        context: GameDiscoveryContext,
    ) -> tuple[GOGInstalledGame, tuple[str, ...]]:
        normalized = {key.casefold(): value for key, value in values.items()}
        provider_game_id = _game_id(normalized, source_id)
        install_directory = _absolute_directory(normalized, "path")
        if not self._filesystem.is_dir(install_directory, context):
            raise ValueError("installed game directory does not exist")

        manifest, manifest_warnings = self._manifest_launch(
            provider_game_id,
            install_directory,
            context,
        )
        title = _optional_text(normalized, "gamename")
        if title is None and manifest is not None:
            title = manifest.title
        if title is None:
            raise ValueError("GOG installation record is missing valid gamename")
        if len(title) > 512:
            raise ValueError("GOG gamename exceeds 512 characters")

        executable_path = None
        arguments: tuple[str, ...] = ()
        working_directory: str | None = None
        if manifest is not None and manifest.executable_path is not None:
            executable_path = manifest.executable_path
            arguments = manifest.arguments
            working_directory = manifest.working_directory
        elif (executable := _optional_text(normalized, "exe")) is not None:
            executable_path = self._resolve_executable(
                executable,
                install_directory,
                context,
            )
            launch_parameters = _optional_text(normalized, "launchparam")
            if launch_parameters is not None:
                arguments = _split_windows_arguments(launch_parameters)
            working_directory = self._resolve_working_directory(
                _optional_text(normalized, "workingdir"),
                install_directory,
                context,
            )

        return (
            GOGInstalledGame(
                provider_game_id=provider_game_id,
                title=title,
                install_directory=install_directory,
                executable_path=executable_path,
                arguments=arguments,
                working_directory=working_directory,
            ),
            manifest_warnings,
        )

    def _manifest_launch(
        self,
        provider_game_id: str,
        install_directory: str,
        context: GameDiscoveryContext,
    ) -> tuple[_GOGManifestLaunch | None, tuple[str, ...]]:
        expected_name = f"goggame-{provider_game_id}.info"
        candidates = tuple(
            path
            for path in self._filesystem.game_info_files(install_directory, context)
            if PureWindowsPath(path).name.casefold() == expected_name.casefold()
        )
        if not candidates:
            return None, ()
        if len(candidates) > 1:
            return None, (f"GOG game {provider_game_id} has duplicate metadata files.",)
        source = candidates[0]
        try:
            text = self._filesystem.read_text(
                source,
                context,
                max_bytes=_MAX_GAME_INFO_BYTES,
            )
            payload = json.loads(text)
            manifest = self._parse_manifest(
                payload,
                provider_game_id,
                install_directory,
                context,
            )
        except (
            OSError,
            TimeoutError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            return None, (f"GOG metadata {source!r} was ignored: {exc}",)
        return manifest, ()

    def _parse_manifest(
        self,
        payload: object,
        provider_game_id: str,
        install_directory: str,
        context: GameDiscoveryContext,
    ) -> _GOGManifestLaunch:
        if not isinstance(payload, dict):
            raise ValueError("metadata root must be an object")
        manifest_id = payload.get("gameId")
        if manifest_id != provider_game_id:
            raise ValueError("metadata gameId does not match its installation record")
        title = payload.get("name")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 512:
            raise ValueError("metadata name must be bounded non-empty text")
        tasks = payload.get("playTasks")
        if not isinstance(tasks, list) or len(tasks) > 64:
            raise ValueError("metadata playTasks must be a bounded list")
        primary = next(
            (
                task
                for task in tasks
                if isinstance(task, dict)
                and task.get("type") == "FileTask"
                and task.get("isPrimary") is True
            ),
            None,
        )
        if primary is None:
            return _GOGManifestLaunch(title.strip(), None, (), install_directory)
        raw_path = primary.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("primary FileTask path must be non-empty text")
        executable = self._resolve_executable(raw_path, install_directory, context)
        raw_arguments = primary.get("arguments")
        arguments: tuple[str, ...] = ()
        if raw_arguments is not None:
            if not isinstance(raw_arguments, str):
                raise ValueError("primary FileTask arguments must be text")
            if raw_arguments.strip():
                arguments = _split_windows_arguments(raw_arguments.strip())
        raw_working_directory = primary.get("workingDir")
        if raw_working_directory is not None and not isinstance(
            raw_working_directory, str
        ):
            raise ValueError("primary FileTask workingDir must be text")
        working_directory = self._resolve_working_directory(
            (
                raw_working_directory.strip()
                if isinstance(raw_working_directory, str)
                else None
            ),
            install_directory,
            context,
        )
        return _GOGManifestLaunch(
            title.strip(),
            executable,
            arguments,
            working_directory,
        )

    def _resolve_executable(
        self,
        value: str,
        install_directory: str,
        context: GameDiscoveryContext,
    ) -> str | None:
        candidate = _resolve_install_path(value, install_directory)
        name = candidate.name.casefold()
        if candidate.suffix.casefold() != ".exe":
            return None
        if name in _BLOCKED_EXECUTABLE_NAMES or name.startswith("unins"):
            return None
        if not _path_is_within_or_equal(candidate, PureWindowsPath(install_directory)):
            return None
        candidate_text = str(candidate)
        if not self._filesystem.is_file(candidate_text, context):
            return None
        return candidate_text

    def _resolve_working_directory(
        self,
        value: str | None,
        install_directory: str,
        context: GameDiscoveryContext,
    ) -> str:
        if value is None:
            return install_directory
        candidate = _resolve_install_path(value, install_directory)
        if not _path_is_within_or_equal(candidate, PureWindowsPath(install_directory)):
            return install_directory
        candidate_text = str(candidate)
        if not self._filesystem.is_dir(candidate_text, context):
            return install_directory
        return candidate_text


def _game_id(values: dict[str, object], source_id: str) -> str:
    value = _optional_text(values, "gameid")
    raw_value = values.get("gameid")
    if value is None and type(raw_value) is int and raw_value > 0:
        value = str(raw_value)
    if value is None:
        value = source_id.rpartition(":")[2].strip()
    if not _GAME_ID.fullmatch(value):
        raise ValueError("GOG game ID must be a positive numeric product ID")
    return value


def _optional_text(values: dict[str, object], key: str) -> str | None:
    value = values.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if any(character in stripped for character in ("\x00", "\r", "\n")):
        raise ValueError(f"GOG {key} contains an unsupported control character")
    return stripped


def _required_text(values: dict[str, object], key: str, *, max_length: int) -> str:
    value = _optional_text(values, key)
    if value is None:
        raise ValueError(f"GOG installation record is missing valid {key}")
    if len(value) > max_length:
        raise ValueError(f"GOG {key} exceeds {max_length} characters")
    return value


def _absolute_directory(values: dict[str, object], key: str) -> str:
    value = _required_text(values, key, max_length=32_767)
    path = PureWindowsPath(os.path.expandvars(_strip_outer_quotes(value)))
    if not path.is_absolute():
        raise ValueError(f"GOG {key} must be an absolute Windows path")
    return str(path)


def _resolve_install_path(value: str, install_directory: str) -> PureWindowsPath:
    cleaned = os.path.expandvars(_strip_outer_quotes(value))
    candidate = PureWindowsPath(cleaned)
    if not candidate.is_absolute():
        candidate = PureWindowsPath(install_directory) / candidate
    return candidate


def _strip_outer_quotes(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped.startswith('"') and stripped.endswith('"'):
        return stripped[1:-1]
    return stripped


def _path_is_within_or_equal(path: PureWindowsPath, root: PureWindowsPath) -> bool:
    path_parts = tuple(part.casefold() for part in path.parts)
    root_parts = tuple(part.casefold() for part in root.parts)
    return (
        len(path_parts) >= len(root_parts)
        and path_parts[: len(root_parts)] == root_parts
    )


def _split_windows_arguments(value: str) -> tuple[str, ...]:
    if len(value) > 8_192:
        raise ValueError("GOG launchParam exceeds 8192 characters")
    arguments: list[str] = []
    index = 0
    while index < len(value):
        while index < len(value) and value[index] in " \t":
            index += 1
        if index >= len(value):
            break
        argument: list[str] = []
        in_quotes = False
        while index < len(value) and (in_quotes or value[index] not in " \t"):
            if value[index] == "\\":
                slash_start = index
                while index < len(value) and value[index] == "\\":
                    index += 1
                slash_count = index - slash_start
                if index < len(value) and value[index] == '"':
                    argument.extend("\\" * (slash_count // 2))
                    if slash_count % 2:
                        argument.append('"')
                        index += 1
                    else:
                        in_quotes = not in_quotes
                        index += 1
                else:
                    argument.extend("\\" * slash_count)
                continue
            if value[index] == '"':
                in_quotes = not in_quotes
                index += 1
                continue
            argument.append(value[index])
            index += 1
        if in_quotes:
            raise ValueError("GOG launchParam contains an unmatched quote")
        arguments.append("".join(argument))
        if len(arguments) > 64:
            raise ValueError("GOG launchParam exceeds 64 arguments")
    return tuple(arguments)


__all__ = ["GOGInstalledGame", "GOGInstalledGameScanner"]
