"""Overlay navigation routing, input arbitration, and focus coordination."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QWidget

from vigil_overlay.core.input_routing import OverlayInputPolicy
from vigil_overlay.ui.dialog_coordination import OverlayDialogCoordinator
from vigil_overlay.ui.modal_guard import ModalInputSource
from vigil_overlay.ui.navigation import (
    FocusZone,
    NavigationCommand,
    NavigationOutcome,
    NavigationResult,
)

if TYPE_CHECKING:
    from vigil_overlay.ui.audio_widget import AudioWidgetView
    from vigil_overlay.ui.display_widget import DisplayWidgetView
    from vigil_overlay.ui.integrations_widget import IntegrationsWidgetView
    from vigil_overlay.ui.settings_widget import SettingsWidgetView
    from vigil_overlay.ui.wifi_widget import WifiWidgetView

_DUPLICATE_WINDOW_SECONDS = 0.14
_ITEM_ACTIVATION_DEDUP_SECONDS = 0.30
InputPolicyProvider = Callable[[], OverlayInputPolicy]
NavigationCallback = Callable[[], None]
VisibilityCallback = Callable[[], bool]
Clock = Callable[[], float]


class DirectionalInteraction(Protocol):
    @property
    def interaction_active(self) -> bool: ...

    def move_interaction(self, delta: int) -> bool: ...

    def activate_interaction(self) -> bool: ...

    def cancel_interaction(self) -> bool: ...


class NavigationSurface(Protocol):
    @property
    def selected_widget_id(self) -> str: ...

    @property
    def selected_item_id(self) -> str | None: ...

    @property
    def selected_item_index(self) -> int | None: ...

    @property
    def focus_zone(self) -> FocusZone: ...

    @property
    def audio_view(self) -> AudioWidgetView | None: ...

    @property
    def display_view(self) -> DisplayWidgetView | None: ...

    @property
    def integrations_view(self) -> IntegrationsWidgetView | None: ...

    @property
    def settings_view(self) -> SettingsWidgetView | None: ...

    @property
    def wifi_view(self) -> WifiWidgetView | None: ...

    def handle_command(self, command: NavigationCommand) -> NavigationResult: ...

    def restore_focus(self) -> None: ...


class OverlayNavigationCoordinator(QObject):
    """Own cross-device arbitration and route commands to the active UI layer."""

    def __init__(
        self,
        host: QWidget,
        *,
        navigation: NavigationSurface,
        dialogs: OverlayDialogCoordinator,
        input_policy: InputPolicyProvider,
        options_visible: VisibilityCallback,
        toggle_options: NavigationCallback,
        hide_options: NavigationCallback,
        close_selected_widget: NavigationCallback,
        request_hide: NavigationCallback,
        begin_controller_mouse_guard: NavigationCallback,
        controller_mouse_guard_active: VisibilityCallback,
        clock: Clock = time.monotonic,
    ) -> None:
        super().__init__(host)
        self._host = host
        self._navigation = navigation
        self._dialogs = dialogs
        self._input_policy = input_policy
        self._options_visible = options_visible
        self._toggle_options = toggle_options
        self._hide_options = hide_options
        self._close_selected_widget = close_selected_widget
        self._request_hide = request_hide
        self._begin_controller_mouse_guard = begin_controller_mouse_guard
        self._controller_mouse_guard_active = controller_mouse_guard_active
        self._clock = clock
        self._last_controller_command_at: dict[NavigationCommand, float] = {}
        self._last_keyboard_command_at: dict[NavigationCommand, float] = {}
        self._last_item_activation: tuple[str, str, float] | None = None
        self._controller_widget_direction_latch: NavigationCommand | None = None
        self._pending_keyboard_back_at: float | None = None
        self.pending_keyboard_back_timer = QTimer(self)
        self.pending_keyboard_back_timer.setSingleShot(True)
        self.pending_keyboard_back_timer.setInterval(int(_DUPLICATE_WINDOW_SECONDS * 1000) + 20)
        self.pending_keyboard_back_timer.timeout.connect(self.flush_pending_keyboard_back)

    def reset_input_arbitration(self) -> None:
        """Clear transient ownership whenever the application input route changes."""

        self._controller_widget_direction_latch = None
        self.cancel_pending_keyboard_back()
        self._last_controller_command_at.clear()
        self._last_keyboard_command_at.clear()
        self._last_item_activation = None

    def handle_controller_command(self, command: NavigationCommand) -> NavigationResult:
        """Apply one physical-controller command with duplicate-input arbitration."""

        if not self._input_policy().route_native_controller_commands:
            return self.current_result()

        now = self._clock()
        if (
            command in {NavigationCommand.MOVE_LEFT, NavigationCommand.MOVE_RIGHT}
            and self._controller_widget_direction_latch is not None
        ):
            self._begin_controller_mouse_guard()
            return self.current_result()
        pending_back_owned = self._cancel_correlated_pending_keyboard_back(
            command,
            now=now,
        )
        keyboard_at = self._last_keyboard_command_at.get(command)
        self._last_controller_command_at[command] = now
        self._begin_controller_mouse_guard()
        if (
            not pending_back_owned
            and keyboard_at is not None
            and now - keyboard_at <= _DUPLICATE_WINDOW_SECONDS
        ):
            return self.current_result()
        if command is NavigationCommand.ACTIVATE:
            self._prepare_activation(ModalInputSource.CONTROLLER)
        result = self.handle_command(command)
        if result.outcome is NavigationOutcome.WIDGET_CHANGED and command in {
            NavigationCommand.MOVE_LEFT,
            NavigationCommand.MOVE_RIGHT,
        }:
            self._controller_widget_direction_latch = command
        return result

    def handle_keyboard_command(self, command: NavigationCommand) -> NavigationResult:
        """Apply one keyboard command while collapsing controller-synthesized copies."""

        now = self._clock()
        if not self._input_policy().route_native_controller_commands:
            if command is NavigationCommand.ACTIVATE:
                self._prepare_activation(ModalInputSource.KEYBOARD)
            return self.handle_command(command)

        self._last_keyboard_command_at[command] = now
        if command is NavigationCommand.BACK:
            self._queue_keyboard_back(now=now)
            return self.current_result()

        controller_at = self._last_controller_command_at.get(command)
        if controller_at is None or now - controller_at > _DUPLICATE_WINDOW_SECONDS:
            if command is NavigationCommand.ACTIVATE:
                self._prepare_activation(ModalInputSource.KEYBOARD)
            return self.handle_command(command)
        return self.current_result()

    def notify_controller_direction_released(self, command: NavigationCommand) -> None:
        """Release the top-level widget-direction latch after physical neutral."""

        if self._controller_widget_direction_latch is command:
            self._controller_widget_direction_latch = None

    def notify_controller_activation_released(self) -> None:
        """Release-gate active selectors, editors, confirmations, and host modals."""

        display_view = self._navigation.display_view
        if display_view is not None:
            display_view.notify_controller_activation_released()
        integrations_view = self._navigation.integrations_view
        if integrations_view is not None:
            integrations_view.notify_controller_activation_released()
        settings_view = self._navigation.settings_view
        if settings_view is not None:
            settings_view.notify_controller_activation_released()
        self._dialogs.notify_controller_activation_released()

    def handle_command(self, command: NavigationCommand) -> NavigationResult:
        """Route one device-neutral command to the active interaction layer."""

        if command in {
            NavigationCommand.PREVIOUS_WIDGET,
            NavigationCommand.NEXT_WIDGET,
        }:
            self.cancel_interactions_for_widget_switch()
            return self._navigation.handle_command(command)

        if self._options_visible():
            if command is NavigationCommand.ACTIVATE:
                self._close_selected_widget()
            elif command in {NavigationCommand.BACK, NavigationCommand.OPEN_OPTIONS}:
                self._hide_options()
            return self.current_result()

        if self._dialogs.handle_controller_command(command):
            return self.current_result()

        settings_view = self._navigation.settings_view
        if settings_view is not None and settings_view.interaction_active:
            settings_view.handle_controller_command(command)
            return self.current_result()

        integrations_view = self._navigation.integrations_view
        if self._route_directional_interaction(
            integrations_view,
            command,
            consume_unhandled=True,
        ):
            return self.current_result()

        audio_view = self._navigation.audio_view
        if self._route_directional_interaction(audio_view, command):
            return self.current_result()

        wifi_view = self._navigation.wifi_view
        if self._route_directional_interaction(
            wifi_view,
            command,
            consume_unhandled=True,
        ):
            return self.current_result()

        if (
            audio_view is not None
            and self._navigation.selected_widget_id == "audio"
            and self._navigation.focus_zone is FocusZone.CONTENT
            and command in {NavigationCommand.MOVE_LEFT, NavigationCommand.MOVE_RIGHT}
        ):
            item_id = self._navigation.selected_item_id
            if item_id is not None and audio_view.adjust_item(
                item_id,
                1 if command is NavigationCommand.MOVE_RIGHT else -1,
            ):
                return self.current_result()

        display_view = self._navigation.display_view
        if self._route_directional_interaction(display_view, command):
            return self.current_result()

        if command is NavigationCommand.OPEN_OPTIONS:
            self._toggle_options()
            return self.current_result()

        result = self._navigation.handle_command(command)
        if result.outcome in {
            NavigationOutcome.HIDE_REQUESTED,
            NavigationOutcome.TOGGLE_REQUESTED,
        }:
            self._request_hide()
        return result

    def cancel_interactions_for_widget_switch(self) -> None:
        """Close transient UI owned by the widget being left."""

        if self._options_visible():
            self._hide_options()
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
        settings_view = self._navigation.settings_view
        if settings_view is not None and settings_view.interaction_active:
            settings_view.handle_controller_command(NavigationCommand.BACK)

    def accepts_item_activation(self, widget_id: str, item_id: str) -> bool:
        """Reject a controller-correlated synthetic click for the same item."""

        now = self._clock()
        previous = self._last_item_activation
        if (
            self._controller_mouse_guard_active()
            and previous is not None
            and previous[0] == widget_id
            and previous[1] == item_id
            and now - previous[2] <= _ITEM_ACTIVATION_DEDUP_SECONDS
        ):
            return False
        self._last_item_activation = (widget_id, item_id, now)
        return True

    def cancel_pending_keyboard_back(self) -> None:
        """Discard any delayed synthetic Escape action."""

        self._pending_keyboard_back_at = None
        self.pending_keyboard_back_timer.stop()

    def flush_pending_keyboard_back(self) -> None:
        """Dispatch a delayed Escape when no controller command claimed it."""

        if self._pending_keyboard_back_at is None:
            return
        self._pending_keyboard_back_at = None
        if self._host.isVisible():
            self.handle_command(NavigationCommand.BACK)

    def current_result(self) -> NavigationResult:
        """Return the current focus snapshot without changing navigation state."""

        return NavigationResult(
            outcome=NavigationOutcome.NO_CHANGE,
            selected_widget_id=self._navigation.selected_widget_id,
            focus_zone=self._navigation.focus_zone,
            selected_item_index=self._navigation.selected_item_index,
            selected_item_id=self._navigation.selected_item_id,
        )

    def restore_focus(self) -> None:
        """Restore NavigationShell's managed visual and Qt focus."""

        self._navigation.restore_focus()

    def _prepare_activation(self, source: ModalInputSource) -> None:
        display_view = self._navigation.display_view
        if display_view is not None:
            display_view.set_next_input_source(source)
        integrations_view = self._navigation.integrations_view
        if integrations_view is not None and not integrations_view.interaction_active:
            integrations_view.set_next_input_source(source)
        if source is ModalInputSource.CONTROLLER:
            settings_view = self._navigation.settings_view
            if settings_view is not None and not settings_view.interaction_active:
                settings_view.set_next_input_source(source)
            if (
                self._navigation.selected_widget_id == "home"
                and self._navigation.selected_item_id == "power"
            ):
                self._dialogs.set_next_power_input_source(source)

    def _cancel_correlated_pending_keyboard_back(
        self,
        command: NavigationCommand,
        *,
        now: float,
    ) -> bool:
        if command not in {NavigationCommand.BACK, NavigationCommand.OPEN_OPTIONS}:
            return False
        pending_at = self._pending_keyboard_back_at
        if pending_at is None or not self.pending_keyboard_back_timer.isActive():
            return False
        if now - pending_at > _DUPLICATE_WINDOW_SECONDS:
            return False
        self.cancel_pending_keyboard_back()
        return True

    def _queue_keyboard_back(self, *, now: float) -> None:
        recent_controller = max(
            (
                self._last_controller_command_at.get(
                    NavigationCommand.BACK,
                    float("-inf"),
                ),
                self._last_controller_command_at.get(
                    NavigationCommand.OPEN_OPTIONS,
                    float("-inf"),
                ),
            )
        )
        if now - recent_controller <= _DUPLICATE_WINDOW_SECONDS:
            self.cancel_pending_keyboard_back()
            return
        self._pending_keyboard_back_at = now
        self.pending_keyboard_back_timer.start()

    @staticmethod
    def _route_directional_interaction(
        interaction: DirectionalInteraction | None,
        command: NavigationCommand,
        *,
        consume_unhandled: bool = False,
    ) -> bool:
        if interaction is None or not interaction.interaction_active:
            return False
        if command in {NavigationCommand.MOVE_UP, NavigationCommand.MOVE_LEFT}:
            interaction.move_interaction(-1)
            return True
        if command in {NavigationCommand.MOVE_DOWN, NavigationCommand.MOVE_RIGHT}:
            interaction.move_interaction(1)
            return True
        if command is NavigationCommand.ACTIVATE:
            interaction.activate_interaction()
            return True
        if command is NavigationCommand.BACK:
            interaction.cancel_interaction()
            return True
        return consume_unhandled


__all__ = ["OverlayNavigationCoordinator"]
