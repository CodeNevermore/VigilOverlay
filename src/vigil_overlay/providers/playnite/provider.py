"""Read-only Playnite snapshot bridge behind Vigil's generic game-provider contract."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import quote
from uuid import UUID

from vigil_overlay.contracts.games import (
    GameDiscoveryContext,
    GameIconKind,
    GameIconReference,
    GameIdentity,
    GameLaunchTarget,
    GameLaunchTargetKind,
    GameProviderDescriptor,
    GameProviderSnapshot,
    GameRecency,
    GameRecord,
)
from vigil_overlay.providers.windows_inventory import windows_file_exists

PLAYNITE_BRIDGE_SCHEMA_VERSION = 1
PLAYNITE_BRIDGE_FILENAME = "playnite_bridge.json"
PLAYNITE_REFRESH_REQUEST_FILENAME = "playnite_refresh_request.json"
_MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
_MAX_GAMES = 100_000

PathExists = Callable[[str], bool]


class PlayniteBridgeProvider:
    """Consume an optional JSON snapshot emitted by the companion Playnite extension."""

    descriptor = GameProviderDescriptor("playnite", "Playnite")

    def __init__(
        self,
        snapshot_path: Path,
        *,
        path_exists: PathExists | None = None,
    ) -> None:
        self._snapshot_path = snapshot_path
        self._refresh_request_path = snapshot_path.with_name(
            PLAYNITE_REFRESH_REQUEST_FILENAME
        )
        self._path_exists = path_exists or windows_file_exists

    @property
    def snapshot_path(self) -> Path:
        return self._snapshot_path

    def discover_games(self, context: GameDiscoveryContext) -> GameProviderSnapshot:
        if context.is_cancelled():
            return GameProviderSnapshot(
                provider=self.descriptor, games=(), complete=False
            )
        requested_refresh_id = self._requested_refresh_id()
        if requested_refresh_id is not None:
            self._await_requested_snapshot(context, requested_refresh_id)
        if not self._snapshot_path.exists():
            return GameProviderSnapshot(provider=self.descriptor, games=())

        try:
            raw_text = self._snapshot_path.read_text(encoding="utf-8")
        except OSError as exc:
            return _unavailable_snapshot(
                f"Playnite bridge snapshot could not be read: {exc}"
            )
        if len(raw_text.encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
            return _unavailable_snapshot(
                "Playnite bridge snapshot exceeds the 32 MiB safety limit."
            )

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            return _unavailable_snapshot(
                f"Playnite bridge snapshot is invalid JSON: {exc}"
            )
        if not isinstance(payload, dict):
            return _unavailable_snapshot(
                "Playnite bridge snapshot root must be an object."
            )
        allowed_root = {
            "schema_version",
            "generated_at_utc",
            "refresh_request_id",
            "games",
        }
        unknown_root = set(payload) - allowed_root
        if unknown_root:
            return _unavailable_snapshot(
                "Playnite bridge snapshot contains unknown root fields: "
                + ", ".join(sorted(unknown_root))
            )
        if payload.get("schema_version") != PLAYNITE_BRIDGE_SCHEMA_VERSION:
            return _unavailable_snapshot(
                "Playnite bridge snapshot schema_version must be 1."
            )

        refresh_request_id = payload.get("refresh_request_id")
        if refresh_request_id is not None:
            try:
                _canonical_guid(refresh_request_id, "refresh_request_id")
            except (TypeError, ValueError) as exc:
                return _unavailable_snapshot(
                    f"Playnite bridge refresh_request_id is invalid: {exc}"
                )

        generated_at = payload.get("generated_at_utc")
        if generated_at is not None:
            try:
                _parse_utc_datetime(generated_at, "generated_at_utc")
            except (TypeError, ValueError) as exc:
                return _unavailable_snapshot(
                    f"Playnite bridge snapshot timestamp is invalid: {exc}"
                )

        entries = payload.get("games")
        if not isinstance(entries, list):
            return _unavailable_snapshot(
                "Playnite bridge snapshot games must be an array."
            )
        if len(entries) > _MAX_GAMES:
            return _unavailable_snapshot(
                f"Playnite bridge snapshot contains more than {_MAX_GAMES} games."
            )

        games: list[GameRecord] = []
        warnings: list[str] = []
        seen_ids: set[str] = set()
        for index, entry in enumerate(entries):
            if context.is_cancelled():
                return GameProviderSnapshot(
                    provider=self.descriptor,
                    games=tuple(games),
                    complete=False,
                    warnings=(*warnings, "Playnite bridge discovery was cancelled."),
                )
            try:
                game, entry_warnings = self._parse_entry(entry)
            except (TypeError, ValueError) as exc:
                warnings.append(
                    f"Playnite bridge game entry {index} was ignored: {exc}"
                )
                continue
            game_id = game.identity.provider_game_id
            if game_id in seen_ids:
                warnings.append(
                    f"Playnite bridge game entry {index} duplicated ID {game_id!r} and was ignored."
                )
                continue
            seen_ids.add(game_id)
            games.append(game)
            warnings.extend(
                f"Playnite bridge game entry {index}: {item}" for item in entry_warnings
            )

        return GameProviderSnapshot(
            provider=self.descriptor,
            games=tuple(games),
            complete=True,
            warnings=tuple(warnings),
        )

    def _requested_refresh_id(self) -> str | None:
        try:
            payload = json.loads(self._refresh_request_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or set(payload) != {
            "request_id",
            "requested_at_utc",
        }:
            return None
        try:
            return _canonical_guid(payload.get("request_id"), "request_id")
        except (TypeError, ValueError):
            return None

    def _await_requested_snapshot(
        self,
        context: GameDiscoveryContext,
        request_id: str,
    ) -> None:
        while not context.is_cancelled():
            try:
                payload = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if (
                isinstance(payload, dict)
                and payload.get("refresh_request_id") == request_id
            ):
                with suppress(OSError):
                    self._refresh_request_path.unlink(missing_ok=True)
                return
            remaining = context.remaining_seconds()
            if remaining is not None and remaining <= 0.0:
                break
            time.sleep(min(0.1, remaining) if remaining is not None else 0.1)
        raise RuntimeError(
            "Playnite bridge did not rebuild its snapshot before the refresh timeout. "
            "Start or restart Playnite and try again."
        )

    def _parse_entry(self, entry: Any) -> tuple[GameRecord, tuple[str, ...]]:
        if not isinstance(entry, dict):
            raise TypeError("entry must be an object")
        allowed = {
            "id",
            "title",
            "is_installed",
            "install_directory",
            "icon",
            "last_played_utc",
        }
        unknown = set(entry) - allowed
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")

        game_id = _canonical_guid(entry.get("id"), "id")
        title = _required_text(entry.get("title"), "title", max_length=512)
        installed = _required_bool(entry.get("is_installed"), "is_installed")
        warnings: list[str] = []

        install_directory: str | None = None
        raw_install_directory = entry.get("install_directory")
        if raw_install_directory is not None:
            try:
                install_directory = _absolute_windows_path(
                    raw_install_directory,
                    "install_directory",
                )
            except (TypeError, ValueError) as exc:
                warnings.append(f"install_directory was ignored: {exc}")

        icon: GameIconReference | None = None
        raw_icon = entry.get("icon")
        if raw_icon is not None:
            try:
                icon_path = _absolute_windows_path(raw_icon, "icon")
            except (TypeError, ValueError) as exc:
                warnings.append(f"icon was ignored: {exc}")
            else:
                if self._path_exists(icon_path):
                    icon = GameIconReference(GameIconKind.LOCAL_IMAGE, icon_path)
                else:
                    warnings.append(
                        "icon was ignored because the local file is unavailable"
                    )

        recency: GameRecency | None = None
        raw_last_played = entry.get("last_played_utc")
        if raw_last_played is not None:
            try:
                last_played = _parse_utc_datetime(raw_last_played, "last_played_utc")
            except (TypeError, ValueError) as exc:
                warnings.append(f"last_played_utc was ignored: {exc}")
            else:
                recency = GameRecency(
                    source="playnite.provider.last_activity",
                    confidence=1.0,
                    last_played_utc=last_played,
                )

        launch_target = None
        if installed:
            encoded_id = quote(game_id, safe="-")
            launch_target = GameLaunchTarget(
                GameLaunchTargetKind.URI,
                f"playnite://playnite/start/{encoded_id}",
            )

        return (
            GameRecord(
                identity=GameIdentity(self.descriptor.provider_id, game_id),
                title=title,
                is_installed=installed,
                is_available=installed,
                launch_target=launch_target,
                install_directory=install_directory,
                icon=icon,
                recency=recency,
            ),
            tuple(warnings),
        )


def _unavailable_snapshot(message: str) -> GameProviderSnapshot:
    return GameProviderSnapshot(
        provider=PlayniteBridgeProvider.descriptor,
        games=(),
        complete=False,
        warnings=(message,),
    )


def _required_text(value: Any, field_name: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(
            f"{field_name} must be non-empty text without surrounding whitespace"
        )
    if len(value) > max_length:
        raise ValueError(f"{field_name} exceeds {max_length} characters")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{field_name} contains an unsupported control character")
    return value


def _required_bool(value: Any, field_name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _canonical_guid(value: Any, field_name: str) -> str:
    text = _required_text(value, field_name, max_length=64)
    try:
        parsed = UUID(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid GUID") from exc
    return str(parsed)


def _absolute_windows_path(value: Any, field_name: str) -> str:
    text = _required_text(value, field_name, max_length=32_767)
    if not PureWindowsPath(text).is_absolute():
        raise ValueError(f"{field_name} must be an absolute Windows path")
    return text


def _parse_utc_datetime(value: Any, field_name: str) -> datetime:
    text = _required_text(value, field_name, max_length=80)
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return parsed.astimezone(UTC)


__all__ = [
    "PLAYNITE_BRIDGE_FILENAME",
    "PLAYNITE_BRIDGE_SCHEMA_VERSION",
    "PLAYNITE_REFRESH_REQUEST_FILENAME",
    "PlayniteBridgeProvider",
]
