"""Consumer-facing integration detection and Playnite bridge lifecycle management."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import psutil  # type: ignore[import-untyped]

from vigil_overlay.contracts.games import GameDiscoveryContext
from vigil_overlay.core.file_io import atomic_copy_file, atomic_write_json, sha256_file
from vigil_overlay.core.paths import ApplicationPaths
from vigil_overlay.providers.battlenet.filesystem import LocalBattleNetFileSystem
from vigil_overlay.providers.battlenet.installed_apps import BattleNetInstalledAppScanner
from vigil_overlay.providers.battlenet.registry import LocalBattleNetRegistry
from vigil_overlay.providers.ea.filesystem import LocalEAFileSystem
from vigil_overlay.providers.ea.installed_apps import EAInstalledAppScanner
from vigil_overlay.providers.ea.registry import LocalEARegistry
from vigil_overlay.providers.epic.filesystem import LocalEpicFileSystem
from vigil_overlay.providers.epic.installation import EpicManifestInstallationLocator
from vigil_overlay.providers.gog.filesystem import LocalGOGFileSystem
from vigil_overlay.providers.gog.installed_games import GOGInstalledGameScanner
from vigil_overlay.providers.gog.registry import LocalGOGRegistry
from vigil_overlay.providers.playnite import (
    PLAYNITE_BRIDGE_FILENAME,
    PLAYNITE_REFRESH_REQUEST_FILENAME,
)
from vigil_overlay.providers.steam.filesystem import LocalSteamFileSystem
from vigil_overlay.providers.steam.installation import SteamInstallationLocator
from vigil_overlay.providers.ubisoft.filesystem import LocalUbisoftFileSystem
from vigil_overlay.providers.ubisoft.installed_apps import UbisoftInstalledAppScanner
from vigil_overlay.providers.ubisoft.registry import LocalUbisoftRegistry
from vigil_overlay.providers.xbox.filesystem import LocalXboxFileSystem
from vigil_overlay.providers.xbox.installation import XboxGamesInstallationLocator
from vigil_overlay.services.game_library import AggregatedGameLibrary

_LOGGER = logging.getLogger("vigil_overlay")

PLAYNITE_BRIDGE_EXTENSION_ID = "7c04ef12-67ae-4db7-ae4f-3af7fb227809"
PLAYNITE_BRIDGE_FOLDER_NAME = "VigilOverlayBridge"
PLAYNITE_BRIDGE_DLL_NAME = "VigilOverlayBridge.dll"
PLAYNITE_BRIDGE_MANIFEST_NAME = "extension.yaml"
PLAYNITE_BUNDLE_MANIFEST_NAME = "bridge_manifest.json"
_PLAYNITE_PROCESS_NAMES = frozenset(
    {"playnite.desktopapp.exe", "playnite.fullscreenapp.exe", "playnite.exe"}
)


class IntegrationState(StrEnum):
    """Stable UI states exposed by the integration manager."""

    CONNECTED = "connected"
    AVAILABLE = "available"
    NOT_DETECTED = "not_detected"
    READY_TO_CONNECT = "ready_to_connect"
    RESTART_REQUIRED = "restart_required"
    UPDATE_AVAILABLE = "update_available"
    NEEDS_REPAIR = "needs_repair"
    ERROR = "error"
    UNAVAILABLE = "unavailable"
    CHECKING = "checking"


@dataclass(frozen=True, slots=True)
class IntegrationStatus:
    """One integration card's current consumer-facing status."""

    integration_id: str
    label: str
    state: IntegrationState
    status_text: str
    detail: str
    primary_action: str | None = None
    primary_action_label: str | None = None
    game_count: int | None = None
    version: str | None = None


@dataclass(frozen=True, slots=True)
class IntegrationOperationResult:
    """Result of a user-requested integration lifecycle action."""

    succeeded: bool
    message: str
    restart_playnite_required: bool = False
    provider_id: str | None = None
    confirmation_required: bool = False


@dataclass(frozen=True, slots=True)
class _BridgeBundle:
    root: Path
    dll_path: Path
    extension_manifest_path: Path
    version: str
    dll_sha256: str


@dataclass(frozen=True, slots=True)
class _NativeIntegrationSpec:
    integration_id: str
    label: str
    not_detected_detail: str
    connected_detail: str


