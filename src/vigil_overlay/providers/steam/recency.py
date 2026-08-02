"""Resolve Steam-owned LastPlayed timestamps from local Steam user configuration."""

from __future__ import annotations

import sys
from collections.abc import Callable
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

ActiveSteamUserResolver = Callable[[], str | None]


class SteamRecencyResolver:
    """Read Steam-owned last-played evidence from local user configuration."""

    def __init__(
        self,
        filesystem: SteamFileSystem,
        parser: ValveKeyValuesParser | None = None,
        *,
        active_user_resolver: ActiveSteamUserResolver | None = None,
    ) -> None:
        self._filesystem = filesystem
        self._parser = parser or ValveKeyValuesParser()
        self._active_user_resolver = (
            active_user_resolver or _windows_active_steam_user_id
        )

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

        active_user_id, account_warnings = self._resolve_active_user_id(
            steam_root,
            context,
        )
        warnings.extend(account_warnings)
        selected_configs = _select_account_configs(configs, active_user_id)
        if active_user_id is not None and not selected_configs:
            warnings.append(
                "Steam active account was identified, but its local recency file "
                "is unavailable."
            )
            return {}, tuple(warnings)
        if active_user_id is None and len(configs) > 1:
            warnings.append(
                "Steam has multiple local accounts and the active account could not "
                "be determined; recency was withheld to avoid mixing user histories."
            )
            return {}, tuple(warnings)
        if active_user_id is None:
            selected_configs = configs

        for config_path in selected_configs:
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

    def _resolve_active_user_id(
        self,
        steam_root: str,
        context: GameDiscoveryContext,
    ) -> tuple[str | None, tuple[str, ...]]:
        registry_warning: tuple[str, ...]
        try:
            registry_id = self._active_user_resolver()
        except OSError as exc:
            registry_id = None
            registry_warning = (
                f"Steam active-account registry value could not be read: {exc}",
            )
        else:
            registry_warning = ()
        if registry_id is not None and registry_id.isdigit() and int(registry_id) > 0:
            return str(int(registry_id)), registry_warning

        loginusers_path = str(PureWindowsPath(steam_root) / "config" / "loginusers.vdf")
        try:
            if not self._filesystem.is_file(loginusers_path, context):
                return None, registry_warning
            parsed = self._parser.parse(
                self._filesystem.read_text(loginusers_path, context)
            )
        except (OSError, TimeoutError, ValueError) as exc:
            return (
                None,
                (
                    *registry_warning,
                    f"Steam loginusers.vdf could not identify the active account: {exc}",
                ),
            )

        users = child_object(parsed, "users")
        if users is None:
            return None, registry_warning
        for steam_id, value in users.items():
            if not steam_id.isdigit() or not isinstance(value, dict):
                continue
            if string_value(value, "MostRecent") != "1":
                continue
            account_id = int(steam_id) & 0xFFFFFFFF
            if account_id > 0:
                return str(account_id), registry_warning
        return None, registry_warning


def _apps_object(root: VdfObject) -> VdfObject | None:
    current = root
    for key in ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps"):
        child = child_object(current, key)
        if child is None:
            return None
        current = child
    return current


def _select_account_configs(
    configs: tuple[str, ...],
    active_user_id: str | None,
) -> tuple[str, ...]:
    if active_user_id is None:
        return configs
    return tuple(
        path for path in configs if _account_id_from_config_path(path) == active_user_id
    )


def _account_id_from_config_path(path: str) -> str | None:
    parts = PureWindowsPath(path).parts
    for index, part in enumerate(parts[:-1]):
        if part.casefold() != "userdata" or index + 1 >= len(parts):
            continue
        candidate = parts[index + 1]
        return candidate if candidate.isdigit() else None
    return None


def _windows_active_steam_user_id() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            value, _value_type = winreg.QueryValueEx(key, "ActiveUser")
    except OSError:
        return None
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return str(int(value))
    return None


__all__ = ["ActiveSteamUserResolver", "SteamRecencyResolver"]
