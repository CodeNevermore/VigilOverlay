"""Atomic, typed, versioned application configuration."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from vigil_overlay.core.controller_shortcuts import ControllerShortcutBinding
from vigil_overlay.core.errors import ConfigError
from vigil_overlay.core.file_io import atomic_write_text
from vigil_overlay.core.hotkeys import parse_hotkey_combination
from vigil_overlay.core.version import CONFIG_SCHEMA_VERSION

_WIDGET_ID: Final = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_DEFAULT_WIDGET_IDS: Final = (
    "home",
    "performance",
    "audio",
    "wifi",
    "display",
    "integrations",
    "settings",
    "widgets",
)


@dataclass(slots=True)
class HotkeySettings:
    """Global overlay hotkey preference."""

    enabled: bool = True
    combination: str = "Ctrl+Alt+Shift+G"


@dataclass(slots=True)
class StartupSettings:
    """Per-user Windows startup preference."""

    start_with_windows: bool = False


@dataclass(slots=True)
class BackgroundSettings:
    """Resident tray and background behavior."""

    run_in_background: bool = True


@dataclass(slots=True)
class NavigationSettings:
    """Persisted navigation position."""

    selected_widget: str = "home"


@dataclass(slots=True)
class ControllerSettings:
    """Controller routing and Guide-button preferences."""

    guide_button_enabled: bool = True
    allow_mouse_navigation_while_controller_connected: bool = False
    focus_preserving_controller_isolation_enabled: bool = False
    focus_preserving_controller_isolation_preference_initialized: bool = False
    shortcut_controls: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WidgetStripSettings:
    """Enabled widgets and their presentation order."""

    enabled_widget_ids: list[str] = field(default_factory=lambda: list(_DEFAULT_WIDGET_IDS))
    widget_order: list[str] = field(default_factory=lambda: list(_DEFAULT_WIDGET_IDS))


@dataclass(slots=True)
class PluginPolicySettings:
    """Resource and trust limits reserved for host-managed widgets."""

    allow_unsigned_manual_install: bool = True
    install_disabled_by_default: bool = True
    max_message_bytes: int = 256 * 1024
    max_messages_per_second: int = 30


@dataclass(slots=True)
class WindowSettings:
    """Persisted overlay size, position, and z-order preference."""

    width: int = 720
    height: int = 460
    x: int | None = None
    y: int | None = None
    always_on_top: bool = True


@dataclass(slots=True)
class AppConfig:
    """Complete validated application configuration."""

    schema_version: int = CONFIG_SCHEMA_VERSION
    theme: str = "dark"
    log_level: str = "INFO"
    hotkey: HotkeySettings = field(default_factory=HotkeySettings)
    startup: StartupSettings = field(default_factory=StartupSettings)
    background: BackgroundSettings = field(default_factory=BackgroundSettings)
    navigation: NavigationSettings = field(default_factory=NavigationSettings)
    controller: ControllerSettings = field(default_factory=ControllerSettings)
    widgets: WidgetStripSettings = field(default_factory=WidgetStripSettings)
    plugins: PluginPolicySettings = field(default_factory=PluginPolicySettings)
    window: WindowSettings = field(default_factory=WindowSettings)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AppConfig:
        """Migrate and validate an untrusted configuration mapping."""

        migrated = _migrate_config(dict(raw))
        _require_exact_keys(
            migrated,
            required={
                "schema_version",
                "theme",
                "log_level",
                "hotkey",
                "startup",
                "background",
                "navigation",
                "controller",
                "widgets",
                "plugins",
                "window",
            },
            context="application configuration",
        )

        theme = _require_string(migrated["theme"], "theme")
        if theme not in {"system", "light", "dark"}:
            raise ConfigError("theme must be one of: system, light, dark")

        log_level = _require_string(migrated["log_level"], "log_level").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("log_level is unsupported")

        hotkey_raw = _require_mapping(migrated["hotkey"], "hotkey")
        _require_exact_keys(hotkey_raw, {"enabled", "combination"}, "hotkey")
        hotkey_combination = _require_string(hotkey_raw["combination"], "hotkey.combination")
        try:
            hotkey_combination = parse_hotkey_combination(hotkey_combination).canonical
        except ValueError as exc:
            raise ConfigError(f"hotkey.combination is invalid: {exc}") from exc
        hotkey = HotkeySettings(
            enabled=_require_bool(hotkey_raw["enabled"], "hotkey.enabled"),
            combination=hotkey_combination,
        )

        startup_raw = _require_mapping(migrated["startup"], "startup")
        _require_exact_keys(startup_raw, {"start_with_windows"}, "startup")
        startup = StartupSettings(
            start_with_windows=_require_bool(
                startup_raw["start_with_windows"], "startup.start_with_windows"
            )
        )

        background_raw = _require_mapping(migrated["background"], "background")
        _require_exact_keys(background_raw, {"run_in_background"}, "background")
        background = BackgroundSettings(
            run_in_background=_require_bool(
                background_raw["run_in_background"], "background.run_in_background"
            )
        )

        navigation_raw = _require_mapping(migrated["navigation"], "navigation")
        _require_exact_keys(navigation_raw, {"selected_widget"}, "navigation")
        selected_widget = _require_widget_id(
            navigation_raw["selected_widget"], "navigation.selected_widget"
        )

        controller_raw = _require_mapping(migrated["controller"], "controller")
        _require_exact_keys(
            controller_raw,
            {
                "guide_button_enabled",
                "allow_mouse_navigation_while_controller_connected",
                "focus_preserving_controller_isolation_enabled",
                "focus_preserving_controller_isolation_preference_initialized",
                "shortcut_controls",
            },
            "controller",
        )
        try:
            shortcut_binding = ControllerShortcutBinding.from_tokens(
                controller_raw["shortcut_controls"]
            )
        except ValueError as exc:
            raise ConfigError(f"controller.shortcut_controls is invalid: {exc}") from exc
        controller = ControllerSettings(
            guide_button_enabled=_require_bool(
                controller_raw["guide_button_enabled"],
                "controller.guide_button_enabled",
            ),
            allow_mouse_navigation_while_controller_connected=_require_bool(
                controller_raw["allow_mouse_navigation_while_controller_connected"],
                "controller.allow_mouse_navigation_while_controller_connected",
            ),
            focus_preserving_controller_isolation_enabled=_require_bool(
                controller_raw["focus_preserving_controller_isolation_enabled"],
                "controller.focus_preserving_controller_isolation_enabled",
            ),
            focus_preserving_controller_isolation_preference_initialized=_require_bool(
                controller_raw["focus_preserving_controller_isolation_preference_initialized"],
                "controller.focus_preserving_controller_isolation_preference_initialized",
            ),
            shortcut_controls=list(shortcut_binding.controls),
        )

        widgets_raw = _require_mapping(migrated["widgets"], "widgets")
        _require_exact_keys(
            widgets_raw,
            {"enabled_widget_ids", "widget_order"},
            "widgets",
        )
        enabled_widget_ids = _require_widget_id_list(
            widgets_raw["enabled_widget_ids"], "widgets.enabled_widget_ids"
        )
        widget_order = _require_widget_id_list(widgets_raw["widget_order"], "widgets.widget_order")
        if "home" not in enabled_widget_ids or "settings" not in enabled_widget_ids:
            raise ConfigError("widgets.enabled_widget_ids must include home and settings")
        if selected_widget not in enabled_widget_ids:
            selected_widget = "home"

        plugins_raw = _require_mapping(migrated["plugins"], "plugins")
        _require_exact_keys(
            plugins_raw,
            {
                "allow_unsigned_manual_install",
                "install_disabled_by_default",
                "max_message_bytes",
                "max_messages_per_second",
            },
            "plugins",
        )
        max_message_bytes = _require_int(
            plugins_raw["max_message_bytes"], "plugins.max_message_bytes"
        )
        if not 4_096 <= max_message_bytes <= 4 * 1024 * 1024:
            raise ConfigError("plugins.max_message_bytes must be between 4096 and 4194304")
        max_messages = _require_int(
            plugins_raw["max_messages_per_second"], "plugins.max_messages_per_second"
        )
        if not 1 <= max_messages <= 120:
            raise ConfigError("plugins.max_messages_per_second must be between 1 and 120")

        window_raw = _require_mapping(migrated["window"], "window")
        _require_exact_keys(
            window_raw,
            {"width", "height", "x", "y", "always_on_top"},
            "window",
        )
        width = _require_int(window_raw["width"], "window.width")
        height = _require_int(window_raw["height"], "window.height")
        if not 520 <= width <= 7680:
            raise ConfigError("window.width must be between 520 and 7680")
        if not 330 <= height <= 4320:
            raise ConfigError("window.height must be between 330 and 4320")

        return cls(
            schema_version=CONFIG_SCHEMA_VERSION,
            theme=theme,
            log_level=log_level,
            hotkey=hotkey,
            startup=startup,
            background=background,
            navigation=NavigationSettings(selected_widget=selected_widget),
            controller=controller,
            widgets=WidgetStripSettings(
                enabled_widget_ids=enabled_widget_ids,
                widget_order=widget_order,
            ),
            plugins=PluginPolicySettings(
                allow_unsigned_manual_install=_require_bool(
                    plugins_raw["allow_unsigned_manual_install"],
                    "plugins.allow_unsigned_manual_install",
                ),
                install_disabled_by_default=_require_bool(
                    plugins_raw["install_disabled_by_default"],
                    "plugins.install_disabled_by_default",
                ),
                max_message_bytes=max_message_bytes,
                max_messages_per_second=max_messages,
            ),
            window=WindowSettings(
                width=width,
                height=height,
                x=_require_optional_int(window_raw["x"], "window.x"),
                y=_require_optional_int(window_raw["y"], "window.y"),
                always_on_top=_require_bool(window_raw["always_on_top"], "window.always_on_top"),
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable configuration mapping."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class ConfigLoadNotice:
    """Non-fatal settings recovery information for startup diagnostics."""

    reason: str
    preserved_path: Path | None
    settings_persisted: bool


Migration = Callable[[dict[str, Any]], dict[str, Any]]


def _migrate_v1_to_v2(raw: dict[str, Any]) -> dict[str, Any]:
    raw["window"] = asdict(WindowSettings())
    raw["schema_version"] = 2
    return raw


def _migrate_v2_to_v3(raw: dict[str, Any]) -> dict[str, Any]:
    raw["navigation"] = {"selected_tab": "home"}
    raw["schema_version"] = 3
    return raw


def _migrate_v3_to_v4(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("theme") == "system":
        raw["theme"] = "dark"
    raw["schema_version"] = 4
    return raw


def _migrate_v4_to_v5(raw: dict[str, Any]) -> dict[str, Any]:
    navigation = raw.get("navigation")
    selected_tab = "home"
    if isinstance(navigation, dict):
        candidate = navigation.get("selected_tab")
        if isinstance(candidate, str):
            selected_tab = candidate.casefold()
    selected_widget = {
        "performance": "performance",
        "settings": "settings",
        "plugins": "settings",
        "apps": "home",
        "running": "home",
        "home": "home",
    }.get(selected_tab, "home")
    raw["navigation"] = {"selected_widget": selected_widget}
    raw["widgets"] = asdict(WidgetStripSettings())
    raw["schema_version"] = 5
    return raw


def _migrate_v5_to_v6(raw: dict[str, Any]) -> dict[str, Any]:
    raw["controller"] = asdict(ControllerSettings())
    raw["schema_version"] = 6
    return raw


def _migrate_v6_to_v7(raw: dict[str, Any]) -> dict[str, Any]:
    widgets = raw.get("widgets")
    if isinstance(widgets, dict):
        for key in ("enabled_widget_ids", "widget_order"):
            values = widgets.get(key)
            if isinstance(values, list) and "integrations" not in values:
                try:
                    index = values.index("settings")
                except ValueError:
                    values.append("integrations")
                else:
                    values.insert(index, "integrations")
    raw["schema_version"] = 7
    return raw


def _migrate_v7_to_v8(raw: dict[str, Any]) -> dict[str, Any]:
    widgets = raw.get("widgets")
    if isinstance(widgets, dict):
        for key in ("enabled_widget_ids", "widget_order"):
            values = widgets.get(key)
            if isinstance(values, list) and "audio" not in values:
                try:
                    index = values.index("display")
                except ValueError:
                    values.append("audio")
                else:
                    values.insert(index, "audio")
    raw["schema_version"] = 8
    return raw


def _migrate_v8_to_v9(raw: dict[str, Any]) -> dict[str, Any]:
    widgets = raw.get("widgets")
    if isinstance(widgets, dict):
        for key in ("enabled_widget_ids", "widget_order"):
            values = widgets.get(key)
            if isinstance(values, list) and "wifi" not in values:
                try:
                    index = values.index("display")
                except ValueError:
                    values.append("wifi")
                else:
                    values.insert(index, "wifi")
    raw["schema_version"] = 9
    return raw


def _migrate_v9_to_v10(raw: dict[str, Any]) -> dict[str, Any]:
    raw["startup"] = asdict(StartupSettings())
    raw["schema_version"] = 10
    return raw


def _migrate_v10_to_v11(raw: dict[str, Any]) -> dict[str, Any]:
    raw["background"] = asdict(BackgroundSettings())
    raw["schema_version"] = 11
    return raw


def _migrate_v11_to_v12(raw: dict[str, Any]) -> dict[str, Any]:
    controller = raw.get("controller")
    if not isinstance(controller, dict):
        controller = {}
        raw["controller"] = controller
    controller.setdefault("disable_mouse_while_connected", True)
    raw["schema_version"] = 12
    return raw


def _migrate_v12_to_v13(raw: dict[str, Any]) -> dict[str, Any]:
    controller = raw.get("controller")
    if not isinstance(controller, dict):
        controller = {}
        raw["controller"] = controller
    previous = controller.pop("disable_mouse_while_connected", True)
    if type(previous) is bool:
        controller["allow_mouse_navigation_while_controller_connected"] = not previous
    else:
        # Preserve invalid legacy input so normal strict validation rejects it.
        controller["allow_mouse_navigation_while_controller_connected"] = previous
    raw["schema_version"] = 13
    return raw


def _migrate_v13_to_v14(raw: dict[str, Any]) -> dict[str, Any]:
    controller = raw.get("controller")
    if not isinstance(controller, dict):
        controller = {}
        raw["controller"] = controller
    controller.setdefault("shortcut_controls", [])
    raw["schema_version"] = 14
    return raw


def _migrate_v14_to_v15(raw: dict[str, Any]) -> dict[str, Any]:
    controller = raw.get("controller")
    if not isinstance(controller, dict):
        controller = {}
        raw["controller"] = controller
    controller.setdefault("focus_preserving_controller_isolation_enabled", False)
    raw["schema_version"] = 15
    return raw


def _migrate_v15_to_v16(raw: dict[str, Any]) -> dict[str, Any]:
    controller = raw.get("controller")
    if not isinstance(controller, dict):
        controller = {}
        raw["controller"] = controller
    controller.setdefault(
        "focus_preserving_controller_isolation_preference_initialized",
        False,
    )
    raw["schema_version"] = 16
    return raw


_MIGRATIONS: dict[int, Migration] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
    4: _migrate_v4_to_v5,
    5: _migrate_v5_to_v6,
    6: _migrate_v6_to_v7,
    7: _migrate_v7_to_v8,
    8: _migrate_v8_to_v9,
    9: _migrate_v9_to_v10,
    10: _migrate_v10_to_v11,
    11: _migrate_v11_to_v12,
    12: _migrate_v12_to_v13,
    13: _migrate_v13_to_v14,
    14: _migrate_v14_to_v15,
    15: _migrate_v15_to_v16,
}


def apply_controller_isolation_install_default(
    config: AppConfig,
    *,
    hidhide_available: bool,
) -> bool:
    """Enable the one-time default after an installed build detects HidHide."""

    controller = config.controller
    if (
        not hidhide_available
        or controller.focus_preserving_controller_isolation_preference_initialized
    ):
        return False
    controller.focus_preserving_controller_isolation_enabled = True
    controller.focus_preserving_controller_isolation_preference_initialized = True
    return True


def load_config(path: Path) -> AppConfig:
    """Load usable settings, recovering invalid or unavailable files to defaults."""

    config, _notice = load_config_with_notice(path)
    return config


def load_config_with_notice(path: Path) -> tuple[AppConfig, ConfigLoadNotice | None]:
    """Load settings without allowing settings failures to block application startup.

    Invalid settings are moved aside before defaults are written. If preservation or
    persistence is unavailable, defaults remain in memory and the original file is never
    overwritten.
    """

    try:
        exists = path.exists()
    except OSError as exc:
        return AppConfig(), ConfigLoadNotice(
            f"Could not inspect settings; using in-memory defaults: {exc}",
            None,
            False,
        )

    if not exists:
        config = AppConfig()
        try:
            save_config(path, config)
        except ConfigError as exc:
            return config, ConfigLoadNotice(str(exc), None, False)
        return config, None

    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _recover_invalid_config(path, exc)

    if not isinstance(raw, dict):
        return _recover_invalid_config(
            path, ConfigError("Application configuration root must be an object")
        )
    original_schema_version = raw.get("schema_version")
    try:
        config = AppConfig.from_dict(raw)
    except ConfigError as exc:
        return _recover_invalid_config(path, exc)
    if original_schema_version != CONFIG_SCHEMA_VERSION:
        try:
            save_config(path, config)
        except ConfigError as exc:
            return config, ConfigLoadNotice(str(exc), None, False)
    return config, None


def save_config(path: Path, config: AppConfig) -> None:
    """Persist configuration with a same-directory atomic replacement."""

    payload = json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, payload, temporary_suffix=".tmp", fsync=True)
    except OSError as exc:
        raise ConfigError(f"Could not save configuration to {path}") from exc


def preserve_invalid_config(path: Path) -> Path:
    """Atomically preserve invalid settings before any replacement is attempted."""

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = path.with_name(f"{path.stem}.invalid-{timestamp}{path.suffix}")
    try:
        os.replace(path, destination)
    except OSError as exc:
        raise ConfigError(f"Could not preserve invalid configuration: {path}") from exc
    return destination


def _recover_invalid_config(
    path: Path,
    failure: Exception,
) -> tuple[AppConfig, ConfigLoadNotice]:
    defaults = AppConfig()
    reason = f"Invalid settings were reset to defaults: {failure}"
    try:
        preserved = preserve_invalid_config(path)
    except ConfigError as preserve_error:
        return defaults, ConfigLoadNotice(
            f"{reason}; {preserve_error}. The original file was not overwritten.",
            None,
            False,
        )

    try:
        save_config(path, defaults)
    except ConfigError as save_error:
        return defaults, ConfigLoadNotice(
            f"{reason}; defaults could not be persisted: {save_error}",
            preserved,
            False,
        )
    return defaults, ConfigLoadNotice(reason, preserved, True)


def _migrate_config(raw: dict[str, Any]) -> dict[str, Any]:
    version = raw.get("schema_version")
    if type(version) is not int:
        raise ConfigError("schema_version must be an integer")
    if version > CONFIG_SCHEMA_VERSION:
        raise ConfigError(
            f"Configuration schema {version} is newer than supported schema {CONFIG_SCHEMA_VERSION}"
        )
    while version < CONFIG_SCHEMA_VERSION:
        migration = _MIGRATIONS.get(version)
        if migration is None:
            raise ConfigError(f"No migration exists from configuration schema {version}")
        raw = migration(raw)
        version = raw.get("schema_version")
        if type(version) is not int:
            raise ConfigError("Migration produced an invalid schema_version")
    return raw


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be an object")
    return value


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _require_widget_id(value: Any, name: str) -> str:
    normalized = _require_string(value, name).casefold()
    if not _WIDGET_ID.fullmatch(normalized):
        raise ConfigError(f"{name} must be a lowercase widget identifier")
    return normalized


def _require_widget_id_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be an array")
    if len(value) > 128:
        raise ConfigError(f"{name} cannot contain more than 128 entries")
    result = [_require_widget_id(item, f"{name}[{index}]") for index, item in enumerate(value)]
    if len(result) != len(set(result)):
        raise ConfigError(f"{name} must not contain duplicate widget IDs")
    return result


def _require_bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{name} must be a boolean")
    return value


def _require_int(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ConfigError(f"{name} must be an integer")
    return value


def _require_optional_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _require_int(value, name)


def _require_exact_keys(mapping: dict[str, Any], required: set[str], context: str) -> None:
    missing = required - mapping.keys()
    unknown = mapping.keys() - required
    if missing:
        raise ConfigError(f"{context} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ConfigError(f"{context} contains unknown fields: {', '.join(sorted(unknown))}")
