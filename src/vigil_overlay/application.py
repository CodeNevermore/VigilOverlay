"""PySide6 application lifecycle for Vigil Overlay."""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

from vigil_overlay.contracts.games import GameRecord
from vigil_overlay.core.background import (
    background_recovery_available,
    background_residency_available,
)
from vigil_overlay.core.config import AppConfig, save_config
from vigil_overlay.core.errors import VigilOverlayError
from vigil_overlay.core.hotkeys import parse_hotkey_combination
from vigil_overlay.core.input_routing import (
    InputControlDiagnostics,
    OverlayInputMode,
    OverlayInputPolicy,
    foreground_pending_input_policy,
    resolve_overlay_input_policy,
)
from vigil_overlay.core.paths import ApplicationPaths
from vigil_overlay.core.single_instance import SingleInstanceGuard
from vigil_overlay.providers.builtins import create_builtin_game_provider_registry
from vigil_overlay.services.controller import (
    ControllerInputService,
    create_platform_controller_service,
)
from vigil_overlay.services.foreground_ownership import (
    ForegroundOwnershipService,
    create_platform_foreground_ownership_service,
)
from vigil_overlay.services.fps_runtime import (
    PresentMonFpsService,
    UnavailableFpsService,
    create_platform_fps_service,
)
from vigil_overlay.services.game_close import (
    GameCloseOutcome,
    GameCloseService,
    create_platform_game_close_service,
)
from vigil_overlay.services.game_launch import GameLaunchError, GameLaunchService
from vigil_overlay.services.game_library import (
    AggregatedGameLibrary,
    GameLibraryAggregator,
    GameProviderRegistry,
    select_recent_games,
)
from vigil_overlay.services.game_library_runtime import GameLibraryService
from vigil_overlay.services.guide_button import (
    ControllerInputOwnershipService,
    GuideButtonInputService,
    create_platform_controller_input_ownership_service,
    create_platform_guide_button_service,
)
from vigil_overlay.services.hotkeys import GlobalHotkeyService, HotkeyBackend, HotkeyRegistration
from vigil_overlay.services.input_containment import (
    InputContainmentService,
    create_platform_input_containment_service,
)
from vigil_overlay.services.integration_runtime import IntegrationStatusService
from vigil_overlay.services.integrations import IntegrationManager
from vigil_overlay.services.recovery import (
    RecoveryProcessLauncher,
    launch_recovery_process,
    safe_mode_restart_command,
)
from vigil_overlay.services.startup import (
    StartupRegistrationService,
    create_platform_startup_service,
)
from vigil_overlay.services.telemetry_runtime import (
    TelemetryPollingService,
    create_platform_telemetry_service,
)
from vigil_overlay.services.windows_process import capture_foreground_fps_target
from vigil_overlay.ui.application_icon import application_icon_path, load_application_icon
from vigil_overlay.ui.main_window import OverlayWindow
from vigil_overlay.ui.navigation import NavigationCommand, NavigationOutcome
from vigil_overlay.ui.theme import apply_host_theme

_LOGGER = logging.getLogger("vigil_overlay")
_FOREGROUND_LEASE_POLL_MILLISECONDS = 50
_FOREGROUND_CLAIM_TIMEOUT_SECONDS = 1.0
_FOREGROUND_LOSS_CONFIRM_SECONDS = 0.20


