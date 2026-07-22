"""Centralized, compiled-safe application path resolution."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from vigil_overlay.core.errors import PathResolutionError
from vigil_overlay.core.runtime import is_packaged_build, packaged_executable_path

APP_FOLDER_NAME = "VigilOverlay"


def _resolve_env_path(name: str, fallback: Path) -> Path:
    raw = os.environ.get(name)
    if not raw:
        return fallback
    return Path(raw).expanduser().resolve()


@dataclass(frozen=True, slots=True)
class ApplicationPaths:
    """All filesystem locations used by the application.

    No module outside this class should derive AppData, cache, log, resource,
    plugin, or helper paths independently.
    """

    install_root: Path
    resource_root: Path
    helper_root: Path
    user_config_root: Path
    user_data_root: Path
    plugin_root: Path
    cache_root: Path
    log_root: Path

    @classmethod
    def discover(cls) -> ApplicationPaths:
        """Resolve install, resource, and per-user storage roots for this process."""

        home = Path.home().resolve()
        roaming = _resolve_env_path("APPDATA", home / ".config")
        local = _resolve_env_path("LOCALAPPDATA", home / ".local" / "share")

        if is_packaged_build():
            packaged = packaged_executable_path()
            executable = (
                packaged
                if packaged is not None
                else Path(sys.argv[0]).expanduser().resolve()
            )
            install_root = executable.parent
            resource_root = install_root / "vigil_overlay" / "resources"
        else:
            package_root = Path(__file__).resolve().parents[1]
            install_root = package_root.parents[1]
            resource_root = package_root / "resources"

        override = os.environ.get("VIGIL_OVERLAY_RESOURCE_ROOT")
        if override:
            resource_root = Path(override).expanduser().resolve()

        paths = cls(
            install_root=install_root,
            resource_root=resource_root,
            helper_root=install_root / "helpers",
            user_config_root=roaming / APP_FOLDER_NAME / "config",
            user_data_root=roaming / APP_FOLDER_NAME / "data",
            plugin_root=roaming / APP_FOLDER_NAME / "plugins",
            cache_root=local / APP_FOLDER_NAME / "cache",
            log_root=local / APP_FOLDER_NAME / "logs",
        )
        paths._validate()
        return paths

    def ensure_user_directories(self) -> None:
        """Create the writable per-user directories used by Vigil services."""

        for path in (
            self.user_config_root,
            self.user_data_root,
            self.plugin_root,
            self.cache_root,
            self.log_root,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def _validate(self) -> None:
        for field_name, path in self.as_dict().items():
            if not path.is_absolute():
                raise PathResolutionError(f"{field_name} must be absolute: {path}")

    def as_dict(self) -> dict[str, Path]:
        """Return all path fields keyed by their stable diagnostic names."""

        return {
            "install_root": self.install_root,
            "resource_root": self.resource_root,
            "helper_root": self.helper_root,
            "user_config_root": self.user_config_root,
            "user_data_root": self.user_data_root,
            "plugin_root": self.plugin_root,
            "cache_root": self.cache_root,
            "log_root": self.log_root,
        }
