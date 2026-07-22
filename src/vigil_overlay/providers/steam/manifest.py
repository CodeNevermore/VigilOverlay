"""Scan installed Steam app manifests without coupling aggregation to Steam details."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PureWindowsPath

from vigil_overlay.contracts.games import GameDiscoveryContext
from vigil_overlay.providers.steam.filesystem import SteamFileSystem
from vigil_overlay.providers.steam.vdf import (
    ValveKeyValuesParser,
    VdfObject,
    child_object,
    string_value,
)


@dataclass(frozen=True, slots=True)
class SteamManifestRecord:
    """Normalized installed-game fields from one Steam app manifest."""

    app_id: str
    title: str
    library_path: str
    install_directory: str


class SteamManifestScanner:
    """Scan validated Steam libraries for installed application manifests."""

    def __init__(
        self,
        filesystem: SteamFileSystem,
        parser: ValveKeyValuesParser | None = None,
    ) -> None:
        self._filesystem = filesystem
        self._parser = parser or ValveKeyValuesParser()

    def scan(
        self,
        libraries: tuple[str, ...],
        context: GameDiscoveryContext,
    ) -> tuple[tuple[SteamManifestRecord, ...], tuple[str, ...]]:
        records: list[SteamManifestRecord] = []
        warnings: list[str] = []
        seen_app_ids: set[str] = set()
        for library in libraries:
            if context.is_cancelled():
                warnings.append("Steam manifest scanning was cancelled.")
                break
            pattern = str(PureWindowsPath(library) / "steamapps" / "appmanifest_*.acf")
            try:
                manifest_paths = self._filesystem.glob(pattern, context)
            except (OSError, TimeoutError) as exc:
                warnings.append(
                    f"Steam library {library!r} manifest scan was skipped: {exc}"
                )
                continue
            for manifest_path in manifest_paths:
                if context.is_cancelled():
                    warnings.append("Steam manifest scanning was cancelled.")
                    return tuple(records), tuple(warnings)
                try:
                    record = self._read_manifest(manifest_path, library, context)
                except (OSError, TimeoutError, ValueError) as exc:
                    warnings.append(
                        f"Steam manifest {manifest_path!r} was ignored: {exc}"
                    )
                    continue
                if record.app_id in seen_app_ids:
                    continue
                seen_app_ids.add(record.app_id)
                records.append(record)
        return tuple(records), tuple(warnings)

    def _read_manifest(
        self,
        manifest_path: str,
        library: str,
        context: GameDiscoveryContext,
    ) -> SteamManifestRecord:
        parsed = self._parser.parse(self._filesystem.read_text(manifest_path, context))
        state = child_object(parsed, "AppState") or parsed
        app_id = _required_manifest_value(state, "appid")
        if not app_id.isdigit():
            raise ValueError("appid must be numeric")
        title = _required_manifest_value(state, "name")
        install_dir_name = _required_manifest_value(state, "installdir")
        install_directory = str(
            PureWindowsPath(library) / "steamapps" / "common" / install_dir_name
        )
        if not self._filesystem.is_dir(install_directory, context):
            raise ValueError("installed game directory does not exist")
        return SteamManifestRecord(
            app_id=app_id,
            title=title,
            library_path=library,
            install_directory=install_directory,
        )


def _required_manifest_value(mapping: VdfObject, key: str) -> str:
    value = string_value(mapping, key)
    if value is None or not value.strip():
        raise ValueError(f"manifest is missing {key}")
    return value.strip()


__all__ = ["SteamManifestRecord", "SteamManifestScanner"]
