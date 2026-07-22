"""Resolve Steam-owned LastPlayed timestamps from local Steam user configuration."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PureWindowsPath

from vigil_overlay.contracts.games import GameDiscoveryContext, GameRecency
from vigil_overlay.providers.steam.filesystem import SteamFileSystem
from vigil_overlay.providers.steam.vdf import (
    ValveKeyValuesParser,
    VdfObject,
    child_object,
    string_value,
)


class SteamRecencyResolver:
    """Read Steam-owned last-played evidence from local user configuration."""

    def __init__(
        self,
        filesystem: SteamFileSystem,
        parser: ValveKeyValuesParser | None = None,
    ) -> None:
        self._filesystem = filesystem
        self._parser = parser or ValveKeyValuesParser()

    def resolve(
        self,
        steam_root: str,
        context: GameDiscoveryContext,
    ) -> tuple[dict[str, GameRecency], tuple[str, ...]]:
        pattern = str(
            PureWindowsPath(steam_root)
            / "userdata"
            / "*"
            / "config"
            / "localconfig.vdf"
        )
        warnings: list[str] = []
        timestamps: dict[str, int] = {}
        try:
            configs = self._filesystem.glob(pattern, context)
        except (OSError, TimeoutError) as exc:
            return {}, (f"Steam recency configuration could not be scanned: {exc}",)

        for config_path in configs:
            if context.is_cancelled():
                warnings.append("Steam recency discovery was cancelled.")
                break
            try:
                parsed = self._parser.parse(
                    self._filesystem.read_text(config_path, context)
                )
                apps = _apps_object(parsed)
            except (OSError, TimeoutError, ValueError) as exc:
                warnings.append(
                    f"Steam recency file {config_path!r} was ignored: {exc}"
                )
                continue
            if apps is None:
                continue
            for app_id, value in apps.items():
                if not app_id.isdigit() or not isinstance(value, dict):
                    continue
                last_played = string_value(value, "LastPlayed")
                if last_played is None:
                    continue
                try:
                    epoch = int(last_played)
                except ValueError:
                    continue
                if epoch <= 0:
                    continue
                timestamps[app_id] = max(timestamps.get(app_id, 0), epoch)

        recency = {
            app_id: GameRecency(
                source="steam.localconfig.lastplayed",
                confidence=1.0,
                last_played_utc=datetime.fromtimestamp(epoch, tz=UTC),
            )
            for app_id, epoch in timestamps.items()
        }
        return recency, tuple(warnings)


def _apps_object(root: VdfObject) -> VdfObject | None:
    current = root
    for key in ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps"):
        child = child_object(current, key)
        if child is None:
            return None
        current = child
    return current


__all__ = ["SteamRecencyResolver"]
