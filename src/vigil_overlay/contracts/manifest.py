"""Compiled widget manifest contract and validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Final

from vigil_overlay.core.errors import ManifestValidationError

_WIDGET_ID: Final = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)+$")
_SEMVER: Final = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$"
)
_API_VERSION: Final = re.compile(r"^[1-9]\d*\.\d+$")

KNOWN_PERMISSIONS: Final[frozenset[str]] = frozenset(
    {
        "notifications",
        "widget_settings",
        "system_stats",
        "hardware_temps",
        "process_list",
        "launch_apps",
        "audio_status",
        "network",
        "file_read_plugin_data",
        "file_write_plugin_data",
    }
)
KNOWN_RUNTIME_FORMATS: Final = frozenset({"nuitka-standalone"})
KNOWN_PLATFORMS: Final = frozenset({"windows"})
KNOWN_ARCHITECTURES: Final = frozenset({"x86_64"})
KNOWN_RENDER_MODES: Final = frozenset({"declarative-v1"})


@dataclass(frozen=True, slots=True)
class WidgetManifest:
    """Identity, compatibility, runtime, and permission metadata for one widget."""

    widget_id: str
    name: str
    version: str
    api_version: str
    component_api_version: str
    minimum_host_version: str
    maximum_host_version: str | None
    platform: str
    architecture: str
    runtime_format: str
    entry_exe: str
    render_mode: str
    permissions: tuple[str, ...]
    publisher: str
    homepage: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> WidgetManifest:
        """Parse a strict manifest mapping and reject missing or unknown fields."""

        required = {
            "id",
            "name",
            "version",
            "api_version",
            "component_api_version",
            "minimum_host_version",
            "maximum_host_version",
            "platform",
            "architecture",
            "runtime_format",
            "entry_exe",
            "render_mode",
            "permissions",
            "publisher",
            "homepage",
        }
        missing = required - raw.keys()
        unknown = raw.keys() - required
        if missing:
            raise ManifestValidationError(
                f"Manifest is missing: {', '.join(sorted(missing))}"
            )
        if unknown:
            raise ManifestValidationError(
                f"Manifest has unknown fields: {', '.join(sorted(unknown))}"
            )

        widget_id = _string(raw["id"], "id", max_length=160)
        if not _WIDGET_ID.fullmatch(widget_id):
            raise ManifestValidationError(
                "id must be a reverse-domain-style lowercase identifier, e.g. com.example.widget"
            )

        version = _semver(raw["version"], "version")
        minimum_host = _semver(raw["minimum_host_version"], "minimum_host_version")
        maximum_host_raw = raw["maximum_host_version"]
        maximum_host = (
            None
            if maximum_host_raw is None
            else _semver(maximum_host_raw, "maximum_host_version")
        )

        api_version = _api_version(raw["api_version"], "api_version")
        component_api_version = _api_version(
            raw["component_api_version"], "component_api_version"
        )

        platform = _choice(raw["platform"], "platform", KNOWN_PLATFORMS)
        architecture = _choice(raw["architecture"], "architecture", KNOWN_ARCHITECTURES)
        runtime_format = _choice(
            raw["runtime_format"], "runtime_format", KNOWN_RUNTIME_FORMATS
        )
        render_mode = _choice(raw["render_mode"], "render_mode", KNOWN_RENDER_MODES)
        entry_exe = _safe_relative_path(raw["entry_exe"], "entry_exe")
        if not entry_exe.lower().endswith(".exe"):
            raise ManifestValidationError(
                "entry_exe must reference a Windows executable"
            )

        permissions_raw = raw["permissions"]
        if not isinstance(permissions_raw, list):
            raise ManifestValidationError("permissions must be an array")
        permissions: list[str] = []
        for index, permission_raw in enumerate(permissions_raw):
            permission = _string(permission_raw, f"permissions[{index}]", max_length=80)
            if permission not in KNOWN_PERMISSIONS:
                raise ManifestValidationError(f"Unknown permission: {permission}")
            if permission in permissions:
                raise ManifestValidationError(f"Duplicate permission: {permission}")
            permissions.append(permission)

        homepage_raw = raw["homepage"]
        homepage = (
            None if homepage_raw is None else _string(homepage_raw, "homepage", 2048)
        )

        return cls(
            widget_id=widget_id,
            name=_string(raw["name"], "name", max_length=120),
            version=version,
            api_version=api_version,
            component_api_version=component_api_version,
            minimum_host_version=minimum_host,
            maximum_host_version=maximum_host,
            platform=platform,
            architecture=architecture,
            runtime_format=runtime_format,
            entry_exe=entry_exe,
            render_mode=render_mode,
            permissions=tuple(permissions),
            publisher=_string(raw["publisher"], "publisher", max_length=160),
            homepage=homepage,
        )


def _string(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ManifestValidationError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ManifestValidationError(f"{field} must not be empty")
    if len(normalized) > max_length:
        raise ManifestValidationError(f"{field} exceeds {max_length} characters")
    if "\x00" in normalized:
        raise ManifestValidationError(f"{field} contains a null character")
    return normalized


def _semver(value: Any, field: str) -> str:
    normalized = _string(value, field, 80)
    if not _SEMVER.fullmatch(normalized):
        raise ManifestValidationError(f"{field} must be semantic version text")
    return normalized


def _api_version(value: Any, field: str) -> str:
    normalized = _string(value, field, 20)
    if not _API_VERSION.fullmatch(normalized):
        raise ManifestValidationError(f"{field} must use major.minor form")
    return normalized


def _choice(value: Any, field: str, choices: frozenset[str]) -> str:
    normalized = _string(value, field, 80)
    if normalized not in choices:
        raise ManifestValidationError(
            f"{field} must be one of: {', '.join(sorted(choices))}"
        )
    return normalized


def _safe_relative_path(value: Any, field: str) -> str:
    normalized = _string(value, field, 512).replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts:
        raise ManifestValidationError(f"{field} must be a relative path")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ManifestValidationError(f"{field} contains an unsafe path segment")
    if ":" in path.parts[0]:
        raise ManifestValidationError(f"{field} must not contain a drive prefix")
    return path.as_posix()
