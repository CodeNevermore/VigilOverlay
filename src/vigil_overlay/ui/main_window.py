"""Compact Mode controls coordinated above a native dim/input backdrop."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import Final, cast

from PySide6.QtCore import (
    QByteArray,
    QEvent,
    QObject,
    QPoint,
    QRect,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QCloseEvent,
    QGuiApplication,
    QHideEvent,
    QIcon,
    QKeyEvent,
    QMouseEvent,
    QMoveEvent,
    QResizeEvent,
    QShowEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from vigil_overlay.contracts.games import GameIdentity, GameRecord
from vigil_overlay.core.config import AppConfig
from vigil_overlay.core.controller_shortcuts import ControllerShortcutBinding
from vigil_overlay.core.errors import VigilOverlayError
from vigil_overlay.core.input_routing import (
    OverlayInputMode,
    OverlayInputPolicy,
    resolve_overlay_input_policy,
)
from vigil_overlay.services.integrations import IntegrationStatus
from vigil_overlay.services.system_status import OverlayStatusBackend
from vigil_overlay.services.telemetry import TelemetrySnapshot
from vigil_overlay.ui.controls import repolish_widget
from vigil_overlay.ui.dialog_coordination import (
    OverlayDialogCoordinator,
    PowerCapabilitiesCallback,
)
from vigil_overlay.ui.dialog_surface import VigilMessageDialog
from vigil_overlay.ui.dim_backdrop import DimBackdropWindow
from vigil_overlay.ui.navigation import (
    NavigationCommand,
    NavigationResult,
    NavigationShell,
    navigation_command_for_key,
)
from vigil_overlay.ui.navigation_coordination import OverlayNavigationCoordinator
from vigil_overlay.ui.overlay_geometry import OverlayGeometryController
from vigil_overlay.ui.power_dialog import PowerActionCallback, PowerMenuDialog
from vigil_overlay.ui.status_cluster import (
    ControllerBatteryStatus,
    OverlayStatusClusterController,
)
from vigil_overlay.ui.update_dialog import UpdateAvailableDialog
from vigil_overlay.ui.windows_windowing import (
    configure_native_overlay_window,
    enforce_native_topmost,
    is_native_display_change,
    native_overlay_message,
)
from vigil_overlay.widgets.builtins import built_in_widget_definitions
from vigil_overlay.widgets.registry import WidgetItemDefinition, WidgetRegistry

_LOGGER = logging.getLogger("vigil_overlay")
PersistConfig = Callable[[AppConfig], None]
HotkeyChangeCallback = Callable[[str], tuple[bool, str]]
HotkeyCaptureCallback = Callable[[bool], None]
HotkeyProbeCallback = Callable[[str], tuple[bool, str]]
ControllerShortcutChangeCallback = Callable[[ControllerShortcutBinding], tuple[bool, str]]
ControllerShortcutCaptureCallback = Callable[[bool], None]
StartupChangeCallback = Callable[[bool], tuple[bool, str]]
BackgroundChangeCallback = Callable[[bool], tuple[bool, str]]
RecoveryActionCallback = Callable[[], tuple[bool, str]]
_CONTROLLER_MOUSE_GUARD_MILLISECONDS = 220
_CONTROLLER_MOUSE_EVENT_TYPES: Final[frozenset[QEvent.Type]] = frozenset(
    {
        QEvent.Type.MouseButtonPress,
        QEvent.Type.MouseButtonRelease,
        QEvent.Type.MouseButtonDblClick,
        QEvent.Type.MouseMove,
        QEvent.Type.Wheel,
        QEvent.Type.ContextMenu,
    }
)


class OverlayWindow(QWidget):
    """Full-screen controls window coordinated above the dim/input backdrop."""

    hidden_to_background = Signal()
    application_exit_requested = Signal()
    input_release_requested = Signal()
    foreground_reconciliation_requested = Signal()
    guide_button_enabled_changed = Signal(bool)
    mouse_navigation_preference_changed = Signal(bool)
    game_launch_requested = Signal(object)
    game_close_requested = Signal(object)
    integration_action_requested = Signal(str)
    update_handoff_requested = Signal()

    def __init__(
        self,
        config: AppConfig,
        persist_config: PersistConfig,
        *,
        background_available: bool,
        widget_registry: WidgetRegistry | None = None,
        telemetry_snapshot: TelemetrySnapshot | None = None,
        hotkey_change_callback: HotkeyChangeCallback | None = None,
        hotkey_capture_callback: HotkeyCaptureCallback | None = None,
        hotkey_probe_callback: HotkeyProbeCallback | None = None,
        controller_shortcut_change_callback: (ControllerShortcutChangeCallback | None) = None,
        controller_shortcut_capture_callback: (ControllerShortcutCaptureCallback | None) = None,
        power_capabilities_callback: PowerCapabilitiesCallback | None = None,
        power_action_callback: PowerActionCallback | None = None,
        startup_change_callback: StartupChangeCallback | None = None,
        startup_available: bool = False,
        background_change_callback: BackgroundChangeCallback | None = None,
        background_setting_available: bool = True,
        safe_mode_active: bool = False,
        safe_mode_restart_callback: RecoveryActionCallback | None = None,
        reset_window_position_callback: RecoveryActionCallback | None = None,
        status_backend: OverlayStatusBackend | None = None,
        controller_battery_status: ControllerBatteryStatus | None = None,
        application_icon: QIcon | None = None,
    ) -> None:
        super().__init__(None)
        self._config = config
        self._persist_config_callback = persist_config
        self._background_available = background_available
        self._hotkey_change_callback = hotkey_change_callback
        self._hotkey_capture_callback = hotkey_capture_callback
        self._hotkey_probe_callback = hotkey_probe_callback
        self._controller_shortcut_change_callback = controller_shortcut_change_callback
        self._controller_shortcut_capture_callback = controller_shortcut_capture_callback
        self._startup_change_callback = startup_change_callback
        self._startup_available = startup_available
        self._background_change_callback = background_change_callback
        self._background_setting_available = background_setting_available
        self._safe_mode_active = safe_mode_active
        self._safe_mode_restart_callback = safe_mode_restart_callback
        self._reset_window_position_callback = reset_window_position_callback
        self._application_icon = application_icon or QIcon()
        self._backdrop = DimBackdropWindow()
        self._allow_close = False
        self._input_release_announced = False
        self._home_games_by_item_id: dict[str, GameRecord] = {}
        self._home_recent_games: tuple[GameRecord, ...] = ()
        self._home_closable_game_identities: frozenset[GameIdentity] = frozenset()
        self._widget_catalog_by_item_id: dict[str, str] = {}
        self._input_policy = resolve_overlay_input_policy(
            overlay_visible=False,
            controller_connected=False,
            allow_mouse_navigation_while_controller_connected=(
                config.controller.allow_mouse_navigation_while_controller_connected
            ),
        )
        self._controller_mouse_input_suppressed = False
        self._controller_mouse_guard_enabled = False
        self._mouse_event_filter_installed = False
        self._controller_mouse_guard_timer = QTimer(self)
        self._controller_mouse_guard_timer.setSingleShot(True)
        self._controller_mouse_guard_timer.setInterval(_CONTROLLER_MOUSE_GUARD_MILLISECONDS)
        self._controller_mouse_guard_timer.timeout.connect(self._release_controller_mouse_guard)
        self._widget_registry = widget_registry or WidgetRegistry(
            built_in_widget_definitions(),
            enabled_widget_ids=tuple(config.widgets.enabled_widget_ids),
            widget_order=tuple(config.widgets.widget_order),
        )
        self._ensure_required_widgets_in_config()
        self._widget_registry.set_enabled_widget_ids(tuple(self._config.widgets.enabled_widget_ids))
        self.setObjectName("overlayRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setAutoFillBackground(False)
        self.setWindowTitle("Vigil Overlay")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._apply_window_flags()
        self._build_ui(
            telemetry_snapshot,
            power_capabilities_callback=power_capabilities_callback,
            power_action_callback=power_action_callback,
            status_backend=status_backend,
            controller_battery_status=controller_battery_status,
        )
        self._sync_guide_button_setting_label()
        self._sync_mouse_navigation_setting_label()
        self._restore_geometry()

    @property
    def backdrop(self) -> DimBackdropWindow:
        return self._backdrop

    @property
    def navigation(self) -> NavigationShell:
        return self._navigation

    @property
    def compact_panel(self) -> QFrame:
        return self._compact_panel

    @property
    def widget_registry(self) -> WidgetRegistry:
        return self._widget_registry

    @property
    def controller_mouse_input_suppressed(self) -> bool:
        return self._controller_mouse_input_suppressed

    @property
    def input_policy(self) -> OverlayInputPolicy:
        return self._input_policy

    def apply_input_policy(self, policy: OverlayInputPolicy) -> None:
        """Apply an application-resolved input route to Vigil's Qt surface."""

        if policy == self._input_policy:
            return
        self._input_policy = policy
        self._controller_mouse_input_suppressed = not policy.allow_mouse_events_in_vigil
        self._controller_mouse_guard_enabled = policy.use_controller_correlated_mouse_guard
        self._controller_mouse_guard_timer.stop()
        self._navigation_coordinator.reset_input_arbitration()
        self._sync_controller_mouse_cursor()

    def _apply_window_flags(self) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self._config.window.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)

    def _build_ui(
        self,
        telemetry_snapshot: TelemetrySnapshot | None,
        *,
        power_capabilities_callback: PowerCapabilitiesCallback | None,
        power_action_callback: PowerActionCallback | None,
        status_backend: OverlayStatusBackend | None,
        controller_battery_status: ControllerBatteryStatus | None,
    ) -> None:
        root_layout = QGridLayout(self)
        self._root_layout = root_layout
        root_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        root_layout.setContentsMargins(50, 28, 48, 28)
        root_layout.setHorizontalSpacing(18)
        root_layout.setVerticalSpacing(12)
        root_layout.setColumnStretch(1, 1)
        root_layout.setRowStretch(1, 1)

        self._compact_panel = QFrame(self)
        self._compact_panel.setObjectName("compactPanel")
        panel_layout = QHBoxLayout(self._compact_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)

        visible_widgets = self._widget_registry.visible_widgets()
        self._navigation = NavigationShell(
            self._widget_registry.registered_widgets(),
            self._config.navigation.selected_widget,
            self._persist_selected_widget,
            self._compact_panel,
            telemetry_snapshot=telemetry_snapshot,
            visible_widget_ids=tuple(widget.widget_id for widget in visible_widgets),
            guide_button_enabled=self._config.controller.guide_button_enabled,
            controller_shortcut_binding=ControllerShortcutBinding(
                tuple(self._config.controller.shortcut_controls)
            ),
            allow_mouse_navigation_while_controller_connected=(
                self._config.controller.allow_mouse_navigation_while_controller_connected
            ),
            hotkey_combination=self._config.hotkey.combination,
            start_with_windows_enabled=self._config.startup.start_with_windows,
            start_with_windows_available=self._startup_available,
            run_in_background_enabled=self._config.background.run_in_background,
            run_in_background_available=self._background_setting_available,
            safe_mode_active=self._safe_mode_active,
            application_icon=self._application_icon,
        )
        self._geometry_controller = OverlayGeometryController(
            self,
            backdrop=self._backdrop,
            compact_panel=self._compact_panel,
            root_layout=self._root_layout,
            config=self._config,
            persist_config=self._persist_config_callback,
            panel_width=lambda: self._navigation.current_panel_width,
            natural_panel_height=lambda: self._navigation.current_natural_panel_height,
            refresh_display_values=self._refresh_display_values,
            configure_native_window=self._configure_native_window,
            reassert_topmost=self._reassert_topmost,
        )
        self._geometry_timer = self._geometry_controller.persist_timer
        self._display_settle_timer = self._geometry_controller.display_settle_timer
        self._navigation.item_activated.connect(self._on_item_activated)
        self._navigation.item_secondary_activated.connect(self._on_item_secondary_activated)
        self._navigation.widget_changed.connect(self._on_widget_changed)
        self._navigation.panel_size_hint_changed.connect(self._schedule_panel_geometry_refresh)
        integrations_view = self._navigation.integrations_view
        if integrations_view is not None:
            integrations_view.uninstall_confirmed.connect(
                lambda: self.integration_action_requested.emit("playnite_remove_confirmed")
            )
        panel_layout.addWidget(self._navigation, 1)
        self._sync_widget_catalog()
        self._apply_panel_geometry()

        root_layout.addWidget(
            self._compact_panel,
            0,
            0,
            2,
            1,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
        )

        self._status_cluster_controller = OverlayStatusClusterController(
            self,
            hide_callback=self.request_hide,
            status_backend=status_backend,
            controller_battery_status=controller_battery_status,
        )
        status_cluster = self._status_cluster_controller.frame
        self._microphone_status_label = self._status_cluster_controller.microphone_label
        self._power_status_label = self._status_cluster_controller.power_label
        self._controller_battery_label = self._status_cluster_controller.controller_battery_label
        self._network_status_label = self._status_cluster_controller.network_label
        self._clock_label = self._status_cluster_controller.clock_label
        self._status_timer = self._status_cluster_controller.timer
        root_layout.addWidget(
            status_cluster,
            0,
            1,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
        )

        self._options_container = QWidget(self)
        self._options_container.setObjectName("widgetOptionsContainer")
        options_layout = QHBoxLayout(self._options_container)
        options_layout.setContentsMargins(0, 0, 0, 0)
        options_layout.setSpacing(0)
        self._options_button = QPushButton("☰  Options", self._options_container)
        self._options_button.setObjectName("widgetOptionsButton")
        self._options_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._options_button.clicked.connect(self._toggle_widget_options)
        options_layout.addWidget(self._options_button)
        self._options_container.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        root_layout.addWidget(
            self._options_container,
            1,
            1,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
        )

        self._options_popup = QFrame(self)
        self._options_popup.setObjectName("widgetOptionsPopup")
        popup_layout = QVBoxLayout(self._options_popup)
        popup_layout.setContentsMargins(10, 8, 10, 10)
        popup_layout.setSpacing(6)
        options_title = QLabel("Options", self._options_popup)
        options_title.setObjectName("widgetOptionsTitle")
        self._close_widget_button = QPushButton(self._options_popup)
        self._close_widget_button.setObjectName("widgetOptionsAction")
        self._close_widget_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._close_widget_button.clicked.connect(self._close_selected_widget)
        popup_layout.addWidget(options_title)
        popup_layout.addWidget(self._close_widget_button)
        self._options_popup.hide()
        self._sync_widget_options()

        # Retain diagnostics without adding desktop-style status text to the compact panel.
        self._hotkey_label = QLabel(self)
        self._hotkey_label.setObjectName("compactHotkeyStatus")
        self._hotkey_label.hide()
        self._action_status = QLabel(self)
        self._action_status.setObjectName("compactActionStatus")
        self._action_status.hide()
        self._dialog_coordinator = OverlayDialogCoordinator(
            self,
            power_capabilities=power_capabilities_callback,
            execute_power_action=power_action_callback,
            restore_focus=self._restore_navigation_focus,
            set_action_status=self._action_status.setText,
            request_update_handoff=self.update_handoff_requested.emit,
        )
        self._navigation_coordinator = OverlayNavigationCoordinator(
            self,
            navigation=self._navigation,
            dialogs=self._dialog_coordinator,
            input_policy=lambda: self._input_policy,
            options_visible=self._options_popup.isVisible,
            toggle_options=self._toggle_widget_options,
            hide_options=self._hide_widget_options,
            close_selected_widget=self._close_selected_widget,
            request_hide=self.request_hide,
            begin_controller_mouse_guard=self._begin_controller_mouse_guard,
            controller_mouse_guard_active=self._controller_mouse_guard_timer.isActive,
        )
        self._pending_keyboard_back_timer = self._navigation_coordinator.pending_keyboard_back_timer
        self._update_clock()
        self._refresh_status_cluster()

    def set_hotkey_status(self, text: str, *, active: bool) -> None:
        self._hotkey_label.setText(text)
        self._hotkey_label.setProperty("hotkeyActive", active)
        self.setToolTip(text)
        repolish_widget(self._hotkey_label)

    def set_hotkey_combination(self, combination: str) -> None:
        self._navigation.set_hotkey_combination(combination)

    def deliver_controller_shortcut(self, binding: ControllerShortcutBinding) -> None:
        settings_view = self._navigation.settings_view
        if settings_view is not None:
            settings_view.deliver_controller_shortcut(binding)

    def set_telemetry_snapshot(self, snapshot: TelemetrySnapshot) -> None:
        self._navigation.set_telemetry_snapshot(snapshot)

    def set_integration_statuses(self, statuses: tuple[IntegrationStatus, ...]) -> None:
        """Refresh the host-owned Integrations widget without rebuilding navigation state."""

        integrations_view = self._navigation.integrations_view
        if integrations_view is not None:
            integrations_view.set_statuses(statuses)

    def set_integration_operation_status(
        self,
        message: str,
        *,
        error: bool = False,
    ) -> None:
        """Show one scoped integration operation result in the Integrations page."""

        integrations_view = self._navigation.integrations_view
        if integrations_view is not None:
            integrations_view.set_operation_status(message, error=error)

    def request_playnite_uninstall_confirmation(self, message: str) -> None:
        """Open the host-owned controller-safe Playnite uninstall warning."""

        integrations_view = self._navigation.integrations_view
        if integrations_view is None:
            return
        integrations_view.open_uninstall_confirmation(message)

    def set_recent_games(
        self,
        games: tuple[GameRecord, ...],
        *,
        closable_game_identities: frozenset[GameIdentity] = frozenset(),
    ) -> None:
        """Populate Home and expose close actions only for confidently running games."""

        visible_games = games[:6]
        visible_closable = frozenset(
            game.identity for game in visible_games if game.identity in closable_game_identities
        )
        if (
            visible_games == self._home_recent_games
            and visible_closable == self._home_closable_game_identities
        ):
            return
        self._home_recent_games = visible_games
        self._home_closable_game_identities = visible_closable

        items: list[WidgetItemDefinition] = []
        mapping: dict[str, GameRecord] = {}
        for game in visible_games:
            digest = hashlib.sha256(
                f"{game.identity.provider_id}\0{game.identity.provider_game_id}".encode()
            ).hexdigest()[:20]
            item_id = f"game_{digest}"
            mapping[item_id] = game
            items.append(
                WidgetItemDefinition(
                    item_id=item_id,
                    label=game.title,
                    description="Launch game.",
                    icon_key="home",
                    icon_path=game.icon.path if game.icon is not None else None,
                    secondary_action_id=(
                        "close_game" if game.identity in visible_closable else None
                    ),
                    secondary_action_label=("X" if game.identity in visible_closable else None),
                    secondary_action_description=(
                        f"Close {game.title}" if game.identity in visible_closable else None
                    ),
                )
            )
        items.append(
            WidgetItemDefinition(
                item_id="power",
                label="Power",
                description="Sleep, hibernate, restart, or shut down this PC.",
                icon_key="overlay",
            )
        )
        self._home_games_by_item_id = mapping
        self._navigation.replace_widget_items(
            "home",
            tuple(items),
            empty_message="No provider-reported recent games are available.",
        )

    def handle_controller_navigation_command(
        self,
        command: NavigationCommand,
    ) -> NavigationResult:
        return self._navigation_coordinator.handle_controller_command(command)

    def notify_controller_direction_released(self, command: NavigationCommand) -> None:
        """Release the top-level widget-direction latch after physical neutral."""

        self._navigation_coordinator.notify_controller_direction_released(command)

    def notify_controller_activation_released(self) -> None:
        """Release-gate any controller-opened host modal."""

        self._navigation_coordinator.notify_controller_activation_released()

    def handle_navigation_command(self, command: NavigationCommand) -> NavigationResult:
        return self._navigation_coordinator.handle_command(command)

    def _cancel_interactions_for_widget_switch(self) -> None:
        self._navigation_coordinator.cancel_interactions_for_widget_switch()

    def _flush_pending_keyboard_back(self) -> None:
        self._navigation_coordinator.flush_pending_keyboard_back()

    def _current_navigation_result(self) -> NavigationResult:
        return self._navigation_coordinator.current_result()

    def _begin_controller_mouse_guard(self) -> None:
        """Temporarily swallow controller-correlated synthetic mouse input."""

        if not self._controller_mouse_guard_enabled:
            return
        self._controller_mouse_guard_timer.start()
        self._sync_controller_mouse_cursor()

    def _release_controller_mouse_guard(self) -> None:
        self._sync_controller_mouse_cursor()

    def _controller_mouse_events_blocked(self) -> bool:
        return self._controller_mouse_input_suppressed or (
            self._controller_mouse_guard_enabled and self._controller_mouse_guard_timer.isActive()
        )

    def _sync_controller_mouse_cursor(self) -> None:
        if self.isVisible() and self._controller_mouse_events_blocked():
            self.setCursor(Qt.CursorShape.BlankCursor)
            self._backdrop.setCursor(Qt.CursorShape.BlankCursor)
            return
        self.unsetCursor()
        self._backdrop.unsetCursor()

    def _install_controller_mouse_event_filter(self) -> None:
        if self._mouse_event_filter_installed:
            return
        application = QApplication.instance()
        if application is None:
            return
        application.installEventFilter(self)
        self._mouse_event_filter_installed = True

    def _remove_controller_mouse_event_filter(self) -> None:
        if not self._mouse_event_filter_installed:
            return
        application = QApplication.instance()
        if application is not None:
            application.removeEventFilter(self)
        self._mouse_event_filter_installed = False

    def _is_overlay_mouse_target(self, watched: QObject) -> bool:
        if watched is self or watched is self._backdrop:
            return True
        return isinstance(watched, QWidget) and self.isAncestorOf(watched)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            self.isVisible()
            and self._controller_mouse_events_blocked()
            and event.type() in _CONTROLLER_MOUSE_EVENT_TYPES
            and self._is_overlay_mouse_target(watched)
        ):
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def _on_item_activated(self, widget_id: str, item_id: str) -> None:
        if not self._navigation_coordinator.accepts_item_activation(widget_id, item_id):
            return

        if widget_id == "home":
            if item_id == "power":
                self._open_power_menu()
                return
            game = self._home_games_by_item_id.get(item_id)
            if game is not None:
                self.game_launch_requested.emit(game)
            return
        if widget_id == "widgets":
            target_widget_id = self._widget_catalog_by_item_id.get(item_id)
            if target_widget_id is not None:
                if target_widget_id not in self._navigation.widget_ids:
                    self._set_widget_enabled(target_widget_id, True, select_after=True)
                else:
                    self._navigation.set_selected_widget(target_widget_id)
            return
        if widget_id == "settings" and item_id == "guide_button":
            self._toggle_guide_button_setting()
            return
        if widget_id == "settings" and item_id == "controller_shortcut":
            settings_view = self._navigation.settings_view
            if (
                settings_view is not None
                and self._controller_shortcut_change_callback is not None
                and self._controller_shortcut_capture_callback is not None
            ):
                settings_view.open_controller_shortcut_editor(
                    self._controller_shortcut_change_callback,
                    self._controller_shortcut_capture_callback,
                )
                self._restore_navigation_focus()
            return
        if (
            widget_id == "settings"
            and item_id == "allow_mouse_navigation_while_controller_connected"
        ):
            self._toggle_mouse_navigation_setting()
            return
        if widget_id == "settings" and item_id == "global_hotkey":
            settings_view = self._navigation.settings_view
            if settings_view is not None and self._hotkey_change_callback is not None:
                settings_view.open_hotkey_editor(
                    self._hotkey_change_callback,
                    capture_callback=self._hotkey_capture_callback,
                    probe_callback=self._hotkey_probe_callback,
                )
                self._restore_navigation_focus()
            return

        if widget_id == "settings" and item_id == "run_in_background":
            if self._background_change_callback is None:
                self._action_status.setText("Run in background is unavailable in this build.")
                return
            target = not self._config.background.run_in_background
            success, detail = self._background_change_callback(target)
            self._action_status.setText(detail)
            if success:
                self._sync_run_in_background_setting_label()
            return
        if widget_id == "settings" and item_id == "start_with_windows":
            if self._startup_change_callback is None:
                self._action_status.setText("Start with Windows is unavailable in this build.")
                return
            target = not self._config.startup.start_with_windows
            success, detail = self._startup_change_callback(target)
            self._action_status.setText(detail)
            if success:
                self._sync_start_with_windows_setting_label()
            return
        if widget_id == "settings" and item_id == "safe_mode":
            if self._safe_mode_restart_callback is None:
                self._action_status.setText("Safe Mode restart is unavailable in this build.")
                return
            _, detail = self._safe_mode_restart_callback()
            self._action_status.setText(detail)
            return
        if widget_id == "settings" and item_id == "reset_window_position":
            if self._reset_window_position_callback is None:
                self._action_status.setText("Overlay position reset is unavailable in this build.")
                return
            _, detail = self._reset_window_position_callback()
            self._action_status.setText(detail)
            return
        if widget_id == "settings" and item_id == "widgets":
            self._navigation.set_selected_widget("widgets")
            return
        if widget_id == "integrations":
            integrations_view = self._navigation.integrations_view
            if integrations_view is not None:
                action = integrations_view.primary_action_for_item(item_id)
                if action is not None:
                    self.integration_action_requested.emit(action)
            return
        if widget_id == "audio":
            audio_view = self._navigation.audio_view
            if audio_view is not None:
                audio_view.activate_item(item_id)
            return
        if widget_id == "wifi":
            wifi_view = self._navigation.wifi_view
            if wifi_view is not None:
                wifi_view.activate_item(item_id)
            return
        if widget_id == "display":
            display_view = self._navigation.display_view
            if display_view is not None:
                display_view.toggle_selector(item_id)
            return
        selected_path = f"Selected {widget_id.replace('_', ' ')} / {item_id.replace('_', ' ')}"
        self._action_status.setText(selected_path)
        _LOGGER.info("Compact widget navigation activated %s/%s", widget_id, item_id)

    def show_startup_hotkey_failure(self, candidate: str, detail: str) -> bool:
        """Display one startup conflict prompt and route Try Again to the editor."""

        settings_view = self._navigation.settings_view
        if settings_view is None:
            return False

        def retry() -> None:
            if self._hotkey_change_callback is None:
                return
            settings_view.open_hotkey_editor(
                self._hotkey_change_callback,
                capture_callback=self._hotkey_capture_callback,
                probe_callback=self._hotkey_probe_callback,
            )
            self._restore_navigation_focus()

        return settings_view.show_hotkey_failure(
            candidate,
            detail,
            retry_callback=retry,
        )

    def _on_item_secondary_activated(
        self,
        widget_id: str,
        item_id: str,
        action_id: str,
    ) -> None:
        if widget_id != "home" or action_id != "close_game":
            return
        game = self._home_games_by_item_id.get(item_id)
        if game is not None:
            self.game_close_requested.emit(game)

    def _open_power_menu(self) -> None:
        self._dialog_coordinator.open_power_menu(
            dialog_factory=PowerMenuDialog,
        )

    def _toggle_guide_button_setting(self) -> None:
        previous = self._config.controller.guide_button_enabled
        enabled = not previous
        self._config.controller.guide_button_enabled = enabled
        try:
            self._persist_config_callback(self._config)
        except (OSError, VigilOverlayError):
            self._config.controller.guide_button_enabled = previous
            self._sync_guide_button_setting_label()
            _LOGGER.exception("Could not persist Xbox/Guide button setting")
            return
        self._sync_guide_button_setting_label()
        self.guide_button_enabled_changed.emit(enabled)
        _LOGGER.info("Xbox/Guide button overlay toggle %s", "enabled" if enabled else "disabled")

    def _sync_guide_button_setting_label(self) -> None:
        settings_view = self._navigation.settings_view
        if settings_view is not None:
            settings_view.set_guide_button_enabled(self._config.controller.guide_button_enabled)

    def _toggle_mouse_navigation_setting(self) -> None:
        previous = self._config.controller.allow_mouse_navigation_while_controller_connected
        enabled = not previous
        self._config.controller.allow_mouse_navigation_while_controller_connected = enabled
        try:
            self._persist_config_callback(self._config)
        except (OSError, VigilOverlayError):
            self._config.controller.allow_mouse_navigation_while_controller_connected = previous
            self._sync_mouse_navigation_setting_label()
            _LOGGER.exception("Could not persist controller mouse-navigation setting")
            return
        self._sync_mouse_navigation_setting_label()
        self.mouse_navigation_preference_changed.emit(enabled)

    def _sync_mouse_navigation_setting_label(self) -> None:
        settings_view = self._navigation.settings_view
        if settings_view is not None:
            settings_view.set_allow_mouse_navigation_while_controller_connected(
                self._config.controller.allow_mouse_navigation_while_controller_connected
            )

    def _sync_start_with_windows_setting_label(self) -> None:
        settings_view = self._navigation.settings_view
        if settings_view is not None:
            settings_view.set_start_with_windows_enabled(self._config.startup.start_with_windows)

    def _sync_run_in_background_setting_label(self) -> None:
        settings_view = self._navigation.settings_view
        if settings_view is not None:
            settings_view.set_run_in_background_enabled(self._config.background.run_in_background)

    def _on_widget_changed(self, widget_id: str) -> None:
        self._hide_widget_options()
        audio_view = self._navigation.audio_view
        if widget_id != "audio" and audio_view is not None and audio_view.interaction_active:
            audio_view.cancel_interaction()
        if widget_id == "audio" and audio_view is not None:
            QTimer.singleShot(0, audio_view.refresh)

        wifi_view = self._navigation.wifi_view
        if widget_id != "wifi" and wifi_view is not None and wifi_view.interaction_active:
            wifi_view.cancel_interaction()
        if widget_id == "wifi" and wifi_view is not None:
            wifi_view.refresh()

        display_view = self._navigation.display_view
        if widget_id != "display" and display_view is not None and display_view.interaction_active:
            # Dropdowns must never survive after Display is no longer active. A
            # pending temporary display change is also reverted here for safety.
            display_view.cancel_interaction()
        self._sync_widget_options()
        self._apply_panel_geometry()
        if widget_id == "display" and display_view is not None:
            display_view.refresh_screen_values(self.screen() or QGuiApplication.primaryScreen())

    def _schedule_panel_geometry_refresh(self) -> None:
        self._geometry_controller.schedule_panel_refresh()

    def _apply_panel_geometry(self) -> None:
        self._geometry_controller.apply_panel_geometry()

    def _persist_selected_widget(self, widget_id: str) -> None:
        previous = self._config.navigation.selected_widget
        if previous == widget_id:
            return
        self._config.navigation.selected_widget = widget_id
        try:
            self._persist_config_callback(self._config)
        except (OSError, VigilOverlayError):
            self._config.navigation.selected_widget = previous
            _LOGGER.exception("Could not persist selected widget")
            return
        _LOGGER.info("Selected overlay widget changed to %s", widget_id)

    def _ensure_required_widgets_in_config(self) -> None:
        enabled = self._config.widgets.enabled_widget_ids
        order = self._config.widgets.widget_order
        for definition in self._widget_registry.registered_widgets():
            if not definition.required:
                continue
            if definition.widget_id not in enabled:
                enabled.append(definition.widget_id)
            if definition.widget_id not in order:
                order.append(definition.widget_id)

    def _sync_widget_catalog(self) -> None:
        items: list[WidgetItemDefinition] = []
        mapping: dict[str, str] = {}
        visible = set(self._navigation.widget_ids)
        for definition in self._widget_registry.registered_widgets():
            if definition.widget_id == "widgets":
                continue
            item_id = f"widget_{definition.widget_id}"
            mapping[item_id] = definition.widget_id
            if definition.required:
                description = f"Open the {definition.label} widget. This widget cannot be closed."
            elif definition.widget_id in visible:
                description = f"Open the {definition.label} widget."
            else:
                description = f"Reopen the {definition.label} widget."
            items.append(
                WidgetItemDefinition(
                    item_id=item_id,
                    label=definition.label,
                    description=description,
                    icon_key=definition.icon_key,
                    widget_icon_source_id=definition.widget_id,
                )
            )
        self._widget_catalog_by_item_id = mapping
        self._navigation.replace_widget_items(
            "widgets",
            tuple(items),
            empty_message="No additional widgets are installed.",
        )

    def _set_widget_enabled(
        self,
        widget_id: str,
        enabled: bool,
        *,
        select_after: bool = False,
    ) -> bool:
        definition = self._widget_registry.definition(widget_id)
        if definition.required and not enabled:
            return False

        previous_enabled = list(self._config.widgets.enabled_widget_ids)
        previous_order = list(self._config.widgets.widget_order)
        previous_selected = self._config.navigation.selected_widget
        previous_visible = self._navigation.widget_ids
        previous_nav_selected = self._navigation.selected_widget_id

        next_enabled = list(previous_enabled)
        if enabled:
            if widget_id not in next_enabled:
                next_enabled.append(widget_id)
        else:
            next_enabled = [current for current in next_enabled if current != widget_id]
        if next_enabled == previous_enabled:
            if select_after and widget_id in self._navigation.widget_ids:
                self._navigation.set_selected_widget(widget_id)
            return True

        next_order = list(previous_order)
        if widget_id not in next_order:
            next_order.append(widget_id)

        self._config.widgets.enabled_widget_ids = next_enabled
        self._config.widgets.widget_order = next_order
        self._widget_registry.set_enabled_widget_ids(tuple(next_enabled))
        next_visible = tuple(
            definition.widget_id for definition in self._widget_registry.visible_widgets()
        )

        target = previous_nav_selected
        if select_after and widget_id in next_visible:
            target = widget_id
        elif target not in next_visible:
            old_index = previous_visible.index(previous_nav_selected)
            target = next_visible[min(old_index, len(next_visible) - 1)]

        self._config.navigation.selected_widget = target
        self._navigation.set_visible_widgets(
            next_visible,
            selected_widget_id=target,
            persist=False,
        )
        try:
            self._persist_config_callback(self._config)
        except (OSError, VigilOverlayError):
            self._config.widgets.enabled_widget_ids = previous_enabled
            self._config.widgets.widget_order = previous_order
            self._config.navigation.selected_widget = previous_selected
            self._widget_registry.set_enabled_widget_ids(tuple(previous_enabled))
            self._navigation.set_visible_widgets(
                previous_visible,
                selected_widget_id=previous_nav_selected,
                persist=False,
            )
            self._sync_widget_catalog()
            self._sync_widget_options()
            _LOGGER.exception("Could not persist widget visibility change for %s", widget_id)
            return False

        self._sync_widget_catalog()
        self._sync_widget_options()
        self._apply_panel_geometry()
        _LOGGER.info("Widget %s %s", widget_id, "enabled" if enabled else "closed")
        return True

    def _toggle_widget_options(self) -> None:
        if self._options_popup.isVisible():
            self._hide_widget_options()
            return
        definition = self._navigation.widget_definition(self._navigation.selected_widget_id)
        if definition.required:
            return
        self._close_widget_button.setText(f"Close {definition.label}")
        self._position_widget_options_popup()
        self._options_popup.show()
        self._options_popup.raise_()
        self._set_navigation_focus(self._close_widget_button, True)

    def _position_widget_options_popup(self) -> None:
        self._options_popup.adjustSize()
        anchor = self._options_button.mapTo(
            self,
            QPoint(0, self._options_button.height() + 6),
        )
        margin = 8
        x = min(
            max(anchor.x(), margin),
            max(self.width() - self._options_popup.width() - margin, margin),
        )
        y = min(
            max(anchor.y(), margin),
            max(self.height() - self._options_popup.height() - margin, margin),
        )
        self._options_popup.move(x, y)

    def _hide_widget_options(self) -> None:
        self._set_navigation_focus(self._close_widget_button, False)
        self._options_popup.hide()

    def _sync_widget_options(self) -> None:
        definition = self._navigation.widget_definition(self._navigation.selected_widget_id)
        closable = not definition.required
        if not closable:
            self._hide_widget_options()
        self._options_container.setVisible(closable)
        self._options_button.setAccessibleName(f"Options for {definition.label}")
        self._close_widget_button.setText(f"Close {definition.label}")

    def _close_selected_widget(self) -> None:
        widget_id = self._navigation.selected_widget_id
        definition = self._navigation.widget_definition(widget_id)
        if definition.required:
            self._hide_widget_options()
            return
        self._hide_widget_options()
        self._set_widget_enabled(widget_id, False)

    def _restore_navigation_focus(self) -> None:
        self._navigation_coordinator.restore_focus()

    @staticmethod
    def _set_navigation_focus(widget: QWidget, active: bool) -> None:
        widget.setProperty("navigationFocus", active)
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()

    def _restore_geometry(self) -> None:
        self._geometry_controller.restore()

    def _target_screen_geometry(self, *, force_primary: bool = False) -> QRect:
        return self._geometry_controller.target_screen_geometry(force_primary=force_primary)

    def set_background_available(self, available: bool) -> None:
        self._background_available = available

    def show_available_update(self, update: object) -> None:
        self._dialog_coordinator.show_available_update(
            update,
            dialog_factory=UpdateAvailableDialog,
        )

    def show_fps_runtime_failure(self, detail: str) -> None:
        self._dialog_coordinator.show_fps_runtime_failure(
            detail,
            dialog_factory=VigilMessageDialog,
        )

    def show_startup_safety_warning(self, detail: str) -> None:
        self._dialog_coordinator.show_startup_safety_warning(
            detail,
            dialog_factory=VigilMessageDialog,
        )

    def reset_position(self) -> None:
        self._geometry_controller.reset_position()

    def show_overlay(self) -> None:
        self._input_release_announced = False
        self._install_controller_mouse_event_filter()
        target = self._target_screen_geometry()
        self._backdrop.show_backdrop(target)
        self.setGeometry(target)
        self._apply_panel_geometry()
        self._update_clock()
        configure_native_overlay_window(int(self.winId()))
        self.show()
        self._configure_native_window()
        self._reassert_topmost()
        if QGuiApplication.platformName() not in {
            "offscreen",
            "minimal",
        }:
            self.raise_()
            self.activateWindow()
        display_view = self._navigation.display_view
        if display_view is not None:
            display_view.refresh_screen_values(self.screen() or QGuiApplication.primaryScreen())
        audio_view = self._navigation.audio_view
        if audio_view is not None and self._navigation.selected_widget_id == "audio":
            audio_view.refresh()
        wifi_view = self._navigation.wifi_view
        if wifi_view is not None and self._navigation.selected_widget_id == "wifi":
            wifi_view.refresh()
        self._restore_navigation_focus()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self._sync_controller_mouse_cursor()

    def request_hide(self) -> None:
        self._announce_input_release()
        self._navigation_coordinator.cancel_pending_keyboard_back()
        integrations_view = self._navigation.integrations_view
        if integrations_view is not None and integrations_view.interaction_active:
            integrations_view.cancel_interaction()
        audio_view = self._navigation.audio_view
        if audio_view is not None and audio_view.interaction_active:
            audio_view.cancel_interaction()
        wifi_view = self._navigation.wifi_view
        if wifi_view is not None and wifi_view.interaction_active:
            wifi_view.cancel_interaction()
        display_view = self._navigation.display_view
        if display_view is not None and display_view.interaction_active:
            display_view.cancel_interaction()
        self._persist_geometry()
        if self._background_available:
            self.hide()
            self._backdrop.hide()
            self.hidden_to_background.emit()
            _LOGGER.info("Unified dimmed overlay hidden to background")
            return

        self._request_application_exit()

    def _request_application_exit(self) -> None:
        _LOGGER.info("Background residency disabled; requesting application exit")
        self.application_exit_requested.emit()
        if self.isVisible():
            # Preserve safe standalone behavior if the host did not connect the
            # application-level exit request.
            self._allow_close = True
            self.close()

    def allow_close(self) -> None:
        self._allow_close = True

    def _announce_input_release(self) -> None:
        """Ask the application to release native input before this window disappears."""

        if self._input_release_announced:
            return
        self._input_release_announced = True
        self.input_release_requested.emit()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._announce_input_release()
        integrations_view = self._navigation.integrations_view
        if integrations_view is not None and integrations_view.interaction_active:
            integrations_view.cancel_interaction()
        audio_view = self._navigation.audio_view
        if audio_view is not None and audio_view.interaction_active:
            audio_view.cancel_interaction()
        wifi_view = self._navigation.wifi_view
        if wifi_view is not None and wifi_view.interaction_active:
            wifi_view.cancel_interaction()
        display_view = self._navigation.display_view
        if display_view is not None and display_view.interaction_active:
            display_view.cancel_interaction()
        self._persist_geometry()
        if self._allow_close:
            self._status_cluster_controller.shutdown()
            self._backdrop.close()
            event.accept()
            return
        if not self._background_available:
            event.ignore()
            QTimer.singleShot(0, self._request_application_exit)
            return
        event.ignore()
        self.hide()
        self._backdrop.hide()
        self.hidden_to_background.emit()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._status_cluster_controller.start()
        self._install_controller_mouse_event_filter()
        if not self._backdrop.isVisible():
            self._backdrop.show_backdrop(self.geometry())
        QTimer.singleShot(0, self._configure_native_window)
        QTimer.singleShot(0, self._reassert_topmost)
        QTimer.singleShot(0, self._apply_panel_geometry)

    def hideEvent(self, event: QHideEvent) -> None:
        self._announce_input_release()
        self._status_cluster_controller.stop()
        self._backdrop.hide()
        self._remove_controller_mouse_event_filter()
        super().hideEvent(event)
        self._sync_controller_mouse_cursor()

    def event(self, event: QEvent) -> bool:
        if event.type() is QEvent.Type.WinIdChange:
            QTimer.singleShot(0, self._configure_native_window)
            QTimer.singleShot(0, self._reassert_topmost)
        elif event.type() is QEvent.Type.ActivationChange and self.isVisible():
            QTimer.singleShot(0, self._reassert_topmost)
            self.foreground_reconciliation_requested.emit()
        elif event.type() in {
            QEvent.Type.ScreenChangeInternal,
            QEvent.Type.DevicePixelRatioChange,
            QEvent.Type.FontChange,
            QEvent.Type.ApplicationFontChange,
            QEvent.Type.StyleChange,
        }:
            QTimer.singleShot(0, self._apply_panel_geometry)
        return super().event(event)

    def nativeEvent(
        self,
        event_type: QByteArray | bytes | bytearray | memoryview[int],
        message: int,
    ) -> tuple[bool, int]:
        result = native_overlay_message(int(message))
        if result is not None:
            return result
        if is_native_display_change(int(message)):
            QTimer.singleShot(0, self._handle_native_display_change)
        return cast(tuple[bool, int], super().nativeEvent(event_type, message))

    def _configure_native_window(self) -> None:
        if not self.isVisible():
            return
        self._backdrop.configure_native_state()
        configure_native_overlay_window(int(self.winId()))

    def _reassert_topmost(self) -> None:
        if not self.isVisible() or not self._config.window.always_on_top:
            return
        # Backdrop first, controls second: the second HWND remains above the first.
        self._backdrop.reassert_topmost()
        enforce_native_topmost(int(self.winId()))

    def _handle_native_display_change(self) -> None:
        self._geometry_controller.handle_display_change()

    def _refresh_display_values(self) -> None:
        display_view = self._navigation.display_view
        if display_view is not None:
            display_view.refresh_screen_values(self.screen() or QGuiApplication.primaryScreen())

    def _settle_display_geometry(self) -> None:
        self._geometry_controller.settle_display_geometry()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._input_policy.mode is OverlayInputMode.FOREGROUND_PENDING:
            event.accept()
            return
        command = navigation_command_for_key(event)
        if command is not None:
            self._navigation_coordinator.handle_keyboard_command(command)
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.accept()

    def moveEvent(self, event: QMoveEvent) -> None:
        if hasattr(self, "_geometry_controller"):
            self._geometry_controller.window_geometry_changed()
        else:
            self._backdrop.setGeometry(self.geometry())
        super().moveEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        if hasattr(self, "_geometry_controller"):
            self._geometry_controller.window_geometry_changed()
        else:
            self._backdrop.setGeometry(self.geometry())
        super().resizeEvent(event)
        self._schedule_panel_geometry_refresh()
        if self._options_popup.isVisible():
            self._position_widget_options_popup()

    def _schedule_geometry_persist(self) -> None:
        self._geometry_controller.schedule_persist()

    def _persist_geometry(self) -> None:
        self._geometry_controller.persist()

    def _refresh_status_cluster(self) -> None:
        self._status_cluster_controller.refresh()

    def _update_clock(self) -> None:
        self._status_cluster_controller.update_clock()