SteamDetector = Callable[[], bool]
XboxDetector = Callable[[], bool]
EpicDetector = Callable[[], bool]
BattleNetDetector = Callable[[], bool]
EADetector = Callable[[], bool]
UbisoftDetector = Callable[[], bool]
GOGDetector = Callable[[], bool]
PlayniteRunningDetector = Callable[[], bool]
PlayniteTerminator = Callable[[], bool]

_NATIVE_INTEGRATIONS = (
    _NativeIntegrationSpec(
        "steam",
        "Steam",
        "Install or start Steam and Vigil will discover it automatically.",
        "Native read-only Steam discovery is active.",
    ),
    _NativeIntegrationSpec(
        "xbox",
        "Xbox / Microsoft Store",
        "No accessible local XboxGames installation folder was detected.",
        "Native read-only Xbox PC game discovery is active.",
    ),
    _NativeIntegrationSpec(
        "epic",
        "Epic Games",
        "No local Epic Games Launcher manifest store was detected.",
        "Native read-only Epic Games discovery is active.",
    ),
    _NativeIntegrationSpec(
        "battlenet",
        "Battle.net",
        "No local Battle.net-managed Windows game installation was detected.",
        "Native read-only Battle.net discovery is active.",
    ),
    _NativeIntegrationSpec(
        "ea",
        "EA app",
        "No local EA app-managed Windows game installation was detected.",
        "Native read-only EA app discovery is active.",
    ),
    _NativeIntegrationSpec(
        "ubisoft",
        "Ubisoft Connect",
        "No local Ubisoft Connect-managed Windows game installation was detected.",
        "Native read-only Ubisoft Connect discovery is active.",
    ),
    _NativeIntegrationSpec(
        "gog",
        "GOG",
        "No local GOG-managed Windows game installation was detected.",
        "Native read-only GOG discovery is active.",
    ),
)


