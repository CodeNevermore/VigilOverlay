"""Compact Mode controls coordinated above a native dim/input backdrop."""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Final, cast

from PySide6.QtCore import QByteArray, QEvent, QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QCursor,
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

from vigil_overlay.contracts.controller import ControllerBatterySnapshot
from vigil_overlay.contracts.games import GameIdentity, GameRecord
from vigil_overlay.core.config import AppConfig
from vigil_overlay.core.errors import VigilOverlayError
from vigil_overlay.core.input_routing import (
    OverlayInputMode,
    OverlayInputPolicy,
    resolve_overlay_input_policy,
)
from vigil_overlay.services.integrations import IntegrationStatus
from vigil_overlay.services.system_status import (
    OverlayStatusBackend,
    OverlayStatusRuntime,
    OverlayStatusSnapshot,
)
from vigil_overlay.services.telemetry import TelemetrySnapshot
from vigil_overlay.ui.controls import repolish_widget
from vigil_overlay.ui.dim_backdrop import DimBackdropWindow
from vigil_overlay.ui.modal_guard import ModalInputSource
from vigil_overlay.ui.navigation import (
    NavigationCommand,
    NavigationOutcome,
    NavigationResult,
    NavigationShell,
    navigation_command_for_key,
)
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
StartupChangeCallback = Callable[[bool], tuple[bool, str]]
BackgroundChangeCallback = Callable[[bool], tuple[bool, str]]
RecoveryActionCallback = Callable[[], tuple[bool, str]]
ControllerBatteryStatus = Callable[[], ControllerBatterySnapshot]
_MIN_COMPACT_PANEL_HEIGHT = 430
_PANEL_BOTTOM_SAFETY_MARGIN = 28
_CONTROLLER_DUPLICATE_WINDOW_SECONDS = 0.14
_CONTROLLER_MOUSE_GUARD_MILLISECONDS = 220
_ITEM_ACTIVATION_DEDUP_SECONDS = 0.30
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
    input_release_requested = Signal()
    foreground_reconciliation_requested = Signal()
    guide_button_enabled_changed = Signal(bool)
    mouse_navigation_preference_changed = Signal(bool)
    game_launch_requested = Signal(object)
    game_close_requested = Signal(object)
    integration_action_requested = Signal(str)

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
        self._startup_change_callback = startup_change_callback
        self._startup_available = startup_available
        self._background_change_callback = background_change_callback
        self._background_setting_available = background_setting_available
        self._safe_mode_active = safe_mode_active
        self._safe_mode_restart_callback = safe_mode_restart_callback
        self._reset_window_position_callback = reset_window_position_callback
        self._status_backend = status_backend
        self._status_runtime = (
            None if status_backend is not None else OverlayStatusRuntime(parent=self)
        )
        self._last_status_snapshot = OverlayStatusSnapshot()
        if self._status_runtime is not None:
            self._status_runtime.snapshot_ready.connect(self._set_status_snapshot)
        self._controller_battery_status = controller_battery_status
        self._application_icon = application_icon or QIcon()
        self._backdrop = DimBackdropWindow()
        self._allow_close = False
        self._input_release_announced = False
        self._home_games_by_item_id: dict[str, GameRecord] = {}
        self._home_recent_games: tuple[GameRecord, ...] = ()
        self._home_closable_game_identities: frozenset[GameIdentity] = frozenset()
        self._widget_catalog_by_item_id: dict[str, str] = {}
        self._last_controller_command_at: dict[NavigationCommand, float] = {}
        self._last_keyboard_command_at: dict[NavigationCommand, float] = {}
        self._last_item_activation: tuple[str, str, float] | None = None
        self._controller_widget_direction_latch: NavigationCommand | None = None
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
        self._controller_mouse_guard_timer.setInterval(
            _CONTROLLER_MOUSE_GUARD_MILLISECONDS
        )
        self._controller_mouse_guard_timer.timeout.connect(
            self._release_controller_mouse_guard
        )
        self._pending_keyboard_back_at: float | None = None
        self._pending_keyboard_back_timer = QTimer(self)
        self._pending_keyboard_back_timer.setSingleShot(True)
        self._pending_keyboard_back_timer.setInterval(
            int(_CONTROLLER_DUPLICATE_WINDOW_SECONDS * 1000) + 20
        )
        self._pending_keyboard_back_timer.timeout.connect(
            self._flush_pending_keyboard_back
        )
        self._widget_registry = widget_registry or WidgetRegistry(
            built_in_widget_definitions(),
            enabled_widget_ids=tuple(config.widgets.enabled_widget_ids),
            widget_order=tuple(config.widgets.widget_order),
        )
        self._ensure_required_widgets_in_config()
        self._widget_registry.set_enabled_widget_ids(
            tuple(self._config.widgets.enabled_widget_ids)
        )
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(250)
        self._geometry_timer.timeout.connect(self._persist_geometry)
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(2000)
        self._status_timer.timeout.connect(self._refresh_status_cluster)
        self._display_settle_timer = QTimer(self)
        self._display_settle_timer.setSingleShot(True)
        self._display_settle_timer.setInterval(350)
        self._display_settle_timer.timeout.connect(self._settle_display_geometry)

        self.setObjectName("overlayRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setAutoFillBackground(False)
        self.setWindowTitle("Vigil Overlay")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._apply_window_flags()
        self._build_ui(telemetry_snapshot)
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
        self._controller_mouse_guard_enabled = (
            policy.use_controller_correlated_mouse_guard
        )
        self._controller_mouse_guard_timer.stop()
        self._controller_widget_direction_latch = None
        self._pending_keyboard_back_timer.stop()
        self._pending_keyboard_back_at = None
        self._last_controller_command_at.clear()
        self._last_keyboard_command_at.clear()
        self._last_item_activation = None
        self._sync_controller_mouse_cursor()

    def _apply_window_flags(self) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self._config.window.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)

    def _build_ui(self, telemetry_snapshot: TelemetrySnapshot | None) -> None:
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
        self._navigation.item_activated.connect(self._on_item_activated)
        self._navigation.item_secondary_activated.connect(
            self._on_item_secondary_activated
        )
        self._navigation.widget_changed.connect(self._on_widget_changed)
        self._navigation.panel_size_hint_changed.connect(
            self._schedule_panel_geometry_refresh
        )
        integrations_view = self._navigation.integrations_view
        if integrations_view is not None:
            integrations_view.uninstall_confirmed.connect(
                lambda: self.integration_action_requested.emit(
                    "playnite_remove_confirmed"
                )
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

        status_cluster = QFrame(self)
        status_cluster.setObjectName("overlayStatusCluster")
        status_layout = QHBoxLayout(status_cluster)
        status_layout.setContentsMargins(8, 3, 8, 3)
        status_layout.setSpacing(13)

        self._microphone_status_label = QLabel("?", status_cluster)
        self._microphone_status_label.setObjectName("statusGlyph")
        self._power_status_label = QLabel("?", status_cluster)
        self._power_status_label.setObjectName("statusGlyph")
        self._controller_battery_label = QLabel("", status_cluster)
        self._controller_battery_label.setObjectName("statusGlyph")
        self._controller_battery_label.setVisible(False)
        self._network_status_label = QLabel("?", status_cluster)
        self._network_status_label.setObjectName("statusGlyph")
        self._clock_label = QLabel(status_cluster)
        self._clock_label.setObjectName("overlayClock")
        hide_button = QPushButton("X", status_cluster)
        hide_button.setObjectName("overlayHideButton")
        hide_button.setToolTip("Hide Vigil Overlay")
        hide_button.setAccessibleName("Hide Vigil Overlay")
        hide_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        hide_button.clicked.connect(self.request_hide)

        status_layout.addWidget(self._microphone_status_label)
        status_layout.addWidget(self._power_status_label)
        status_layout.addWidget(self._controller_battery_label)
        status_layout.addWidget(self._network_status_label)
        status_layout.addWidget(self._clock_label)
        status_layout.addWidget(hide_button)
        status_cluster.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
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
        self._update_clock()
        self._refresh_status_cluster()

    def set_hotkey_status(self, text: str, *, active: bool) -> None:
        self._hotkey_label.setText(text)
        self._hotkey_label.setProperty("hotkeyActive", active)
        self.setToolTip(text)
        repolish_widget(self._hotkey_label)

    def set_hotkey_combination(self, combination: str) -> None:
        self._navigation.set_hotkey_combination(combination)

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
            game.identity
            for game in visible_games
            if game.identity in closable_game_identities
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
                    secondary_action_label=(
                        "X" if game.identity in visible_closable else None
                    ),
                    secondary_action_description=(
                        f"Close {game.title}"
                        if game.identity in visible_closable
                        else None
                    ),
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
        """Apply one physical-controller command with duplicate-input arbitration.

        Controller mapping tools such as JoyXoff can synthesize a keyboard or mouse
        action for the same physical press that Vigil also receives through XInput.
        Keep a short ownership window around controller activity so one physical press
        produces one Vigil action without disabling controller repeat behavior.
        """

        if not self._input_policy.route_native_controller_commands:
            return self._current_navigation_result()

        now = time.monotonic()
        if (
            command in {NavigationCommand.MOVE_LEFT, NavigationCommand.MOVE_RIGHT}
            and self._controller_widget_direction_latch is not None
        ):
            self._begin_controller_mouse_guard()
            return self._current_navigation_result()
        pending_back_owned = self._cancel_correlated_pending_keyboard_back(
            command, now=now
        )
        keyboard_at = self._last_keyboard_command_at.get(command)
        self._last_controller_command_at[command] = now
        self._begin_controller_mouse_guard()
        if (
            not pending_back_owned
            and keyboard_at is not None
            and now - keyboard_at <= _CONTROLLER_DUPLICATE_WINDOW_SECONDS
        ):
            return self._current_navigation_result()
        if command is NavigationCommand.ACTIVATE:
            display_view = self._navigation.display_view
            if display_view is not None:
                display_view.set_next_input_source(ModalInputSource.CONTROLLER)
            integrations_view = self._navigation.integrations_view
            if (
                integrations_view is not None
                and not integrations_view.interaction_active
            ):
                integrations_view.set_next_input_source(ModalInputSource.CONTROLLER)
        result = self.handle_navigation_command(command)
        if result.outcome is NavigationOutcome.WIDGET_CHANGED and command in {
            NavigationCommand.MOVE_LEFT,
            NavigationCommand.MOVE_RIGHT,
        }:
            self._controller_widget_direction_latch = command
        return result

    def notify_controller_direction_released(self, command: NavigationCommand) -> None:
        """Release the top-level widget-direction latch after physical neutral."""

        if self._controller_widget_direction_latch is command:
            self._controller_widget_direction_latch = None

    def notify_controller_activation_released(self) -> None:
        """Release-gate any controller-opened host modal."""

        display_view = self._navigation.display_view
        if display_view is not None:
            display_view.notify_controller_activation_released()
        integrations_view = self._navigation.integrations_view
        if integrations_view is not None:
            integrations_view.notify_controller_activation_released()

    def handle_navigation_command(self, command: NavigationCommand) -> NavigationResult:
        """Apply one keyboard/controller-neutral navigation command."""

        if command in {
            NavigationCommand.PREVIOUS_WIDGET,
            NavigationCommand.NEXT_WIDGET,
        }:
            self._cancel_interactions_for_widget_switch()
            return self._navigation.handle_command(command)

        if self._options_popup.isVisible():
            if command is NavigationCommand.ACTIVATE:
                self._close_selected_widget()
            elif command in {NavigationCommand.BACK, NavigationCommand.OPEN_OPTIONS}:
                self._hide_widget_options()
            return self._current_navigation_result()

        integrations_view = self._navigation.integrations_view
        if integrations_view is not None and integrations_view.interaction_active:
            if command in {NavigationCommand.MOVE_UP, NavigationCommand.MOVE_LEFT}:
                integrations_view.move_interaction(-1)
                return self._current_navigation_result()
            if command in {NavigationCommand.MOVE_DOWN, NavigationCommand.MOVE_RIGHT}:
                integrations_view.move_interaction(1)
                return self._current_navigation_result()
            if command is NavigationCommand.ACTIVATE:
                integrations_view.activate_interaction()
                return self._current_navigation_result()
            if command is NavigationCommand.BACK:
                integrations_view.cancel_interaction()
                return self._current_navigation_result()
            return self._current_navigation_result()

        audio_view = self._navigation.audio_view
        if audio_view is not None and audio_view.interaction_active:
            if command in {NavigationCommand.MOVE_UP, NavigationCommand.MOVE_LEFT}:
                audio_view.move_interaction(-1)
                return self._current_navigation_result()
            if command in {NavigationCommand.MOVE_DOWN, NavigationCommand.MOVE_RIGHT}:
                audio_view.move_interaction(1)
                return self._current_navigation_result()
            if command is NavigationCommand.ACTIVATE:
                audio_view.activate_interaction()
                return self._current_navigation_result()
            if command is NavigationCommand.BACK:
                audio_view.cancel_interaction()
                return self._current_navigation_result()

        wifi_view = self._navigation.wifi_view
        if wifi_view is not None and wifi_view.interaction_active:
            if command in {NavigationCommand.MOVE_UP, NavigationCommand.MOVE_LEFT}:
                wifi_view.move_interaction(-1)
                return self._current_navigation_result()
            if command in {NavigationCommand.MOVE_DOWN, NavigationCommand.MOVE_RIGHT}:
                wifi_view.move_interaction(1)
                return self._current_navigation_result()
            if command is NavigationCommand.ACTIVATE:
                wifi_view.activate_interaction()
                return self._current_navigation_result()
            if command is NavigationCommand.BACK:
                wifi_view.cancel_interaction()
                return self._current_navigation_result()
            return self._current_navigation_result()

        if (
            audio_view is not None
            and self._navigation.selected_widget_id == "audio"
            and self._navigation.focus_zone.value == "content"
            and command in {NavigationCommand.MOVE_LEFT, NavigationCommand.MOVE_RIGHT}
        ):
            item_id = self._navigation.selected_item_id
            if item_id is not None and audio_view.adjust_item(
                item_id, 1 if command is NavigationCommand.MOVE_RIGHT else -1
            ):
                return self._current_navigation_result()

        display_view = self._navigation.display_view
        if display_view is not None and display_view.interaction_active:
            if command in {NavigationCommand.MOVE_UP, NavigationCommand.MOVE_LEFT}:
                display_view.move_interaction(-1)
                return self._current_navigation_result()
            if command in {NavigationCommand.MOVE_DOWN, NavigationCommand.MOVE_RIGHT}:
                display_view.move_interaction(1)
                return self._current_navigation_result()
            if command is NavigationCommand.ACTIVATE:
                display_view.activate_interaction()
                return self._current_navigation_result()
            if command is NavigationCommand.BACK:
                display_view.cancel_interaction()
                return self._current_navigation_result()

        if command is NavigationCommand.OPEN_OPTIONS:
            self._toggle_widget_options()
            return self._current_navigation_result()

        result = self._navigation.handle_command(command)
        if result.outcome in {
            NavigationOutcome.HIDE_REQUESTED,
            NavigationOutcome.TOGGLE_REQUESTED,
        }:
            self.request_hide()
        return result

    def _cancel_interactions_for_widget_switch(self) -> None:
        if self._options_popup.isVisible():
            self._hide_widget_options()
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

    def _cancel_correlated_pending_keyboard_back(
        self,
        command: NavigationCommand,
        *,
        now: float,
    ) -> bool:
        """Let physical controller Back/Menu own a correlated synthetic Escape press."""

        if command not in {NavigationCommand.BACK, NavigationCommand.OPEN_OPTIONS}:
            return False
        pending_at = self._pending_keyboard_back_at
        if pending_at is None or not self._pending_keyboard_back_timer.isActive():
            return False
        if now - pending_at > _CONTROLLER_DUPLICATE_WINDOW_SECONDS:
            return False
        self._pending_keyboard_back_timer.stop()
        self._pending_keyboard_back_at = None
        return True

    def _queue_keyboard_back(self, *, now: float) -> None:
        """Delay Escape only while a controller is connected so XInput can own duplicates."""

        if not self._input_policy.route_native_controller_commands:
            self.handle_navigation_command(NavigationCommand.BACK)
            return

        recent_controller = max(
            (
                self._last_controller_command_at.get(
                    NavigationCommand.BACK, float("-inf")
                ),
                self._last_controller_command_at.get(
                    NavigationCommand.OPEN_OPTIONS,
                    float("-inf"),
                ),
            )
        )
        if now - recent_controller <= _CONTROLLER_DUPLICATE_WINDOW_SECONDS:
            self._pending_keyboard_back_at = None
            self._pending_keyboard_back_timer.stop()
            return
        self._pending_keyboard_back_at = now
        self._pending_keyboard_back_timer.start()

    def _flush_pending_keyboard_back(self) -> None:
        if self._pending_keyboard_back_at is None:
            return
        self._pending_keyboard_back_at = None
        if self.isVisible():
            self.handle_navigation_command(NavigationCommand.BACK)

    def _current_navigation_result(self) -> NavigationResult:
        return NavigationResult(
            outcome=NavigationOutcome.NO_CHANGE,
            selected_widget_id=self._navigation.selected_widget_id,
            focus_zone=self._navigation.focus_zone,
            selected_item_index=self._navigation.selected_item_index,
            selected_item_id=self._navigation.selected_item_id,
        )

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
            self._controller_mouse_guard_enabled
            and self._controller_mouse_guard_timer.isActive()
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
        now = time.monotonic()
        previous = self._last_item_activation
        if (
            self._controller_mouse_guard_timer.isActive()
            and previous is not None
            and previous[0] == widget_id
            and previous[1] == item_id
            and now - previous[2] <= _ITEM_ACTIVATION_DEDUP_SECONDS
        ):
            return
        self._last_item_activation = (widget_id, item_id, now)

        if widget_id == "home":
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
                )
            return
        if widget_id == "settings" and item_id == "run_in_background":
            if self._background_change_callback is None:
                self._action_status.setText(
                    "Run in background is unavailable in this build."
                )
                return
            target = not self._config.background.run_in_background
            success, detail = self._background_change_callback(target)
            self._action_status.setText(detail)
            if success:
                self._sync_run_in_background_setting_label()
            return
        if widget_id == "settings" and item_id == "start_with_windows":
            if self._startup_change_callback is None:
                self._action_status.setText(
                    "Start with Windows is unavailable in this build."
                )
                return
            target = not self._config.startup.start_with_windows
            success, detail = self._startup_change_callback(target)
            self._action_status.setText(detail)
            if success:
                self._sync_start_with_windows_setting_label()
            return
        if widget_id == "settings" and item_id == "safe_mode":
            if self._safe_mode_restart_callback is None:
                self._action_status.setText(
                    "Safe Mode restart is unavailable in this build."
                )
                return
            _, detail = self._safe_mode_restart_callback()
            self._action_status.setText(detail)
            return
        if widget_id == "settings" and item_id == "reset_window_position":
            if self._reset_window_position_callback is None:
                self._action_status.setText(
                    "Overlay position reset is unavailable in this build."
                )
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
        selected_path = (
            f"Selected {widget_id.replace('_', ' ')} / {item_id.replace('_', ' ')}"
        )
        self._action_status.setText(selected_path)
        _LOGGER.info("Compact widget navigation activated %s/%s", widget_id, item_id)

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
        _LOGGER.info(
            "Xbox/Guide button overlay toggle %s", "enabled" if enabled else "disabled"
        )

    def _sync_guide_button_setting_label(self) -> None:
        settings_view = self._navigation.settings_view
        if settings_view is not None:
            settings_view.set_guide_button_enabled(
                self._config.controller.guide_button_enabled
            )

    def _toggle_mouse_navigation_setting(self) -> None:
        previous = (
            self._config.controller.allow_mouse_navigation_while_controller_connected
        )
        enabled = not previous
        self._config.controller.allow_mouse_navigation_while_controller_connected = (
            enabled
        )
        try:
            self._persist_config_callback(self._config)
        except (OSError, VigilOverlayError):
            self._config.controller.allow_mouse_navigation_while_controller_connected = (
                previous
            )
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
            settings_view.set_start_with_windows_enabled(
                self._config.startup.start_with_windows
            )

    def _sync_run_in_background_setting_label(self) -> None:
        settings_view = self._navigation.settings_view
        if settings_view is not None:
            settings_view.set_run_in_background_enabled(
                self._config.background.run_in_background
            )

    def _on_widget_changed(self, widget_id: str) -> None:
        self._hide_widget_options()
        audio_view = self._navigation.audio_view
        if (
            widget_id != "audio"
            and audio_view is not None
            and audio_view.interaction_active
        ):
            audio_view.cancel_interaction()
        if widget_id == "audio" and audio_view is not None:
            QTimer.singleShot(0, audio_view.refresh)

        wifi_view = self._navigation.wifi_view
        if (
            widget_id != "wifi"
            and wifi_view is not None
            and wifi_view.interaction_active
        ):
            wifi_view.cancel_interaction()
        if widget_id == "wifi" and wifi_view is not None:
            wifi_view.refresh()

        display_view = self._navigation.display_view
        if (
            widget_id != "display"
            and display_view is not None
            and display_view.interaction_active
        ):
            # Dropdowns must never survive after Display is no longer active. A
            # pending temporary display change is also reverted here for safety.
            display_view.cancel_interaction()
        self._sync_widget_options()
        self._apply_panel_geometry()
        if widget_id == "display" and display_view is not None:
            display_view.refresh_screen_values(
                self.screen() or QGuiApplication.primaryScreen()
            )

    def _schedule_panel_geometry_refresh(self) -> None:
        QTimer.singleShot(0, self._apply_panel_geometry)

    def _apply_panel_geometry(self) -> None:
        """Fit the active widget naturally, then cap it to the active monitor height."""

        if not hasattr(self, "_compact_panel") or not hasattr(self, "_navigation"):
            return
        self._compact_panel.setFixedWidth(self._navigation.current_panel_width)

        margins = self._root_layout.contentsMargins()
        panel_top = self._compact_panel.y()
        if panel_top <= 0:
            panel_top = margins.top()
        bottom_margin = max(margins.bottom(), _PANEL_BOTTOM_SAFETY_MARGIN)
        available_height = max(self.height() - panel_top - bottom_margin, 1)
        natural_height = max(
            self._navigation.current_natural_panel_height,
            _MIN_COMPACT_PANEL_HEIGHT,
        )
        target_height = min(natural_height, available_height)
        self._compact_panel.setFixedHeight(target_height)

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
                description = (
                    f"Open the {definition.label} widget. This widget cannot be closed."
                )
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
            definition.widget_id
            for definition in self._widget_registry.visible_widgets()
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
            _LOGGER.exception(
                "Could not persist widget visibility change for %s", widget_id
            )
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
        definition = self._navigation.widget_definition(
            self._navigation.selected_widget_id
        )
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
        definition = self._navigation.widget_definition(
            self._navigation.selected_widget_id
        )
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

    @staticmethod
    def _set_navigation_focus(widget: QWidget, active: bool) -> None:
        widget.setProperty("navigationFocus", active)
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()

    def _restore_geometry(self) -> None:
        target = self._target_screen_geometry()
        self.setGeometry(target)

    def _target_screen_geometry(self, *, force_primary: bool = False) -> QRect:
        screens = QGuiApplication.screens()
        primary = QGuiApplication.primaryScreen()
        if force_primary and primary is not None:
            return primary.geometry()

        settings = self._config.window
        if settings.x is not None and settings.y is not None:
            center = QPoint(
                settings.x + max(settings.width, 1) // 2,
                settings.y + max(settings.height, 1) // 2,
            )
            for screen in screens:
                if screen.geometry().contains(center):
                    return screen.geometry()

        current_screen = self.screen()
        if current_screen is not None and current_screen in screens:
            return current_screen.geometry()

        if QGuiApplication.platformName() not in {"offscreen", "minimal"}:
            cursor_screen = QGuiApplication.screenAt(QCursor.pos())
            if cursor_screen is not None:
                return cursor_screen.geometry()

        if primary is not None:
            return primary.geometry()
        return QRect(0, 0, 1280, 720)

    def set_background_available(self, available: bool) -> None:
        self._background_available = available

    def reset_position(self) -> None:
        target = self._target_screen_geometry(force_primary=True)
        self.setGeometry(target)
        self._persist_geometry()

    def show_overlay(self) -> None:
        self._input_release_announced = False
        self._install_controller_mouse_event_filter()
        target = self._target_screen_geometry()
        self._backdrop.show_backdrop(target)
        self.setGeometry(target)
        self._apply_panel_geometry()
        self._update_clock()
        self.show()
        self._configure_native_window()
        self._reassert_topmost()
        if QGuiApplication.platformName() not in {"offscreen", "minimal"}:
            self.raise_()
            self.activateWindow()
        display_view = self._navigation.display_view
        if display_view is not None:
            display_view.refresh_screen_values(
                self.screen() or QGuiApplication.primaryScreen()
            )
        audio_view = self._navigation.audio_view
        if audio_view is not None and self._navigation.selected_widget_id == "audio":
            audio_view.refresh()
        wifi_view = self._navigation.wifi_view
        if wifi_view is not None and self._navigation.selected_widget_id == "wifi":
            wifi_view.refresh()
        self._navigation.restore_focus()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self._sync_controller_mouse_cursor()

    def request_hide(self) -> None:
        self._announce_input_release()
        self._pending_keyboard_back_timer.stop()
        self._pending_keyboard_back_at = None
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

        _LOGGER.warning(
            "No background restore mechanism is available; closing application"
        )
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
        if self._allow_close or not self._background_available:
            self._status_timer.stop()
            if self._status_runtime is not None:
                self._status_runtime.close()
            self._backdrop.close()
            event.accept()
            return
        event.ignore()
        self.hide()
        self._backdrop.hide()
        self.hidden_to_background.emit()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._status_timer.start()
        self._refresh_status_cluster()
        self._install_controller_mouse_event_filter()
        if not self._backdrop.isVisible():
            self._backdrop.show_backdrop(self.geometry())
        QTimer.singleShot(0, self._configure_native_window)
        QTimer.singleShot(0, self._reassert_topmost)
        QTimer.singleShot(0, self._apply_panel_geometry)

    def hideEvent(self, event: QHideEvent) -> None:
        self._announce_input_release()
        self._status_timer.stop()
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
        if not self.isVisible():
            return
        target = self._target_screen_geometry()
        self._backdrop.setGeometry(target)
        self.setGeometry(target)
        self._apply_panel_geometry()
        display_view = self._navigation.display_view
        if display_view is not None:
            display_view.refresh_screen_values(
                self.screen() or QGuiApplication.primaryScreen()
            )
        self._configure_native_window()
        self._reassert_topmost()
        self._display_settle_timer.start()

    def _settle_display_geometry(self) -> None:
        if not self.isVisible():
            return
        target = self._target_screen_geometry()
        self._backdrop.setGeometry(target)
        self.setGeometry(target)
        self._apply_panel_geometry()
        display_view = self._navigation.display_view
        if display_view is not None:
            display_view.refresh_screen_values(
                self.screen() or QGuiApplication.primaryScreen()
            )
        self._configure_native_window()
        self._reassert_topmost()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._input_policy.mode is OverlayInputMode.FOREGROUND_PENDING:
            event.accept()
            return
        command = navigation_command_for_key(event)
        if command is not None:
            now = time.monotonic()
            if not self._input_policy.route_native_controller_commands:
                if command is NavigationCommand.ACTIVATE:
                    display_view = self._navigation.display_view
                    if display_view is not None:
                        display_view.set_next_input_source(ModalInputSource.KEYBOARD)
                    integrations_view = self._navigation.integrations_view
                    if (
                        integrations_view is not None
                        and not integrations_view.interaction_active
                    ):
                        integrations_view.set_next_input_source(
                            ModalInputSource.KEYBOARD
                        )
                self.handle_navigation_command(command)
                event.accept()
                return
            self._last_keyboard_command_at[command] = now
            if command is NavigationCommand.BACK:
                self._queue_keyboard_back(now=now)
                event.accept()
                return
            controller_at = self._last_controller_command_at.get(command)
            if (
                controller_at is None
                or now - controller_at > _CONTROLLER_DUPLICATE_WINDOW_SECONDS
            ):
                if command is NavigationCommand.ACTIVATE:
                    display_view = self._navigation.display_view
                    if display_view is not None:
                        display_view.set_next_input_source(ModalInputSource.KEYBOARD)
                    integrations_view = self._navigation.integrations_view
                    if (
                        integrations_view is not None
                        and not integrations_view.interaction_active
                    ):
                        integrations_view.set_next_input_source(
                            ModalInputSource.KEYBOARD
                        )
                self.handle_navigation_command(command)
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
        self._backdrop.setGeometry(self.geometry())
        self._schedule_geometry_persist()
        super().moveEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._backdrop.setGeometry(self.geometry())
        self._schedule_geometry_persist()
        super().resizeEvent(event)
        self._schedule_panel_geometry_refresh()
        if self._options_popup.isVisible():
            self._position_widget_options_popup()

    def _schedule_geometry_persist(self) -> None:
        if self.isVisible():
            self._geometry_timer.start()

    def _persist_geometry(self) -> None:
        geometry = self.geometry()
        settings = self._config.window
        current_values = (
            settings.x,
            settings.y,
            settings.width,
            settings.height,
        )
        new_values = (
            geometry.x(),
            geometry.y(),
            geometry.width(),
            geometry.height(),
        )
        if current_values == new_values:
            return

        settings.x, settings.y, settings.width, settings.height = new_values
        try:
            self._persist_config_callback(self._config)
        except (OSError, VigilOverlayError):
            settings.x, settings.y, settings.width, settings.height = current_values
            _LOGGER.exception("Could not persist overlay geometry")
            return
        _LOGGER.debug("Persisted overlay screen geometry: %s", new_values)

    def _refresh_status_cluster(self) -> None:
        if self._status_runtime is not None:
            self._status_runtime.request_refresh()
            snapshot = self._last_status_snapshot
        else:
            try:
                backend = self._status_backend
                snapshot = (
                    backend.snapshot()
                    if backend is not None
                    else OverlayStatusSnapshot()
                )
            except Exception:
                _LOGGER.debug("Overlay header status refresh failed", exc_info=True)
                snapshot = OverlayStatusSnapshot()
        self._render_status_snapshot(snapshot)

    def _set_status_snapshot(self, snapshot: object) -> None:
        if not isinstance(snapshot, OverlayStatusSnapshot):
            return
        self._last_status_snapshot = snapshot
        self._render_status_snapshot(snapshot)

    def _render_status_snapshot(self, snapshot: OverlayStatusSnapshot) -> None:
        self._apply_microphone_status(snapshot.microphone_muted)
        self._apply_power_status(snapshot)
        self._apply_controller_battery_status(self._read_controller_battery_status())
        self._apply_network_status(snapshot.network_connected)
        self._update_clock()

    def _apply_microphone_status(self, muted: bool | None) -> None:
        if muted is True:
            text = "⊘"
            tooltip = "Microphone muted"
            state = "off"
        elif muted is False:
            text = "●"
            tooltip = "Microphone unmuted"
            state = "active"
        else:
            text = "?"
            tooltip = "Microphone status unavailable"
            state = "unknown"
        self._set_status_label(self._microphone_status_label, text, tooltip, state)

    def _apply_power_status(self, snapshot: OverlayStatusSnapshot) -> None:
        if snapshot.battery_present is False:
            text = "⚡" if snapshot.power_plugged is not False else "▰"
            tooltip = (
                "AC power"
                if snapshot.power_plugged is not False
                else "Power status available"
            )
            state = "active"
        elif snapshot.battery_present is True:
            percent = snapshot.battery_percent
            if snapshot.power_plugged is True:
                text = f"⚡ {percent}%" if percent is not None else "⚡"
                tooltip = (
                    f"Battery {percent}% · plugged in"
                    if percent is not None
                    else "Battery plugged in"
                )
                state = "active"
            else:
                glyph = "▰" if percent is None or percent > 20 else "▱"
                text = f"{glyph} {percent}%" if percent is not None else glyph
                tooltip = (
                    f"Battery {percent}%" if percent is not None else "On battery power"
                )
                state = "warning" if percent is not None and percent <= 20 else "active"
        elif snapshot.power_plugged is True:
            text = "⚡"
            tooltip = "Plugged in"
            state = "active"
        else:
            text = "?"
            tooltip = "Power status unavailable"
            state = "unknown"
        self._set_status_label(self._power_status_label, text, tooltip, state)

    def _read_controller_battery_status(self) -> ControllerBatterySnapshot:
        provider = self._controller_battery_status
        if provider is None:
            return ControllerBatterySnapshot()
        try:
            return provider()
        except Exception:
            _LOGGER.debug("Controller battery status refresh failed", exc_info=True)
            return ControllerBatterySnapshot(connected=True)

    def _apply_controller_battery_status(
        self, snapshot: ControllerBatterySnapshot
    ) -> None:
        if not snapshot.connected:
            self._controller_battery_label.setVisible(False)
            return

        self._controller_battery_label.setVisible(True)
        percent = snapshot.battery_percent
        approximate_prefix = (
            "~" if snapshot.approximate_percent and percent is not None else ""
        )
        if snapshot.battery_present is False:
            text = "🎮"
            tooltip = "Controller connected · wired"
            state = "active"
        elif percent is not None:
            text = f"🎮 {approximate_prefix}{percent}%"
            level = snapshot.level_label or "battery"
            approximation = "approximately " if snapshot.approximate_percent else ""
            tooltip = f"Controller battery {level} · {approximation}{percent}%"
            state = "warning" if level in {"critical", "low"} else "active"
        else:
            text = "🎮"
            tooltip = "Controller connected · battery status unavailable"
            state = "unknown"
        self._set_status_label(self._controller_battery_label, text, tooltip, state)

    def _apply_network_status(self, connected: bool | None) -> None:
        if connected is True:
            text = "⌁"
            tooltip = "Network connected"
            state = "active"
        elif connected is False:
            text = "X"
            tooltip = "Network disconnected"
            state = "off"
        else:
            text = "?"
            tooltip = "Network status unavailable"
            state = "unknown"
        self._set_status_label(self._network_status_label, text, tooltip, state)

    @staticmethod
    def _set_status_label(label: QLabel, text: str, tooltip: str, state: str) -> None:
        label.setText(text)
        label.setToolTip(tooltip)
        label.setAccessibleName(tooltip)
        label.setProperty("statusState", state)
        repolish_widget(label)

    def _update_clock(self) -> None:
        self._clock_label.setText(datetime.now().strftime("%H:%M"))
