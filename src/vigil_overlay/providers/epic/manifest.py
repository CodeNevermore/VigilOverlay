"""Parse local Epic Games Launcher installed-item manifests defensively."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any

from vigil_overlay.contracts.games import GameDiscoveryContext
from vigil_overlay.providers.epic.filesystem import EpicFileSystem

_MAX_MANIFEST_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class EpicManifestRecord:
    """Normalized installed-game fields from one Epic item manifest."""

    provider_game_id: str
    sandbox_id: str
    catalog_id: str
    artifact_id: str
    title: str
    install_directory: str
    executable_path: str | None


class EpicManifestScanner:
    """Read installed game manifests while ignoring non-game launcher content."""

    def __init__(self, filesystem: EpicFileSystem) -> None:
        self._filesystem = filesystem

    def scan(
        self,
        manifest_root: str,
        context: GameDiscoveryContext,
    ) -> tuple[tuple[EpicManifestRecord, ...], tuple[str, ...]]:
        pattern = str(PureWindowsPath(manifest_root) / "*.item")
        try:
            paths = self._filesystem.glob(pattern, context)
        except (OSError, TimeoutError) as exc:
            return (), (f"Epic Games manifest scan was skipped: {exc}",)

        records: list[EpicManifestRecord] = []
        warnings: list[str] = []
        seen_ids: set[str] = set()
        for path in paths:
            if context.is_cancelled():
                warnings.append("Epic Games manifest scanning was cancelled.")
                break
            try:
                record = self._read_manifest(path, context)
            except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                warnings.append(f"Epic Games manifest {path!r} was ignored: {exc}")
                continue
            if record is None or record.provider_game_id in seen_ids:
                continue
            seen_ids.add(record.provider_game_id)
            records.append(record)
        return tuple(records), tuple(warnings)

    def _read_manifest(
        self,
        path: str,
        context: GameDiscoveryContext,
    ) -> EpicManifestRecord | None:
        raw = self._filesystem.read_text(path, context)
        if len(raw.encode("utf-8", errors="replace")) > _MAX_MANIFEST_BYTES:
            raise ValueError("manifest exceeds the 4 MiB safety limit")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("manifest root must be an object")

        if _optional_bool(payload, "bIsIncompleteInstall") is True:
            return None
        if _optional_bool(payload, "bIsApplication") is False:
            return None

        categories = _optional_string_list(payload, "AppCategories")
        if categories is None or "games" not in {
            value.casefold() for value in categories
        }:
            return None

        sandbox_id = _required_text(payload, "CatalogNamespace", max_length=256)
        catalog_id = _required_text(payload, "CatalogItemId", max_length=256)
        artifact_id = _required_text(payload, "AppName", max_length=256)
        title = _required_text(payload, "DisplayName", max_length=512)
        install_directory = _required_windows_absolute_path(payload, "InstallLocation")
        if not self._filesystem.is_dir(install_directory, context):
            raise ValueError("installed game directory does not exist")

        executable_path = _resolve_optional_executable(
            payload,
            install_directory,
            self._filesystem,
            context,
        )
        provider_game_id = f"{sandbox_id}:{catalog_id}:{artifact_id}"
        if len(provider_game_id) > 256:
            raise ValueError("combined Epic game identity exceeds 256 characters")
        return EpicManifestRecord(
            provider_game_id=provider_game_id,
            sandbox_id=sandbox_id,
            catalog_id=catalog_id,
            artifact_id=artifact_id,
            title=title,
            install_directory=install_directory,
            executable_path=executable_path,
        )


def _required_text(payload: dict[str, Any], key: str, *, max_length: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"manifest is missing valid {key}")
    if len(value) > max_length or any(
        character in value for character in ("\x00", "\r", "\n")
    ):
        raise ValueError(f"manifest {key} is invalid")
    return value


def _required_windows_absolute_path(payload: dict[str, Any], key: str) -> str:
    value = _required_text(payload, key, max_length=32_767)
    path = PureWindowsPath(value)
    if not path.is_absolute():
        raise ValueError(f"manifest {key} must be an absolute Windows path")
    return str(path)


def _optional_bool(payload: dict[str, Any], key: str) -> bool | None:
    value = payload.get(key)
    if value is None:
        return None
    if type(value) is not bool:
        raise ValueError(f"manifest {key} must be a boolean")
    return value


def _optional_string_list(payload: dict[str, Any], key: str) -> tuple[str, ...] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"manifest {key} must be an array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"manifest {key} must contain non-empty strings")
        result.append(item.strip())
    return tuple(result)


def _resolve_optional_executable(
    payload: dict[str, Any],
    install_directory: str,
    filesystem: EpicFileSystem,
    context: GameDiscoveryContext,
) -> str | None:
    value = payload.get("LaunchExecutable")
    if not isinstance(value, str) or not value.strip():
        return None
    relative = PureWindowsPath(value.strip())
    if (
        relative.is_absolute()
        or relative.drive
        or relative.root
        or ".." in relative.parts
    ):
        return None
    candidate = str(PureWindowsPath(install_directory) / relative)
    try:
        is_executable = PureWindowsPath(candidate).suffix.casefold() == ".exe"
        if filesystem.is_file(candidate, context) and is_executable:
            return candidate
    except (OSError, TimeoutError):
        return None
    return None


__all__ = ["EpicManifestRecord", "EpicManifestScanner"]