class IntegrationManager:
    """Detect integrations and own safe Playnite bridge install/update/remove operations."""

    def __init__(
        self,
        paths: ApplicationPaths,
        *,
        enabled: bool = True,
        steam_detector: SteamDetector | None = None,
        xbox_detector: XboxDetector | None = None,
        epic_detector: EpicDetector | None = None,
        battlenet_detector: BattleNetDetector | None = None,
        ea_detector: EADetector | None = None,
        ubisoft_detector: UbisoftDetector | None = None,
        gog_detector: GOGDetector | None = None,
        playnite_root_override: Path | None = None,
        bundle_root: Path | None = None,
        playnite_running_detector: PlayniteRunningDetector | None = None,
        playnite_terminator: PlayniteTerminator | None = None,
    ) -> None:
        self._paths = paths
        self._enabled = enabled
        self._native_detectors: dict[str, Callable[[], bool]] = {
            "steam": steam_detector or self._detect_steam,
            "xbox": xbox_detector or self._detect_xbox,
            "epic": epic_detector or self._detect_epic,
            "battlenet": battlenet_detector or self._detect_battlenet,
            "ea": ea_detector or self._detect_ea,
            "ubisoft": ubisoft_detector or self._detect_ubisoft,
            "gog": gog_detector or self._detect_gog,
        }
        self._native_detection_cache: dict[str, bool | None] = {
            spec.integration_id: None for spec in _NATIVE_INTEGRATIONS
        }
        self._playnite_root_override = playnite_root_override
        self._bundle_root = bundle_root
        self._playnite_running_detector = (
            playnite_running_detector or _playnite_is_running
        )
        self._playnite_terminator = playnite_terminator or _terminate_playnite
        self._restart_required = False
        self._last_operation_message: str | None = None

    @property
    def snapshot_path(self) -> Path:
        return self._paths.user_data_root / "games" / PLAYNITE_BRIDGE_FILENAME

    @property
    def refresh_request_path(self) -> Path:
        return self._paths.user_data_root / "games" / PLAYNITE_REFRESH_REQUEST_FILENAME

    def statuses(
        self,
        library: AggregatedGameLibrary | None = None,
    ) -> tuple[IntegrationStatus, ...]:
        """Return native providers, Playnite, and manual integration status in stable UI order."""

        if not self._enabled:
            unavailable = (
                "Integration management is disabled while Vigil is in Safe Mode."
            )
            return (
                *(
                    IntegrationStatus(
                        spec.integration_id,
                        spec.label,
                        IntegrationState.UNAVAILABLE,
                        "Unavailable",
                        unavailable,
                    )
                    for spec in _NATIVE_INTEGRATIONS
                ),
                IntegrationStatus(
                    "playnite",
                    "Playnite",
                    IntegrationState.UNAVAILABLE,
                    "Unavailable",
                    unavailable,
                ),
                IntegrationStatus(
                    "manual",
                    "Manual Games",
                    IntegrationState.UNAVAILABLE,
                    "Unavailable",
                    unavailable,
                ),
            )

        provider_counts, provider_errors = _provider_summary(library)
        native_statuses = tuple(
            self._native_status(spec, provider_counts, provider_errors)
            for spec in _NATIVE_INTEGRATIONS
        )
        return (
            *native_statuses,
            self._safe_playnite_status(provider_counts, provider_errors),
            IntegrationStatus(
                "manual",
                "Manual Games",
                IntegrationState.AVAILABLE,
                "Available",
                "Games added directly to Vigil remain available without another launcher.",
                game_count=provider_counts.get("manual", 0),
            ),
        )

    def initial_statuses(self) -> tuple[IntegrationStatus, ...]:
        """Return an I/O-free placeholder set suitable for synchronous UI construction."""

        if not self._enabled:
            return self.statuses()
        checking = tuple(
            IntegrationStatus(
                spec.integration_id,
                spec.label,
                IntegrationState.CHECKING,
                "Checking…",
                "Vigil is checking local integration state in the background.",
            )
            for spec in _NATIVE_INTEGRATIONS
        )
        return (
            *checking,
            IntegrationStatus(
                "playnite",
                "Playnite",
                IntegrationState.CHECKING,
                "Checking…",
                "Vigil is checking the Playnite bridge in the background.",
            ),
            IntegrationStatus(
                "manual",
                "Manual Games",
                IntegrationState.AVAILABLE,
                "Available",
                "Games added directly to Vigil remain available without another launcher.",
                game_count=0,
            ),
        )

    def perform(self, action: str) -> IntegrationOperationResult:
        """Perform a supported lifecycle action requested by the Integrations widget."""

        if not self._enabled:
            return IntegrationOperationResult(
                False,
                "Integration management is disabled in Safe Mode.",
            )

        install_verb = {
            "playnite_enable": "installed",
            "playnite_update": "updated",
            "playnite_repair": "repaired",
        }.get(action)
        if install_verb is not None:
            return self._install_or_repair_playnite(install_verb)
        if action == "playnite_remove":
            return self._request_playnite_remove()
        if action == "playnite_remove_confirmed":
            return self._confirm_playnite_remove()
        if action.startswith("refresh_"):
            return self._refresh_provider(action.removeprefix("refresh_"))
        return IntegrationOperationResult(
            False, f"Unsupported integration action: {action}"
        )

    def _request_playnite_remove(self) -> IntegrationOperationResult:
        if self._playnite_running_detector():
            return IntegrationOperationResult(
                False,
                "Playnite is running. Close Playnite and uninstall the Vigil bridge? "
                "Unsaved Playnite changes may be lost.",
                confirmation_required=True,
            )
        return self._remove_playnite_bridge()

    def _confirm_playnite_remove(self) -> IntegrationOperationResult:
        if self._playnite_running_detector() and not self._playnite_terminator():
            return IntegrationOperationResult(
                False,
                "Could not close Playnite. The bridge remains installed.",
            )
        return self._remove_playnite_bridge()

    def _refresh_provider(self, provider_id: str) -> IntegrationOperationResult:
        if provider_id == "playnite":
            return self._request_playnite_refresh()
        if provider_id not in self._native_detection_cache:
            return IntegrationOperationResult(
                False, f"Unsupported integration refresh: {provider_id}"
            )
        self._native_detection_cache[provider_id] = None
        self._last_operation_message = f"{provider_id} discovery refresh started."
        return IntegrationOperationResult(
            True,
            self._last_operation_message,
            provider_id=provider_id,
        )

    def _safe_playnite_status(
        self,
        provider_counts: dict[str, int],
        provider_errors: dict[str, str],
    ) -> IntegrationStatus:
        try:
            return self._playnite_status(provider_counts, provider_errors)
        except Exception as exc:
            _LOGGER.exception("Playnite integration status inspection failed")
            return IntegrationStatus(
                "playnite",
                "Playnite",
                IntegrationState.ERROR,
                "Status unavailable",
                f"Vigil could not inspect the Playnite integration: {exc}",
                primary_action="refresh_playnite",
                primary_action_label="Retry",
                game_count=provider_counts.get("playnite", 0),
            )

    def _request_playnite_refresh(self) -> IntegrationOperationResult:
        extension_root = self._resolve_playnite_extension_root(
            self._resolve_playnite_root()
        )
        bridge_dll = extension_root / PLAYNITE_BRIDGE_DLL_NAME
        bridge_manifest = extension_root / PLAYNITE_BRIDGE_MANIFEST_NAME
        if not bridge_dll.is_file() or not bridge_manifest.is_file():
            return IntegrationOperationResult(
                False,
                "The Playnite bridge is not installed completely.",
            )
        if not self._playnite_running_detector():
            return IntegrationOperationResult(
                False,
                "Playnite must be running to rebuild its library snapshot.",
            )
        request_id = str(uuid.uuid4())
        payload = {
            "request_id": request_id,
            "requested_at_utc": datetime.now(UTC).isoformat(),
        }
        try:
            self.refresh_request_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.refresh_request_path, payload)
        except OSError as exc:
            return IntegrationOperationResult(
                False,
                f"Could not request a Playnite snapshot rebuild: {exc}",
            )
        self._last_operation_message = "Playnite snapshot rebuild requested."
        return IntegrationOperationResult(
            True,
            self._last_operation_message,
            provider_id="playnite",
        )

    def _native_status(
        self,
        spec: _NativeIntegrationSpec,
        provider_counts: dict[str, int],
        provider_errors: dict[str, str],
    ) -> IntegrationStatus:
        provider_id = spec.integration_id
        action = f"refresh_{provider_id}"
        count = provider_counts.get(provider_id, 0)
        error = provider_errors.get(provider_id)
        if error is not None:
            return IntegrationStatus(
                provider_id,
                spec.label,
                IntegrationState.ERROR,
                "Provider error",
                error,
                primary_action=action,
                primary_action_label="Refresh",
                game_count=count,
            )

        detected = self._native_detection_cache[provider_id]
        if detected is None:
            try:
                detected = self._native_detectors[provider_id]()
            except Exception as exc:
                _LOGGER.exception(
                    "Native integration detection failed: %s", provider_id
                )
                return IntegrationStatus(
                    provider_id,
                    spec.label,
                    IntegrationState.ERROR,
                    "Detection failed",
                    f"Vigil could not inspect this integration: {exc}",
                    primary_action=action,
                    primary_action_label="Retry",
                    game_count=count,
                )
            self._native_detection_cache[provider_id] = detected
        if not detected:
            return IntegrationStatus(
                provider_id,
                spec.label,
                IntegrationState.NOT_DETECTED,
                "Not detected",
                spec.not_detected_detail,
                primary_action=action,
                primary_action_label="Refresh",
                game_count=0,
            )
        return IntegrationStatus(
            provider_id,
            spec.label,
            IntegrationState.CONNECTED,
            "Connected",
            spec.connected_detail,
            primary_action=action,
            primary_action_label="Refresh",
            game_count=count,
        )

    def _playnite_status(
        self,
        provider_counts: dict[str, int],
        provider_errors: dict[str, str],
    ) -> IntegrationStatus:
        playnite_root = self._resolve_playnite_root()
        extension_root = self._resolve_playnite_extension_root(playnite_root)
        installed_dll = extension_root / PLAYNITE_BRIDGE_DLL_NAME
        installed_manifest = extension_root / PLAYNITE_BRIDGE_MANIFEST_NAME
        installed = installed_dll.is_file() and installed_manifest.is_file()
        bundle = self._resolve_bridge_bundle()
        installed_version = (
            _read_extension_version(installed_manifest) if installed else None
        )

        if playnite_root is None and not installed:
            return IntegrationStatus(
                "playnite",
                "Playnite",
                IntegrationState.NOT_DETECTED,
                "Not detected",
                "Install Playnite first. Vigil can connect it automatically once detected.",
            )

        if not installed:
            if bundle is None:
                return IntegrationStatus(
                    "playnite",
                    "Playnite",
                    IntegrationState.UNAVAILABLE,
                    "Bridge unavailable",
                    "This Vigil build does not contain a staged Playnite bridge DLL.",
                )
            return IntegrationStatus(
                "playnite",
                "Playnite",
                IntegrationState.READY_TO_CONNECT,
                "Ready to connect",
                "Playnite was detected. Vigil can install its bridge automatically.",
                primary_action="playnite_enable",
                primary_action_label="Enable Integration",
                version=bundle.version,
            )

        if bundle is not None:
            if installed_version != bundle.version:
                return IntegrationStatus(
                    "playnite",
                    "Playnite",
                    IntegrationState.UPDATE_AVAILABLE,
                    "Update available",
                    "A newer Vigil Playnite bridge is ready to install.",
                    primary_action="playnite_update",
                    primary_action_label="Update Integration",
                    game_count=provider_counts.get("playnite", 0),
                    version=installed_version,
                )
            if sha256_file(installed_dll) != bundle.dll_sha256:
                return IntegrationStatus(
                    "playnite",
                    "Playnite",
                    IntegrationState.NEEDS_REPAIR,
                    "Needs repair",
                    "The installed bridge does not match Vigil's bundled bridge.",
                    primary_action="playnite_repair",
                    primary_action_label="Repair Integration",
                    game_count=provider_counts.get("playnite", 0),
                    version=installed_version,
                )

        error = provider_errors.get("playnite")
        if error is not None:
            return IntegrationStatus(
                "playnite",
                "Playnite",
                IntegrationState.ERROR,
                "Connection error",
                error,
                primary_action=(
                    "playnite_repair" if bundle is not None else "refresh_playnite"
                ),
                primary_action_label=(
                    "Repair Integration" if bundle is not None else "Refresh"
                ),
                game_count=provider_counts.get("playnite", 0),
                version=installed_version,
            )

        if self._restart_required and self.snapshot_path.is_file():
            self._restart_required = False
        if self._restart_required or not self.snapshot_path.is_file():
            return IntegrationStatus(
                "playnite",
                "Playnite",
                IntegrationState.RESTART_REQUIRED,
                "Restart Playnite required",
                self._last_operation_message or "Restart Playnite to load the bridge.",
                primary_action="refresh_playnite",
                primary_action_label="Check Connection",
                game_count=provider_counts.get("playnite", 0),
                version=installed_version,
            )

        count = provider_counts.get("playnite", 0)
        return IntegrationStatus(
            "playnite",
            "Playnite",
            IntegrationState.CONNECTED,
            "Connected",
            "Bridge active.",
            primary_action="refresh_playnite",
            primary_action_label="Refresh",
            game_count=count,
            version=installed_version,
        )

    def _install_or_repair_playnite(self, verb: str) -> IntegrationOperationResult:
        bundle = self._resolve_bridge_bundle()
        if bundle is None:
            return IntegrationOperationResult(
                False,
                "The Playnite bridge is not bundled with this Vigil build.",
            )
        playnite_root = self._resolve_playnite_root()
        if playnite_root is None:
            return IntegrationOperationResult(
                False, "Playnite was not detected on this PC."
            )
        extension_root = self._resolve_playnite_extension_root(playnite_root)
        try:
            extension_root.mkdir(parents=True, exist_ok=True)
            atomic_copy_file(bundle.dll_path, extension_root / PLAYNITE_BRIDGE_DLL_NAME)
            atomic_copy_file(
                bundle.extension_manifest_path,
                extension_root / PLAYNITE_BRIDGE_MANIFEST_NAME,
            )
        except OSError as exc:
            return IntegrationOperationResult(
                False,
                "Could not update the Playnite bridge. Close Playnite and try again. "
                f"Details: {exc}",
            )

        installed_dll = extension_root / PLAYNITE_BRIDGE_DLL_NAME
        if sha256_file(installed_dll) != bundle.dll_sha256:
            return IntegrationOperationResult(
                False,
                "Playnite bridge verification failed after copy.",
            )

        try:
            self.snapshot_path.unlink(missing_ok=True)
            self.refresh_request_path.unlink(missing_ok=True)
        except OSError as exc:
            return IntegrationOperationResult(
                False,
                f"Bridge installed, but stale Playnite data could not be cleared: {exc}",
            )

        self._restart_required = True
        self._last_operation_message = f"Bridge {verb}. Restart Playnite to finish."
        return IntegrationOperationResult(
            True,
            self._last_operation_message,
            restart_playnite_required=True,
        )

    def _remove_playnite_bridge(self) -> IntegrationOperationResult:
        playnite_root = self._resolve_playnite_root()
        extension_root = self._resolve_playnite_extension_root(playnite_root)
        try:
            if extension_root.exists():
                shutil.rmtree(extension_root)
            self.snapshot_path.unlink(missing_ok=True)
            self.refresh_request_path.unlink(missing_ok=True)
        except OSError as exc:
            return IntegrationOperationResult(
                False,
                "Could not uninstall the Playnite bridge. "
                f"The bridge remains installed. Details: {exc}",
            )
        if extension_root.exists():
            return IntegrationOperationResult(
                False,
                "Playnite bridge verification failed after uninstall.",
            )
        self._restart_required = False
        self._last_operation_message = "Playnite integration uninstalled."
        return IntegrationOperationResult(
            True,
            self._last_operation_message,
            provider_id="playnite",
        )

    def _resolve_bridge_bundle(self) -> _BridgeBundle | None:
        roots: list[Path] = []
        if self._bundle_root is not None:
            roots.append(self._bundle_root)
        roots.append(self._paths.resource_root / "integrations" / "playnite")

        for root in roots:
            bundle = _load_bundle(root)
            if bundle is not None:
                return bundle
        return None

    def _resolve_playnite_root(self) -> Path | None:
        if self._playnite_root_override is not None:
            return self._playnite_root_override
        env_override = os.environ.get("VIGIL_PLAYNITE_ROOT")
        if env_override:
            candidate = Path(env_override).expanduser()
            if candidate.exists():
                return candidate

        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        installed_root = Path(appdata) / "Playnite"
        if (installed_root / "config.json").is_file() or (
            installed_root / "Extensions"
        ).is_dir():
            return installed_root
        return None

    def _resolve_playnite_extension_root(self, playnite_root: Path | None) -> Path:
        if playnite_root is not None:
            return playnite_root / "Extensions" / PLAYNITE_BRIDGE_FOLDER_NAME
        appdata = os.environ.get("APPDATA")
        base = (
            Path(appdata) / "Playnite"
            if appdata
            else self._paths.user_data_root / "Playnite"
        )
        return base / "Extensions" / PLAYNITE_BRIDGE_FOLDER_NAME

    @staticmethod
    def _detect_steam() -> bool:
        context = GameDiscoveryContext(deadline_monotonic=time.monotonic() + 1.5)
        locator = SteamInstallationLocator(LocalSteamFileSystem())
        return locator.locate(context) is not None

    @staticmethod
    def _detect_xbox() -> bool:
        context = GameDiscoveryContext(deadline_monotonic=time.monotonic() + 1.5)
        filesystem = LocalXboxFileSystem()
        locator = XboxGamesInstallationLocator(filesystem)
        return bool(locator.locate_roots(context))

    @staticmethod
    def _detect_epic() -> bool:
        context = GameDiscoveryContext(deadline_monotonic=time.monotonic() + 1.5)
        filesystem = LocalEpicFileSystem()
        locator = EpicManifestInstallationLocator(filesystem)
        return locator.locate(context) is not None

    @staticmethod
    def _detect_battlenet() -> bool:
        context = GameDiscoveryContext(deadline_monotonic=time.monotonic() + 1.5)
        scanner = BattleNetInstalledAppScanner(
            LocalBattleNetRegistry(),
            LocalBattleNetFileSystem(),
        )
        return scanner.has_managed_installation(context)

    @staticmethod
    def _detect_ea() -> bool:
        context = GameDiscoveryContext(deadline_monotonic=time.monotonic() + 1.5)
        scanner = EAInstalledAppScanner(
            LocalEARegistry(),
            LocalEAFileSystem(),
        )
        return scanner.has_managed_installation(context)

    @staticmethod
    def _detect_ubisoft() -> bool:
        context = GameDiscoveryContext(deadline_monotonic=time.monotonic() + 1.5)
        scanner = UbisoftInstalledAppScanner(
            LocalUbisoftRegistry(),
            LocalUbisoftFileSystem(),
        )
        return scanner.has_managed_installation(context)

    @staticmethod
    def _detect_gog() -> bool:
        context = GameDiscoveryContext(deadline_monotonic=time.monotonic() + 1.5)
        scanner = GOGInstalledGameScanner(
            LocalGOGRegistry(),
            LocalGOGFileSystem(),
        )
        return scanner.has_managed_installation(context)