class VigilApplication:
    """Own the Qt application, overlay window, hotkey, and tray restore paths."""

    def __init__(
        self,
        argv: Sequence[str],
        config: AppConfig,
        config_path: Path,
        *,
        safe_mode: bool = False,
        read_only_config: bool = False,
        hotkey_backend: HotkeyBackend | None = None,
        tray_available_override: bool | None = None,
        telemetry_service: TelemetryPollingService | None = None,
        fps_service: PresentMonFpsService | UnavailableFpsService | None = None,
        controller_service: ControllerInputService | None = None,
        guide_button_service: GuideButtonInputService | None = None,
        controller_input_ownership_service: (
            ControllerInputOwnershipService | None
        ) = None,
        input_containment_service: InputContainmentService | None = None,
        foreground_ownership_service: ForegroundOwnershipService | None = None,
        foreground_clock: Callable[[], float] = time.monotonic,
        foreground_claim_timeout_seconds: float = _FOREGROUND_CLAIM_TIMEOUT_SECONDS,
        game_provider_registry: GameProviderRegistry | None = None,
        game_library_service: GameLibraryService | None = None,
        game_launch_service: GameLaunchService | None = None,
        game_close_service: GameCloseService | None = None,
        integration_manager: IntegrationManager | None = None,
        integration_status_service: IntegrationStatusService | None = None,
        startup_service: StartupRegistrationService | None = None,
        safe_mode_restart_launcher: RecoveryProcessLauncher | None = None,
        single_instance_guard: SingleInstanceGuard | None = None,
    ) -> None:
        if foreground_claim_timeout_seconds <= 0:
            raise ValueError("foreground_claim_timeout_seconds must be positive")
        existing_app = QApplication.instance()
        if existing_app is None:
            self.qt_app = QApplication(list(argv))
        elif isinstance(existing_app, QApplication):
            self.qt_app = existing_app
        else:
            raise RuntimeError("A non-QApplication Qt application already exists")
        self.qt_app.setApplicationDisplayName("Vigil Overlay")
        self.qt_app.setApplicationName("VigilOverlay")
        self.qt_app.setOrganizationName("Vigil Overlay")
        self._application_paths = ApplicationPaths.discover()
        self._application_icon = load_application_icon(self._application_paths)
        if self._application_icon.isNull():
            _LOGGER.warning(
                "Vigil application icon could not be loaded from %s; using Qt fallback",
                application_icon_path(self._application_paths),
            )
            self._application_icon = self.qt_app.style().standardIcon(
                QStyle.StandardPixmap.SP_ComputerIcon
            )
        self.qt_app.setWindowIcon(self._application_icon)
        self.resolved_theme = apply_host_theme(self.qt_app, config.theme)

        self._config = config
        self._config_path = config_path
        self._read_only_config = read_only_config
        self._startup_service = startup_service or create_platform_startup_service()
        self._safe_mode = safe_mode
        self._safe_mode_restart_launcher = (
            safe_mode_restart_launcher or launch_recovery_process
        )
        self._single_instance_guard = single_instance_guard
        self._instance_activation_timer = QTimer()
        self._instance_activation_timer.setInterval(250)
        self._instance_activation_timer.timeout.connect(
            self._consume_instance_activation
        )
        self._tray: QSystemTrayIcon | None = None
        self._tray_menu: QMenu | None = None
        self._tray_toggle_action: QAction | None = None
        self._tray_reset_action: QAction | None = None
        self._tray_quit_action: QAction | None = None
        self._hotkey_service = GlobalHotkeyService(hotkey_backend)
        self._hotkey_registration: HotkeyRegistration | None = None
        self._quitting = False
        self._telemetry_service = (
            telemetry_service or create_platform_telemetry_service()
        )
        self._fps_service = fps_service or create_platform_fps_service()
        self._controller_service = (
            controller_service or create_platform_controller_service()
        )
        self._guide_button_service = (
            guide_button_service or create_platform_guide_button_service()
        )
        self._controller_input_ownership_service = (
            controller_input_ownership_service
            or create_platform_controller_input_ownership_service()
        )
        self._input_containment_service = (
            input_containment_service or create_platform_input_containment_service()
        )
        self._input_containment_health_timer = QTimer()
        self._input_containment_health_timer.setInterval(1000)
        self._input_containment_health_timer.timeout.connect(
            self._input_containment_service.maintain
        )
        self._foreground_ownership_service = (
            foreground_ownership_service
            or create_platform_foreground_ownership_service()
        )
        self._foreground_clock = foreground_clock
        self._foreground_claim_timeout_seconds = foreground_claim_timeout_seconds
        self._foreground_claim_deadline = 0.0
        self._foreground_claim_pending = False
        self._foreground_loss_confirming = False
        self._foreground_verified = False
        self._foreground_ownership_timer = QTimer()
        self._foreground_ownership_timer.setInterval(
            _FOREGROUND_LEASE_POLL_MILLISECONDS
        )
        self._foreground_ownership_timer.timeout.connect(
            self._reconcile_foreground_ownership
        )
        self._controller_connected_state = False
        self._controller_commands_ready = True
        self._input_policy = resolve_overlay_input_policy(
            overlay_visible=False,
            controller_connected=False,
            allow_mouse_navigation_while_controller_connected=(
                config.controller.allow_mouse_navigation_while_controller_connected
            ),
        )
        self._game_provider_registry = game_provider_registry or GameProviderRegistry()
        self._game_library_service = game_library_service or GameLibraryService(
            GameLibraryAggregator(self._game_provider_registry)
        )
        self._game_launch_service = game_launch_service or GameLaunchService(
            self._game_provider_registry
        )
        self._game_close_service = (
            game_close_service or create_platform_game_close_service()
        )
        self._recent_games: tuple[GameRecord, ...] = ()
        self._current_game_library: AggregatedGameLibrary | None = None
        self._integration_manager = integration_manager or IntegrationManager(
            self._application_paths,
            enabled=not safe_mode,
        )
        self._integration_status_service = (
            integration_status_service
            or IntegrationStatusService(self._integration_manager)
        )
        if not self._read_only_config:
            try:
                self._startup_service.reconcile(self._config.startup.start_with_windows)
            except OSError:
                _LOGGER.exception("Could not reconcile Start with Windows registration")
        startup_available = (
            self._startup_service.supported and not self._read_only_config
        )

        tray_available = (
            QSystemTrayIcon.isSystemTrayAvailable()
            if tray_available_override is None
            else tray_available_override
        )
        self._tray_available = tray_available
        initial_background = background_residency_available(
            run_in_background=self._config.background.run_in_background,
            tray_available=tray_available,
            hotkey_active=False,
        )
        self.window = OverlayWindow(
            config,
            self._save_config,
            background_available=initial_background,
            controller_battery_status=self._controller_service.battery_snapshot,
            hotkey_change_callback=self._change_global_hotkey,
            hotkey_capture_callback=self._set_hotkey_capture_active,
            startup_change_callback=self._change_start_with_windows,
            startup_available=startup_available,
            background_change_callback=self._change_run_in_background,
            background_setting_available=not self._read_only_config,
            safe_mode_active=safe_mode,
            safe_mode_restart_callback=self._restart_in_safe_mode,
            reset_window_position_callback=self._reset_window_position_from_settings,
            application_icon=self._application_icon,
        )
        self.window.setWindowIcon(self._application_icon)
        self.window.set_integration_statuses(
            self._integration_manager.initial_statuses()
        )

        self._hotkey_service.activated.connect(self.toggle_overlay)
        hotkey_active = self._setup_hotkey()
        background_available = self._sync_background_availability(
            hotkey_active=hotkey_active
        )

        self._setup_tray(tray_available, background_available)
        self.window.hidden_to_background.connect(self._sync_tray_action)
        self.window.hidden_to_background.connect(self._pause_fps_discovery)
        self.window.hidden_to_background.connect(self._handle_overlay_hidden)
        self.window.input_release_requested.connect(self._handle_overlay_hiding)
        self.window.foreground_reconciliation_requested.connect(
            self._reconcile_foreground_ownership
        )
        self._telemetry_service.snapshot_ready.connect(
            self.window.set_telemetry_snapshot
        )
        self._fps_service.metric_ready.connect(self._telemetry_service.apply_fps_update)
        self._controller_service.command_ready.connect(self._handle_controller_command)
        self._controller_service.connection_changed.connect(
            self._handle_controller_connection_changed
        )
        self._controller_service.activation_released.connect(
            self.window.notify_controller_activation_released
        )
        self._controller_service.direction_released.connect(
            self.window.notify_controller_direction_released
        )
        self._controller_service.commands_rearmed.connect(
            self._handle_controller_commands_rearmed
        )
        self._guide_button_service.command_ready.connect(
            self._handle_guide_button_command
        )
        self.window.guide_button_enabled_changed.connect(self._set_guide_button_enabled)
        self.window.mouse_navigation_preference_changed.connect(
            self._handle_mouse_navigation_preference_changed
        )
        self.window.game_launch_requested.connect(self._launch_game)
        self.window.game_close_requested.connect(self._close_game)
        self.window.integration_action_requested.connect(
            self._handle_integration_action
        )
        self._game_library_service.library_ready.connect(self._apply_game_library)
        self._integration_status_service.statuses_ready.connect(
            self.window.set_integration_statuses
        )
        self.qt_app.aboutToQuit.connect(self._before_quit)
        self.window.apply_input_policy(self._input_policy)

    @property
    def hotkey_registration(self) -> HotkeyRegistration | None:
        return self._hotkey_registration

    @property
    def input_control_diagnostics(self) -> InputControlDiagnostics:
        """Return the containment guarantees that are currently verifiable."""

        return InputControlDiagnostics(
            mode=self._input_policy.mode,
            foreground_verification_required=self._foreground_ownership_service.required,
            foreground_verification_supported=self._foreground_ownership_service.supported,
            foreground_verified=self._foreground_verified,
            gameinput_exclusivity_active=self._controller_input_ownership_service.active,
            mouse_keyboard_containment_supported=self._input_containment_service.supported,
            mouse_keyboard_containment_active=self._input_containment_service.active,
        )

    def _save_config(self, config: AppConfig) -> None:
        if self._read_only_config:
            _LOGGER.debug("Safe mode is read-only; configuration write skipped")
            return
        save_config(self._config_path, config)

    def _setup_hotkey(self) -> bool:
        if not self._config.hotkey.enabled:
            detail = "Global hotkey disabled in settings"
            self._hotkey_registration = HotkeyRegistration(
                active=False,
                combination=self._config.hotkey.combination,
                detail=detail,
            )
            self.window.set_hotkey_status(detail, active=False)
            _LOGGER.info(detail)
            return False

        try:
            combination = parse_hotkey_combination(self._config.hotkey.combination)
        except (
            ValueError
        ) as exc:  # Defensive boundary for programmatic AppConfig construction.
            detail = f"Invalid global hotkey: {exc}"
            self._hotkey_registration = HotkeyRegistration(
                active=False,
                combination=self._config.hotkey.combination,
                detail=detail,
            )
            self.window.set_hotkey_status(detail, active=False)
            _LOGGER.error(detail)
            return False

        registration = self._hotkey_service.start(combination)
        self._hotkey_registration = registration
        if registration.active:
            status = f"Hotkey: {registration.combination}"
            _LOGGER.info("%s registered", registration.combination)
        else:
            status = f"Hotkey unavailable: {registration.detail}"
            _LOGGER.warning(
                "Global hotkey %s unavailable: %s",
                registration.combination,
                registration.detail,
            )
        self.window.set_hotkey_status(status, active=registration.active)
        return registration.active

    def _set_hotkey_capture_active(self, active: bool) -> None:
        if active:
            self._hotkey_service.suspend_activations()
            return
        self._hotkey_service.resume_activations()

    def _change_start_with_windows(self, enabled: bool) -> tuple[bool, str]:
        if self._read_only_config:
            return (
                False,
                "Safe mode is read-only; Start with Windows cannot be changed.",
            )
        if not self._startup_service.supported:
            return (
                False,
                "Start with Windows is available only in the installed Windows build.",
            )

        previous_enabled = self._config.startup.start_with_windows
        try:
            previous_command = self._startup_service.read_command()
        except OSError as exc:
            _LOGGER.exception("Could not read Start with Windows registration")
            return False, f"Could not update Start with Windows: {exc}"

        try:
            state = self._startup_service.set_enabled(enabled)
        except OSError as exc:
            rollback_detail = self._restore_startup_command_after_failure(
                previous_command
            )
            _LOGGER.exception("Could not update Start with Windows registration")
            return (
                False,
                f"Could not update Start with Windows: {exc}.{rollback_detail}",
            )

        if not state.supported or state.enabled != enabled:
            rollback_detail = self._restore_startup_command_after_failure(
                previous_command
            )
            return (
                False,
                f"Could not update Start with Windows: {state.detail}.{rollback_detail}",
            )

        self._config.startup.start_with_windows = enabled
        try:
            self._save_config(self._config)
        except (OSError, VigilOverlayError) as exc:
            self._config.startup.start_with_windows = previous_enabled
            rollback_detail = self._restore_startup_command_after_failure(
                previous_command
            )
            _LOGGER.exception("Could not persist Start with Windows setting")
            return (
                False,
                f"Could not save Start with Windows: {exc}.{rollback_detail}",
            )

        state_text = "enabled" if enabled else "disabled"
        _LOGGER.info("Start with Windows %s", state_text)
        return True, f"Start with Windows {state_text}."

    def _restore_startup_command_after_failure(self, command: str | None) -> str:
        try:
            self._startup_service.restore_command(command)
        except OSError as exc:
            _LOGGER.exception(
                "Could not restore previous Start with Windows registration"
            )
            return f" Registry rollback also failed: {exc}."
        return " Previous startup registration was restored."

    def _change_global_hotkey(self, candidate: str) -> tuple[bool, str]:
        if self._read_only_config:
            return False, "Safe mode is read-only; the global hotkey cannot be changed."

        try:
            canonical = parse_hotkey_combination(candidate).canonical
        except ValueError as exc:
            return False, f"Invalid global hotkey: {exc}"

        previous = self._config.hotkey.combination
        if not self._config.hotkey.enabled:
            self._config.hotkey.combination = canonical
            try:
                self._save_config(self._config)
            except (OSError, VigilOverlayError) as exc:
                self._config.hotkey.combination = previous
                _LOGGER.exception("Could not persist global hotkey setting")
                return False, f"Could not save the global hotkey: {exc}"
            self.window.set_hotkey_combination(canonical)
            return True, "Global hotkey saved. The hotkey is currently disabled."

        registration = self._hotkey_service.start(parse_hotkey_combination(canonical))
        if not registration.active:
            restore = self._restore_hotkey_registration(previous)
            detail = f"Could not register {canonical}: {registration.detail}."
            if restore.active:
                detail += f" The previous hotkey {previous} was restored."
            else:
                detail += f" The previous hotkey also could not be restored: {restore.detail}."
            return False, detail

        self._config.hotkey.combination = canonical
        try:
            self._save_config(self._config)
        except (OSError, VigilOverlayError) as exc:
            self._config.hotkey.combination = previous
            restore = self._restore_hotkey_registration(previous)
            _LOGGER.exception("Could not persist global hotkey setting")
            detail = f"Could not save the global hotkey: {exc}."
            if restore.active:
                detail += f" The previous hotkey {previous} was restored."
            else:
                detail += f" The previous hotkey also could not be restored: {restore.detail}."
            return False, detail

        self._hotkey_registration = registration
        self.window.set_hotkey_combination(canonical)
        self.window.set_hotkey_status(f"Hotkey: {canonical}", active=True)
        self._sync_background_availability(hotkey_active=True)
        _LOGGER.info("Global hotkey changed to %s", canonical)
        return True, f"Global hotkey changed to {canonical}."

    def _restore_hotkey_registration(self, combination: str) -> HotkeyRegistration:
        restored = self._hotkey_service.start(parse_hotkey_combination(combination))
        self._hotkey_registration = restored
        if restored.active:
            status = f"Hotkey: {restored.combination}"
        else:
            status = f"Hotkey unavailable: {restored.detail}"
        self.window.set_hotkey_status(status, active=restored.active)
        self._sync_background_availability(hotkey_active=restored.active)
        return restored

    def _background_recovery_available(self, *, hotkey_active: bool) -> bool:
        return background_recovery_available(
            tray_available=self._tray_available,
            hotkey_active=hotkey_active,
        )

    def _sync_background_availability(self, *, hotkey_active: bool) -> bool:
        available = background_residency_available(
            run_in_background=self._config.background.run_in_background,
            tray_available=self._tray_available,
            hotkey_active=hotkey_active,
        )
        self.window.set_background_available(available)
        self.qt_app.setQuitOnLastWindowClosed(not available)
        return available

    def _change_run_in_background(self, enabled: bool) -> tuple[bool, str]:
        if self._read_only_config:
            return False, "Safe mode is read-only; Run in background cannot be changed."

        hotkey_active = bool(
            self._hotkey_registration is not None and self._hotkey_registration.active
        )
        if enabled and not self._background_recovery_available(
            hotkey_active=hotkey_active
        ):
            return (
                False,
                "Run in background requires the system tray or an active global hotkey "
                "so Vigil can be restored.",
            )

        previous = self._config.background.run_in_background
        if previous == enabled:
            self._sync_background_availability(hotkey_active=hotkey_active)
            state = "enabled" if enabled else "disabled"
            return True, f"Run in background is already {state}."

        self._config.background.run_in_background = enabled
        try:
            self._save_config(self._config)
        except (OSError, VigilOverlayError) as exc:
            self._config.background.run_in_background = previous
            self._sync_background_availability(hotkey_active=hotkey_active)
            _LOGGER.exception("Could not persist Run in background setting")
            return False, f"Could not save Run in background: {exc}"

        self._sync_background_availability(hotkey_active=hotkey_active)
        if enabled:
            return True, "Run in background enabled."
        return (
            True,
            "Run in background disabled. Hiding or closing Vigil will exit the app.",
        )

    def _setup_tray(self, available: bool, background_available: bool) -> None:
        if self._tray is not None:
            _LOGGER.debug(
                "System tray already initialized; reusing existing tray objects"
            )
            return
        if not available:
            if background_available:
                _LOGGER.info(
                    "System tray unavailable; another background restore path is active"
                )
            else:
                _LOGGER.warning(
                    "System tray is unavailable; hide actions will exit the app"
                )
            return

        tray = QSystemTrayIcon(self._application_icon, self.qt_app)
        tray.setToolTip("Vigil Overlay")

        menu = QMenu(self.window)
        toggle_action = QAction("Hide Vigil Overlay", menu)
        toggle_action.triggered.connect(self.toggle_overlay)
        reset_action = QAction("Reset Window Position", menu)
        reset_action.triggered.connect(self.reset_window_position)
        quit_action = QAction("Exit Vigil Overlay", menu)
        quit_action.triggered.connect(self.quit)
        menu.addAction(toggle_action)
        menu.addAction(reset_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)

        # Keep explicit Python references for the full application lifetime. The tray,
        # menu, and actions are created once and reused for every show/hide cycle.
        self._tray = tray
        self._tray_menu = menu
        self._tray_toggle_action = toggle_action
        self._tray_reset_action = reset_action
        self._tray_quit_action = quit_action
        tray.show()
        self._sync_tray_action()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Use the normal single-click activation only. Some Windows tray backends
        # emit both Trigger and DoubleClick for one double-click, which would toggle
        # twice and leave the overlay in its original state.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_overlay()

    def _handle_controller_connection_changed(
        self,
        connected: bool,
        controller_index: int,
    ) -> None:
        del controller_index
        self._controller_connected_state = bool(connected)
        self._sync_input_policy()

    def _handle_controller_commands_rearmed(self) -> None:
        """Open controller-primary routing after the worker observes neutral input."""

        self._controller_commands_ready = True

    def _handle_mouse_navigation_preference_changed(self, enabled: bool) -> None:
        del enabled
        self._sync_input_policy()

    def _handle_overlay_hidden(self) -> None:
        self._release_visible_input_control("overlay hidden")

    def _handle_overlay_hiding(self) -> None:
        """Release native input before the visible overlay begins to disappear."""

        self._release_visible_input_control("overlay hide requested")

    def _sync_input_policy(
        self,
        *,
        overlay_visible: bool | None = None,
    ) -> OverlayInputPolicy:
        visible = (
            self.window.isVisible() if overlay_visible is None else overlay_visible
        )
        if (
            visible
            and self._foreground_ownership_service.required
            and not self._foreground_verified
        ):
            policy = foreground_pending_input_policy()
        else:
            policy = resolve_overlay_input_policy(
                overlay_visible=visible,
                controller_connected=self._controller_connected_state,
                allow_mouse_navigation_while_controller_connected=(
                    self._config.controller.allow_mouse_navigation_while_controller_connected
                ),
            )
        previous = self._input_policy
        if policy == previous:
            self._apply_runtime_input_policy(policy)
            return policy

        entering_controller_primary = (
            policy.mode is OverlayInputMode.CONTROLLER_PRIMARY
            and previous.mode is not OverlayInputMode.CONTROLLER_PRIMARY
        )
        if entering_controller_primary:
            # Disarm before publishing controller-primary mode. Any controller signal
            # already queued from mouse-primary mode is rejected until the worker
            # reports a fully neutral controller state.
            self._controller_commands_ready = False
            self._controller_service.require_neutral_before_commands()
        elif policy.mode is not OverlayInputMode.CONTROLLER_PRIMARY:
            self._controller_commands_ready = True

        self._input_policy = policy
        self._apply_runtime_input_policy(policy)
        _LOGGER.debug(
            "Overlay input mode changed: %s -> %s", previous.mode, policy.mode
        )
        return policy

    def _apply_runtime_input_policy(self, policy: OverlayInputPolicy) -> None:
        """Synchronize window routing and native ownership without crossing modes."""

        # Release global hooks before publishing a route that needs mouse input.
        # Entering controller-primary does the inverse: first make Qt ignore mouse,
        # then acquire ordinary controller ownership and native containment.
        if policy.mode is not OverlayInputMode.CONTROLLER_PRIMARY:
            self._input_containment_service.apply_policy(policy)

        self.window.apply_input_policy(policy)
        if policy.hold_gameinput_ownership:
            self._guide_button_service.set_controller_ownership_active(True)
            self._controller_input_ownership_service.activate(
                background_guide_enabled=self._config.controller.guide_button_enabled
            )
        else:
            self._controller_input_ownership_service.deactivate()
            self._guide_button_service.set_controller_ownership_active(False)

        if policy.mode is OverlayInputMode.CONTROLLER_PRIMARY:
            self._input_containment_service.apply_policy(policy)

    def _begin_foreground_input_lease(self) -> None:
        """Suspend input until Windows confirms that Vigil owns foreground."""

        service = self._foreground_ownership_service
        service.release()
        self._foreground_verified = False
        self._foreground_loss_confirming = False
        if not service.required:
            self._foreground_claim_pending = False
            self._sync_input_policy(overlay_visible=True)
            _LOGGER.debug(
                "Foreground verification unavailable; using portable input routing: %s",
                service.detail,
            )
            return

        self._foreground_claim_pending = True
        self._foreground_claim_deadline = (
            self._foreground_clock() + self._foreground_claim_timeout_seconds
        )
        self._controller_commands_ready = False
        self._sync_input_policy(overlay_visible=True)
        service.request(int(self.window.winId()))
        self._foreground_ownership_timer.start()
        self._reconcile_foreground_ownership()

    def _reconcile_foreground_ownership(self) -> None:
        """Acquire, monitor, or fail-open the visible-overlay foreground lease."""

        service = self._foreground_ownership_service
        if not service.required:
            return
        if not (self._foreground_claim_pending or self._foreground_verified):
            return
        if not self.window.isVisible():
            self._release_visible_input_control("overlay no longer visible")
            return
        if service.verify():
            if self._foreground_verified:
                return
            restored_after_transition = self._foreground_loss_confirming
            self._foreground_verified = True
            self._foreground_claim_pending = False
            self._foreground_loss_confirming = False
            self._sync_input_policy(overlay_visible=True)
            reason = (
                "foreground lease restored after transient transition"
                if restored_after_transition
                else "foreground lease acquired"
            )
            self._log_input_control_status(reason)
            return

        if self._foreground_verified:
            _LOGGER.warning(
                "Vigil no longer owns foreground; suspending input control while the "
                "foreground transition is confirmed: %s",
                service.detail,
            )
            self._suspend_input_for_foreground_recheck()
            return

        if (
            self._foreground_claim_pending
            and self._foreground_clock() >= self._foreground_claim_deadline
        ):
            confirmed_loss = self._foreground_loss_confirming
            _LOGGER.warning(
                "%s; input stayed suspended and the overlay will hide: %s",
                (
                    "Vigil foreground loss was confirmed"
                    if confirmed_loss
                    else "Vigil could not verify foreground ownership"
                ),
                service.detail,
            )
            self._release_visible_input_control(
                "foreground ownership lost"
                if confirmed_loss
                else "foreground claim timed out"
            )
            self.window.request_hide()

    def _suspend_input_for_foreground_recheck(self) -> None:
        """Release containment immediately while tolerating a transient HWND handoff."""

        self._foreground_ownership_service.release()
        self._foreground_verified = False
        self._foreground_claim_pending = True
        self._foreground_loss_confirming = True
        self._foreground_claim_deadline = (
            self._foreground_clock() + _FOREGROUND_LOSS_CONFIRM_SECONDS
        )
        self._controller_commands_ready = False
        self._sync_input_policy(overlay_visible=True)
        self._log_input_control_status("foreground transition suspended")

    def _release_visible_input_control(self, reason: str) -> None:
        """Drop all process/global containment state before hiding or shutdown."""

        had_lease_state = self._foreground_claim_pending or self._foreground_verified
        self._foreground_ownership_timer.stop()
        self._foreground_ownership_service.release()
        self._foreground_claim_pending = False
        self._foreground_loss_confirming = False
        self._foreground_verified = False
        self._sync_input_policy(overlay_visible=False)
        if had_lease_state:
            self._log_input_control_status(reason)

    def _log_input_control_status(self, reason: str) -> None:
        status = self.input_control_diagnostics
        _LOGGER.info(
            "Input control status (%s): mode=%s foreground_required=%s "
            "foreground_supported=%s foreground_verified=%s gameinput_exclusive=%s "
            "low_level_supported=%s low_level_containment=%s "
            "raw_input_isolation=%s xinput_isolation=%s",
            reason,
            status.mode.value,
            status.foreground_verification_required,
            status.foreground_verification_supported,
            status.foreground_verified,
            status.gameinput_exclusivity_active,
            status.mouse_keyboard_containment_supported,
            status.mouse_keyboard_containment_active,
            status.raw_input_isolation_available,
            status.xinput_isolation_available,
        )
        if (
            status.mode is OverlayInputMode.CONTROLLER_PRIMARY
            and not status.gameinput_exclusivity_active
        ):
            _LOGGER.warning(
                "Controller-primary is using the XInput compatibility route; GameInput "
                "exclusivity is unavailable and background controller consumers may react"
            )

    def _handle_controller_command(self, command: NavigationCommand) -> None:
        """Route controller input through the same command boundary as keyboard input."""

        if command is NavigationCommand.TOGGLE_OVERLAY:
            self.toggle_overlay()
            return
        if not self.window.isVisible():
            return
        if (
            not self._input_policy.route_native_controller_commands
            or not self._controller_commands_ready
        ):
            return
        result = self.window.handle_controller_navigation_command(command)
        if result.outcome is NavigationOutcome.WIDGET_CHANGED and command in {
            NavigationCommand.MOVE_LEFT,
            NavigationCommand.MOVE_RIGHT,
        }:
            self._controller_service.suppress_direction_repeat_until_release(command)

    def _handle_guide_button_command(self, command: NavigationCommand) -> None:
        """Gate Guide toggles through the persisted user preference."""

        if not self._config.controller.guide_button_enabled:
            return
        self._handle_controller_command(command)

    def _set_guide_button_enabled(self, enabled: bool) -> None:
        if enabled:
            self._guide_button_service.set_controller_ownership_active(
                self._input_policy.hold_gameinput_ownership
            )
            self._guide_button_service.start()
        else:
            self._guide_button_service.deactivate()
        self._controller_input_ownership_service.set_background_guide_enabled(enabled)

    def _apply_game_library(self, library: object) -> None:
        if not isinstance(library, AggregatedGameLibrary):
            _LOGGER.error("Ignored invalid game library payload: %r", type(library))
            return
        self._current_game_library = library
        recent = select_recent_games(library, limit=6)
        self._recent_games = recent
        self._refresh_home_games()
        self._integration_status_service.request(library)
        failures = [
            result for result in library.provider_results if not result.succeeded
        ]
        _LOGGER.info(
            "Game library discovery completed: games=%d recent=%d provider_failures=%d",
            len(library.games),
            len(recent),
            len(failures),
        )

    def _refresh_home_games(self) -> None:
        closable = self._game_close_service.closable_game_identities(self._recent_games)
        self.window.set_recent_games(
            self._recent_games,
            closable_game_identities=closable,
        )

    def _close_game(self, payload: object) -> None:
        if not isinstance(payload, GameRecord):
            _LOGGER.error("Ignored invalid game close payload: %r", type(payload))
            return
        result = self._game_close_service.request_close(payload)
        if result.outcome is GameCloseOutcome.REQUESTED:
            _LOGGER.info(
                "Requested graceful close for provider game %s/%s (pid=%s)",
                payload.identity.provider_id,
                payload.identity.provider_game_id,
                result.process_id,
            )
            QTimer.singleShot(350, self._refresh_home_games)
            QTimer.singleShot(1200, self._refresh_home_games)
            return
        if result.outcome is GameCloseOutcome.AMBIGUOUS:
            _LOGGER.warning(
                "Could not safely close %s because multiple visible processes matched",
                payload.title,
            )
        else:
            _LOGGER.info(
                "No safely closable running process found for %s", payload.title
            )
        self._refresh_home_games()

    def _handle_integration_action(self, action: str) -> None:
        result = self._integration_manager.perform(action)
        self.window.set_integration_operation_status(
            result.message,
            error=not result.succeeded and not result.confirmation_required,
        )
        if result.confirmation_required:
            _LOGGER.info("Integration action %s requires confirmation", action)
            self.window.request_playnite_uninstall_confirmation(result.message)
            return
        if result.succeeded:
            _LOGGER.info("Integration action %s completed: %s", action, result.message)
        else:
            _LOGGER.warning("Integration action %s failed: %s", action, result.message)
        self._integration_status_service.request(self._current_game_library)
        if result.succeeded and result.provider_id is not None:
            self._game_library_service.refresh_provider(result.provider_id)

    def _launch_game(self, payload: object) -> None:
        if not isinstance(payload, GameRecord):
            _LOGGER.error("Ignored invalid game launch payload: %r", type(payload))
            return
        try:
            self._game_launch_service.launch(payload)
        except (GameLaunchError, OSError) as exc:
            _LOGGER.warning("Could not launch %s: %s", payload.title, exc)
            return
        _LOGGER.info(
            "Launched provider game %s/%s without recording Vigil activity history",
            payload.identity.provider_id,
            payload.identity.provider_game_id,
        )
        self.window.request_hide()

    def toggle_overlay(self) -> None:
        if self.window.isVisible():
            self._release_visible_input_control("overlay toggle requested hide")
            self.window.request_hide()
        else:
            self._show_overlay()
        self._sync_tray_action()

    def _show_overlay(self) -> None:
        self._fps_service.set_overlay_visible(True)
        self._refresh_home_games()
        target_found = self._prepare_fps_target()
        self.window.show_overlay()
        self._begin_foreground_input_lease()
        if not target_found:
            QTimer.singleShot(0, self._prepare_fps_target)

    def _consume_instance_activation(self) -> None:
        guard = self._single_instance_guard
        if guard is None or not guard.consume_activation_request():
            return
        self._show_overlay()
        self._sync_tray_action()
        _LOGGER.info("Existing Vigil instance activated by a repeated launch")

    def _prepare_fps_target(self) -> bool:
        target = capture_foreground_fps_target()
        if target is None:
            _LOGGER.debug(
                "No eligible foreground or underlay FPS target was found; "
                "preserving any active FPS session"
            )
            return False
        self._fps_service.set_target(target)
        _LOGGER.info(
            "FPS target selected from foreground/underlay window: %s (pid=%d)",
            target.executable_name,
            target.process_id,
        )
        return True

    def _restart_in_safe_mode(self) -> tuple[bool, str]:
        if self._safe_mode:
            return False, "Vigil is already running in Safe Mode."

        command = safe_mode_restart_command()
        self._teardown_hotkey()
        try:
            self._safe_mode_restart_launcher(command)
        except OSError as exc:
            hotkey_active = self._setup_hotkey()
            self._sync_background_availability(hotkey_active=hotkey_active)
            _LOGGER.exception("Could not restart Vigil in Safe Mode")
            return False, f"Could not restart Vigil in Safe Mode: {exc}"

        self.quit()
        return True, "Restarting Vigil in Safe Mode."

    def _reset_window_position_from_settings(self) -> tuple[bool, str]:
        self.reset_window_position()
        if self._read_only_config:
            return True, "Overlay position reset for this Safe Mode session."
        return True, "Overlay position reset to the primary display."

    def reset_window_position(self) -> None:
        self._fps_service.set_overlay_visible(True)
        target_found = self._prepare_fps_target()
        self.window.reset_position()
        self.window.show_overlay()
        self._begin_foreground_input_lease()
        if not target_found:
            QTimer.singleShot(0, self._prepare_fps_target)
        self._sync_tray_action()
        _LOGGER.info("Overlay window position reset")

    def _pause_fps_discovery(self) -> None:
        """Suspend failed-target discovery while preserving a verified live FPS session."""

        self._fps_service.set_overlay_visible(False)
        _LOGGER.debug("FPS target discovery paused while Vigil is hidden")

    def _sync_tray_action(self) -> None:
        action = self._tray_toggle_action
        if action is None:
            return
        action.setText(
            "Hide Vigil Overlay" if self.window.isVisible() else "Show Vigil Overlay"
        )

    def _teardown_hotkey(self) -> None:
        self._hotkey_service.stop()
        self._hotkey_registration = None

    def _teardown_tray(self) -> None:
        tray = self._tray
        if tray is None:
            return

        tray.hide()
        with suppress(RuntimeError):
            tray.activated.disconnect(self._tray_activated)
        tray.setContextMenu(None)  # type: ignore[arg-type]

        menu = self._tray_menu
        if menu is not None:
            menu.close()
            menu.deleteLater()
        tray.deleteLater()

        self._tray = None
        self._tray_menu = None
        self._tray_toggle_action = None
        self._tray_reset_action = None
        self._tray_quit_action = None

    def quit(self) -> None:
        self._quitting = True
        self._instance_activation_timer.stop()
        self._input_containment_health_timer.stop()
        self._release_visible_input_control("application quit")
        self._game_library_service.stop()
        self._integration_status_service.stop()
        self._guide_button_service.stop()
        self._controller_input_ownership_service.stop()
        self._input_containment_service.stop()
        self._controller_service.stop()
        self._fps_service.stop()
        self._telemetry_service.stop()
        self._teardown_hotkey()
        self.window.allow_close()
        self.window.close()
        self._teardown_tray()
        self.qt_app.quit()

    def run(self) -> int:
        """Start application services and enter the Qt event loop."""

        if self._single_instance_guard is not None:
            self._instance_activation_timer.start()
        self._input_containment_health_timer.start()
        self._game_library_service.start()
        self._integration_status_service.request(self._current_game_library)
        self._telemetry_service.start()
        self._fps_service.start()
        self._fps_service.set_overlay_visible(True)
        self._controller_service.start()
        if self._config.controller.guide_button_enabled:
            self._guide_button_service.start()
        target_found = self._prepare_fps_target()
        self.window.show_overlay()
        self._begin_foreground_input_lease()
        if not target_found:
            QTimer.singleShot(0, self._prepare_fps_target)
        return self.qt_app.exec()

    def _before_quit(self) -> None:
        self._instance_activation_timer.stop()
        self._input_containment_health_timer.stop()
        self._release_visible_input_control("Qt application shutdown")
        self._game_library_service.stop()
        self._integration_status_service.stop()
        self._guide_button_service.stop()
        self._controller_input_ownership_service.stop()
        self._input_containment_service.stop()
        self._controller_service.stop()
        self._fps_service.stop()
        self._telemetry_service.stop()
        self._teardown_hotkey()
        if not self._quitting:
            self.window.allow_close()
            self.window.close()
        self._teardown_tray()
        if not self._read_only_config:
            try:
                self._save_config(self._config)
            except (OSError, VigilOverlayError):
                _LOGGER.exception("Could not persist configuration during shutdown")
        _LOGGER.info("Vigil Overlay Qt application stopped")


def run_gui(
    config: AppConfig,
    config_path: Path,
    *,
    safe_mode: bool = False,
    read_only_config: bool = False,
    single_instance_guard: SingleInstanceGuard | None = None,
) -> int:
    """Assemble platform services and run the Qt application."""

    paths = ApplicationPaths.discover()
    game_provider_registry = create_builtin_game_provider_registry(
        paths, safe_mode=safe_mode
    )
    integration_manager = IntegrationManager(paths, enabled=not safe_mode)
    application = VigilApplication(
        sys.argv,
        config,
        config_path,
        safe_mode=safe_mode,
        read_only_config=read_only_config,
        game_provider_registry=game_provider_registry,
        integration_manager=integration_manager,
        single_instance_guard=single_instance_guard,
    )
    _LOGGER.info(
        "Starting PySide6 shell, theme=%s, safe_mode=%s",
        application.resolved_theme,
        safe_mode,
    )
    return application.run()
