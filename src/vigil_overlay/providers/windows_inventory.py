"""Shared read-only primitives for Windows installed-app game providers."""

from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol

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

_DISPLAY_ICON_EXE = re.compile(
    r'^(?:"(?P<quoted>[^"]+)"|(?P<plain>.+?\.exe))(?:\s*,\s*-?\d+)?$',
    re.IGNORECASE,
)
_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"


@dataclass(frozen=True, slots=True)
class WindowsInstalledAppRegistryEntry:
    """One Windows uninstall-inventory entry with a stable source identity."""

    source_id: str
    values: dict[str, object]


class WindowsInstalledAppRegistry(Protocol):
    """Read-only source of Windows installed-application entries."""

    def entries(
        self,
        context: GameDiscoveryContext,
    ) -> tuple[WindowsInstalledAppRegistryEntry, ...]: ...


class LocalWindowsInstalledAppRegistry:
    """Enumerate Windows uninstall inventory without mutating launcher or game state."""

    def entries(
        self,
        context: GameDiscoveryContext,
    ) -> tuple[WindowsInstalledAppRegistryEntry, ...]:
        if context.is_cancelled() or sys.platform != "win32":
            return ()
        try:
            import winreg
        except ImportError:
            return ()

        locations = (
            ("hklm64", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
            ("hklm32", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
            ("hkcu64", winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_64KEY),
            ("hkcu32", winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_32KEY),
        )
        discovered: list[WindowsInstalledAppRegistryEntry] = []
        seen: set[str] = set()
        for label, hive, view in locations:
            if context.is_cancelled():
                break
            try:
                with winreg.OpenKey(
                    hive, _UNINSTALL_KEY, 0, winreg.KEY_READ | view
                ) as root:
                    subkey_count = winreg.QueryInfoKey(root)[0]
                    for index in range(subkey_count):
                        if context.is_cancelled():
                            break
                        try:
                            subkey_name = winreg.EnumKey(root, index)
                            with winreg.OpenKey(root, subkey_name) as subkey:
                                values = _read_registry_values(subkey, winreg)
                        except OSError:
                            continue
                        source_id = f"{label}:{subkey_name}"
                        dedupe_key = source_id.casefold()
                        if dedupe_key in seen:
                            continue
                        seen.add(dedupe_key)
                        discovered.append(
                            WindowsInstalledAppRegistryEntry(source_id, values)
                        )
            except OSError:
                continue
        return tuple(discovered)


class WindowsInstalledAppFileSystem(Protocol):
    """Filesystem checks used to validate installed-application evidence."""

    def is_dir(self, path: str, context: GameDiscoveryContext) -> bool: ...

    def is_file(self, path: str, context: GameDiscoveryContext) -> bool: ...


class LocalWindowsInstalledAppFileSystem:
    """Local filesystem implementation for installed-application validation."""

    def is_dir(self, path: str, context: GameDiscoveryContext) -> bool:
        return not context.is_cancelled() and Path(path).is_dir()

    def is_file(self, path: str, context: GameDiscoveryContext) -> bool:
        return not context.is_cancelled() and Path(path).is_file()


class InstalledExecutableGame(Protocol):
    """Normalized installed-game fields consumed by snapshot construction."""

    @property
    def provider_game_id(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def install_directory(self) -> str: ...

    @property
    def executable_path(self) -> str | None: ...


def optional_installed_app_text(values: dict[str, object], key: str) -> str | None:
    """Return a clean optional string from an installed-app entry."""

    value = values.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or any(character in stripped for character in ("\x00", "\r", "\n")):
        return None
    return stripped


def optional_installed_app_int(values: dict[str, object], key: str) -> int | None:
    """Return an optional integer without accepting booleans or coercion."""

    value = values.get(key)
    if type(value) is int:
        return value
    return None


def required_installed_app_text(
    values: dict[str, object],
    key: str,
    *,
    max_length: int,
) -> str:
    """Return a bounded required installed-app string."""

    value = optional_installed_app_text(values, key)
    if value is None:
        raise ValueError(f"installed-app entry is missing valid {key}")
    if len(value) > max_length:
        raise ValueError(f"installed-app {key} exceeds {max_length} characters")
    return value


def required_absolute_install_directory(values: dict[str, object], key: str) -> str:
    """Return a required absolute Windows installation directory."""

    value = required_installed_app_text(values, key, max_length=32_767)
    path = PureWindowsPath(value)
    if not path.is_absolute():
        raise ValueError(f"installed-app {key} must be an absolute Windows path")
    return str(path)


def stable_installed_game_id(title: str, install_directory: str) -> str:
    """Derive a stable provider-local ID without exposing source paths."""

    identity_seed = f"{title.casefold()}\0{install_directory.casefold()}".encode()
    return hashlib.sha256(identity_seed).hexdigest()[:32]


def resolve_display_icon_executable(
    values: dict[str, object],
    install_directory: str,
    filesystem: WindowsInstalledAppFileSystem,
    context: GameDiscoveryContext,
    *,
    blocked_executable_names: set[str] | frozenset[str],
) -> str | None:
    """Resolve a safe in-install executable from a DisplayIcon value."""

    raw = optional_installed_app_text(values, "DisplayIcon")
    if raw is None:
        return None
    expanded = os.path.expandvars(raw)
    match = _DISPLAY_ICON_EXE.fullmatch(expanded.strip())
    if match is None:
        return None
    candidate = match.group("quoted") or match.group("plain")
    if candidate is None:
        return None
    path = PureWindowsPath(candidate.strip())
    if not path.is_absolute() or path.suffix.casefold() != ".exe":
        return None
    normalized_name = path.name.casefold()
    if normalized_name in blocked_executable_names or normalized_name.startswith(
        "unins"
    ):
        return None
    if not _path_is_within(path, PureWindowsPath(install_directory)):
        return None
    candidate_text = str(path)
    if not filesystem.is_file(candidate_text, context):
        return None
    return candidate_text


def build_installed_executable_snapshot(
    *,
    descriptor: GameProviderDescriptor,
    installed_games: tuple[InstalledExecutableGame, ...],
    warnings: tuple[str, ...],
    context: GameDiscoveryContext,
) -> GameProviderSnapshot:
    """Convert validated installed games into a provider snapshot."""

    games: list[GameRecord] = []
    for installed in installed_games:
        executable = installed.executable_path
        launch_target = None
        icon = None
        if executable is not None:
            launch_target = GameLaunchTarget(
                GameLaunchTargetKind.EXECUTABLE,
                executable,
                working_directory=installed.install_directory,
            )
            icon = GameIconReference(GameIconKind.EXECUTABLE, executable)
        games.append(
            GameRecord(
                identity=GameIdentity(
                    descriptor.provider_id, installed.provider_game_id
                ),
                title=installed.title,
                is_installed=True,
                is_available=launch_target is not None,
                launch_target=launch_target,
                install_directory=installed.install_directory,
                icon=icon,
                recency=None,
            )
        )
    return GameProviderSnapshot(
        provider=descriptor,
        games=tuple(games),
        complete=not context.is_cancelled(),
        warnings=warnings,
    )


def windows_file_exists(path: str) -> bool:
    """Return whether a path identifies a local regular file."""

    return Path(path).is_file()


def _path_is_within(path: PureWindowsPath, root: PureWindowsPath) -> bool:
    path_parts = tuple(part.casefold() for part in path.parts)
    root_parts = tuple(part.casefold() for part in root.parts)
    return (
        len(path_parts) > len(root_parts)
        and path_parts[: len(root_parts)] == root_parts
    )


def _read_registry_values(key: Any, winreg_module: Any) -> dict[str, object]:
    values: dict[str, object] = {}
    value_count = winreg_module.QueryInfoKey(key)[1]
    for index in range(value_count):
        try:
            name, value, _ = winreg_module.EnumValue(key, index)
        except OSError:
            continue
        if isinstance(name, str):
            values[name] = value
    return values


__all__ = [
    "InstalledExecutableGame",
    "LocalWindowsInstalledAppFileSystem",
    "LocalWindowsInstalledAppRegistry",
    "WindowsInstalledAppFileSystem",
    "WindowsInstalledAppRegistry",
    "WindowsInstalledAppRegistryEntry",
    "build_installed_executable_snapshot",
    "optional_installed_app_int",
    "optional_installed_app_text",
    "required_absolute_install_directory",
    "required_installed_app_text",
    "resolve_display_icon_executable",
    "stable_installed_game_id",
    "windows_file_exists",
]