def _provider_summary(
    library: AggregatedGameLibrary | None,
) -> tuple[dict[str, int], dict[str, str]]:
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    if library is None:
        return counts, errors
    for result in library.provider_results:
        if result.snapshot is not None:
            counts[result.provider_id] = len(result.snapshot.games)
            if not result.snapshot.complete and result.snapshot.warnings:
                errors[result.provider_id] = result.snapshot.warnings[0]
        if result.error is not None:
            errors[result.provider_id] = result.error
    return counts, errors


def _load_bundle(root: Path) -> _BridgeBundle | None:
    packaged_manifest = root / PLAYNITE_BUNDLE_MANIFEST_NAME
    dll_path = root / PLAYNITE_BRIDGE_DLL_NAME
    extension_manifest = root / PLAYNITE_BRIDGE_MANIFEST_NAME
    if packaged_manifest.is_file():
        return _load_packaged_bundle(
            root, packaged_manifest, dll_path, extension_manifest
        )
    return None


def _load_packaged_bundle(
    root: Path,
    packaged_manifest: Path,
    dll_path: Path,
    extension_manifest: Path,
) -> _BridgeBundle | None:
    try:
        payload = json.loads(packaged_manifest.read_text(encoding="utf-8"))
        expected_keys = {
            "extension_id",
            "version",
            "dll_filename",
            "dll_sha256",
        }
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            return None
        if payload["extension_id"] != PLAYNITE_BRIDGE_EXTENSION_ID:
            return None
        if payload["dll_filename"] != PLAYNITE_BRIDGE_DLL_NAME:
            return None
        if not dll_path.is_file() or not extension_manifest.is_file():
            return None
        digest = sha256_file(dll_path)
        if digest != payload["dll_sha256"]:
            return None
        version = _read_extension_version(extension_manifest)
        if not version or version != payload["version"]:
            return None
        return _BridgeBundle(root, dll_path, extension_manifest, version, digest)
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _read_extension_version(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = value.strip()
    if values.get("Id") != PLAYNITE_BRIDGE_EXTENSION_ID:
        return None
    if values.get("Module") != PLAYNITE_BRIDGE_DLL_NAME:
        return None
    if values.get("Type") != "GenericPlugin":
        return None
    version = values.get("Version")
    return version or None


def _playnite_processes() -> tuple[psutil.Process, ...]:
    matches: list[psutil.Process] = []
    for process in psutil.process_iter(("pid", "name")):
        try:
            name = str(process.info.get("name") or "").casefold()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name in _PLAYNITE_PROCESS_NAMES:
            matches.append(process)
    return tuple(matches)


def _playnite_is_running() -> bool:
    return bool(_playnite_processes())


def _terminate_playnite() -> bool:
    processes = _playnite_processes()
    if not processes:
        return True
    for process in processes:
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            process.terminate()
    _gone, alive = psutil.wait_procs(processes, timeout=3.0)
    for process in alive:
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            process.kill()
    if alive:
        _gone, alive = psutil.wait_procs(alive, timeout=2.0)
    return not alive and not _playnite_processes()


__all__ = [
    "PLAYNITE_BRIDGE_DLL_NAME",
    "PLAYNITE_BRIDGE_EXTENSION_ID",
    "PLAYNITE_BRIDGE_FOLDER_NAME",
    "PLAYNITE_REFRESH_REQUEST_FILENAME",
    "IntegrationManager",
    "IntegrationOperationResult",
    "IntegrationState",
    "IntegrationStatus",
]
