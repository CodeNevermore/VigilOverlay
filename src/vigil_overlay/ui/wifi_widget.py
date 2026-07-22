"""Controller-first Windows-managed saved-profile Wi-Fi widget surface."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from vigil_overlay.services.wifi_control import (
    WifiControlBackend,
    WifiControlError,
    WifiProfileInfo,
    WifiSnapshot,
    create_platform_wifi_control_backend,
)
from vigil_overlay.ui.controls import (
    SelectorToggleAction,
    VigilSelectorButton,
    VigilToggleSwitch,
    repolish_widget,
    selector_toggle_action,
)
from vigil_overlay.ui.selector_popup import SelectorPopup
from vigil_overlay.widgets.registry import WidgetDefinition, WidgetItemDefinition

_LOGGER = logging.getLogger("vigil_overlay")
_REFRESH_MS = 5000
_CONNECTED_IDENTITY_RETRY_MS = 350
_MAX_CONNECTED_IDENTITY_RETRIES = 4


class WifiToggleButton(QPushButton):
    """Controller-focusable Wi-Fi software-radio toggle."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("wifiToggleButton")
        self.setProperty("itemId", "wifi_toggle")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("Turn the Windows Wi-Fi software radio on or off.")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(12)
        label = QLabel("Wi-Fi", self)
        label.setObjectName("toggleRowTitle")
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(label, 1)
        self.toggle_switch = VigilToggleSwitch(self)
        layout.addWidget(self.toggle_switch, 0, Qt.AlignmentFlag.AlignVCenter)
        self.set_state(None)

    def set_state(self, enabled: bool | None) -> None:
        self.toggle_switch.setChecked(bool(enabled))
        self.toggle_switch.setEnabled(enabled is not None)
        if enabled is None:
            self.setAccessibleName("Wi-Fi state unavailable")
        else:
            self.setAccessibleName(
                f"Wi-Fi {'On' if enabled else 'Off'}. Activate to toggle."
            )
        self.setProperty("wifiEnabled", enabled)
        repolish_widget(self)


class WifiProfileSelectorButton(VigilSelectorButton):
    """Compact saved-profile dropdown anchor."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("wifiProfileSelectorButton")
        self.setProperty("itemId", "profile_selector")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("Choose a Wi-Fi profile already saved by Windows.")
        self.set_profile_name(None)

    def set_profile_name(self, name: str | None, *, has_profiles: bool = False) -> None:
        if name:
            self.setText(name)
            self.setAccessibleName(
                f"Saved Wi-Fi profile: {name}. Activate to choose another."
            )
        elif has_profiles:
            self.setText("Choose saved network")
            self.setAccessibleName("Choose a saved Wi-Fi profile")
        else:
            self.setText("No saved Wi-Fi profiles")
            self.setAccessibleName("No saved Wi-Fi profiles are available")


class WifiActionButton(QPushButton):
    """Controller-focusable Wi-Fi action row."""

    def __init__(
        self, item_id: str, label: str, description: str, parent: QWidget
    ) -> None:
        super().__init__(label, parent)
        self.setObjectName("wifiActionButton")
        self.setProperty("itemId", item_id)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(description)
        self.setAccessibleName(f"{label}. {description}")


class WifiWidgetView(QWidget):
    """Location-free Wi-Fi surface using Windows-managed profiles and radio state."""

    items_changed = Signal(object, object)

    def __init__(
        self,
        definition: WidgetDefinition,
        parent: QWidget | None = None,
        *,
        backend: WifiControlBackend | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("wifiWidgetPage")
        self.setProperty("widgetId", definition.widget_id)
        self.setProperty("compactPage", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._definition = definition
        self._backend = backend or create_platform_wifi_control_backend()
        self._snapshot = WifiSnapshot(False, "Wi-Fi has not been queried yet.")
        self._item_definitions: tuple[WidgetItemDefinition, ...] = ()
        self._buttons: tuple[QPushButton, ...] = ()
        self._status_label: QLabel
        self._error_label: QLabel
        self._toggle_button: WifiToggleButton
        self._profile_selector: WifiProfileSelectorButton
        self._connect_button: WifiActionButton
        self._disconnect_button: WifiActionButton
        self._refresh_button: WifiActionButton
        self._settings_button: WifiActionButton
        self._profiles: tuple[WifiProfileInfo, ...] = ()
        self._selected_profile_key: tuple[str, str] | None = None
        self._selection_is_user_override = False
        self._pending_connection_key: tuple[str, str] | None = None
        self._choice_popup: SelectorPopup | None = None
        self._last_navigation_fingerprint: tuple[object, ...] | None = None
        self._connected_identity_retry_count = 0
        self._connected_identity_retry_timer = QTimer(self)
        self._connected_identity_retry_timer.setSingleShot(True)
        self._connected_identity_retry_timer.setInterval(_CONNECTED_IDENTITY_RETRY_MS)
        self._connected_identity_retry_timer.timeout.connect(self.refresh)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(_REFRESH_MS)
        self._refresh_timer.timeout.connect(self.refresh)
        self._build_ui()
        self.refresh()

    @property
    def item_buttons(self) -> tuple[QPushButton, ...]:
        return self._buttons

    @property
    def item_definitions(self) -> tuple[WidgetItemDefinition, ...]:
        return self._item_definitions

    @property
    def interaction_active(self) -> bool:
        return self._choice_popup is not None

    def showEvent(self, event: object) -> None:
        super().showEvent(event)  # type: ignore[arg-type]
        self.refresh()
        self._refresh_timer.start()

    def hideEvent(self, event: object) -> None:
        self._refresh_timer.stop()
        self._connected_identity_retry_timer.stop()
        self.cancel_interaction()
        super().hideEvent(event)  # type: ignore[arg-type]

    def refresh(self) -> None:
        try:
            snapshot = self._backend.snapshot()
        except WifiControlError as exc:
            snapshot = WifiSnapshot(False, str(exc))
        self._apply_snapshot(snapshot)

    def activate_item(self, item_id: str) -> bool:
        if item_id == "refresh":
            self.refresh()
            return True
        if item_id == "wifi_settings":
            return self._open_settings()
        if item_id == "profile_selector":
            return self.toggle_selector(item_id)
        if item_id == "wifi_toggle":
            if self._snapshot.wifi_enabled is None:
                self._show_error(
                    "Windows did not report a controllable Wi-Fi radio state."
                )
                return True
            try:
                self._backend.set_wifi_enabled(not self._snapshot.wifi_enabled)
            except WifiControlError as exc:
                self._show_error(str(exc))
                return True
            QTimer.singleShot(500, self.refresh)
            return True
        if item_id == "connect_profile":
            profile = self._selected_profile()
            if profile is None:
                self._show_error("Choose a saved Wi-Fi profile first.")
                return True
            if self._snapshot.wifi_enabled is False:
                self._show_error("Turn Wi-Fi on before connecting to a saved profile.")
                return True
            try:
                self._backend.connect(profile)
            except WifiControlError as exc:
                self._show_error(str(exc))
                return True
            self._pending_connection_key = _profile_key(profile)
            QTimer.singleShot(800, self.refresh)
            return True
        if item_id == "disconnect":
            try:
                self._backend.disconnect()
            except WifiControlError as exc:
                self._show_error(str(exc))
                return True
            QTimer.singleShot(500, self.refresh)
            return True
        return False

    def move_interaction(self, delta: int) -> bool:
        popup = self._choice_popup
        return popup.move_selection(delta) if popup is not None else False

    def activate_interaction(self) -> bool:
        popup = self._choice_popup
        return popup.activate_selection() if popup is not None else False

    def cancel_interaction(self) -> bool:
        if self._choice_popup is None:
            return False
        self._close_choice_popup()
        return True

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 22)
        root.setSpacing(10)

        title = QLabel(self._definition.label, self)
        title.setObjectName("wifiTitle")
        root.addWidget(title)

        self._status_label = QLabel("Checking Wi-Fi...", self)
        self._status_label.setObjectName("wifiStatusLabel")
        self._status_label.setWordWrap(True)
        root.addWidget(self._status_label)

        self._toggle_button = WifiToggleButton(self)
        root.addWidget(self._toggle_button)

        saved = QLabel("Saved network", self)
        saved.setObjectName("wifiSectionLabel")
        root.addWidget(saved)

        self._profile_selector = WifiProfileSelectorButton(self)
        root.addWidget(self._profile_selector)

        self._connect_button = WifiActionButton(
            "connect_profile",
            "Connect selected network",
            "Ask Windows to connect using the selected saved Wi-Fi profile.",
            self,
        )
        self._disconnect_button = WifiActionButton(
            "disconnect",
            "Disconnect Wi-Fi",
            "Ask Windows to disconnect the currently connected Wi-Fi interface.",
            self,
        )
        self._refresh_button = WifiActionButton(
            "refresh",
            "Refresh saved networks",
            "Reload Wi-Fi profiles already saved by Windows.",
            self,
        )
        self._settings_button = WifiActionButton(
            "wifi_settings",
            "Open Windows Wi-Fi",
            "Open Windows Wi-Fi settings to find, join, forget, or manage networks.",
            self,
        )
        root.addWidget(self._connect_button)
        root.addWidget(self._disconnect_button)
        root.addWidget(self._refresh_button)
        root.addWidget(self._settings_button)
        self._buttons = (
            self._toggle_button,
            self._profile_selector,
            self._connect_button,
            self._disconnect_button,
            self._refresh_button,
            self._settings_button,
        )

        self._error_label = QLabel(self)
        self._error_label.setObjectName("wifiErrorLabel")
        self._error_label.setWordWrap(True)
        self._error_label.hide()
        root.addWidget(self._error_label)

        hint = QLabel(
            "Vigil uses Wi-Fi profiles already managed by Windows. "
            "Open Windows Wi-Fi to find or join a new network.",
            self,
        )
        hint.setObjectName("wifiHelpLabel")
        hint.setWordWrap(True)
        root.addWidget(hint)
        root.addStretch(1)

    def _apply_snapshot(self, snapshot: WifiSnapshot) -> None:
        self._snapshot = snapshot
        self._profiles = snapshot.profiles
        self._preserve_selected_profile(snapshot)
        self._sync_profile_selector()
        self._toggle_button.set_state(snapshot.wifi_enabled)

        if snapshot.available:
            if snapshot.wifi_enabled is False:
                self._status_label.setText("Wi-Fi is off")
            else:
                connected_name = snapshot.connected_profile_name
                self._status_label.setText(
                    f"Connected to {connected_name}"
                    if snapshot.connected and connected_name
                    else (
                        "Connected through Wi-Fi"
                        if snapshot.connected
                        else "Not connected"
                    )
                )
            self._error_label.hide()
        else:
            self._status_label.setText("Wi-Fi unavailable")
            self._show_error(snapshot.detail)

        self._sync_connected_identity_retry(snapshot)

        can_toggle = snapshot.available and snapshot.wifi_enabled is not None
        has_profile = self._selected_profile() is not None
        can_connect = (
            snapshot.available and snapshot.wifi_enabled is not False and has_profile
        )
        self._toggle_button.setEnabled(can_toggle)
        self._profile_selector.setEnabled(
            snapshot.available and bool(snapshot.profiles)
        )
        self._connect_button.setEnabled(can_connect)
        self._disconnect_button.setEnabled(snapshot.available and snapshot.connected)
        self._refresh_button.setEnabled(snapshot.available)
        self._settings_button.setEnabled(True)

        definitions = self._navigation_definitions(snapshot, has_profile=has_profile)
        fingerprint: tuple[object, ...] = tuple(
            (item.item_id, item.enabled) for item in definitions
        )
        if fingerprint != self._last_navigation_fingerprint:
            self._last_navigation_fingerprint = fingerprint
            self._item_definitions = definitions
            self.items_changed.emit(self._item_definitions, self._buttons)

    def _sync_connected_identity_retry(self, snapshot: WifiSnapshot) -> None:
        identity_unresolved = (
            snapshot.available
            and snapshot.connected
            and snapshot.wifi_enabled is not False
            and snapshot.connected_profile_name is None
            and bool(snapshot.profiles)
        )
        if not identity_unresolved:
            self._connected_identity_retry_count = 0
            self._connected_identity_retry_timer.stop()
            return
        if (
            self._connected_identity_retry_count >= _MAX_CONNECTED_IDENTITY_RETRIES
            or self._connected_identity_retry_timer.isActive()
        ):
            return
        self._connected_identity_retry_count += 1
        self._connected_identity_retry_timer.start()

    def _navigation_definitions(
        self,
        snapshot: WifiSnapshot,
        *,
        has_profile: bool,
    ) -> tuple[WidgetItemDefinition, ...]:
        return (
            WidgetItemDefinition(
                "wifi_toggle",
                "Wi-Fi",
                "Turn the Windows Wi-Fi software radio on or off.",
                "wifi",
                enabled=snapshot.available and snapshot.wifi_enabled is not None,
            ),
            WidgetItemDefinition(
                "profile_selector",
                "Saved network",
                "Choose a Wi-Fi profile already saved by Windows.",
                "wifi",
                enabled=snapshot.available and bool(snapshot.profiles),
            ),
            WidgetItemDefinition(
                "connect_profile",
                "Connect selected network",
                "Ask Windows to connect using the selected saved Wi-Fi profile.",
                "wifi",
                enabled=(
                    snapshot.available
                    and snapshot.wifi_enabled is not False
                    and has_profile
                ),
            ),
            WidgetItemDefinition(
                "disconnect",
                "Disconnect Wi-Fi",
                "Ask Windows to disconnect the currently connected Wi-Fi interface.",
                "wifi",
                enabled=snapshot.available and snapshot.connected,
            ),
            WidgetItemDefinition(
                "refresh",
                "Refresh saved networks",
                "Reload Wi-Fi profiles already saved by Windows.",
                "wifi",
                enabled=snapshot.available,
            ),
            WidgetItemDefinition(
                "wifi_settings",
                "Open Windows Wi-Fi",
                "Open Windows Wi-Fi settings to find, join, forget, or manage networks.",
                "settings",
            ),
        )

    def _preserve_selected_profile(self, snapshot: WifiSnapshot) -> None:
        keys = {_profile_key(profile) for profile in self._profiles}
        connected_key = _connected_profile_key(snapshot, self._profiles)

        if self._pending_connection_key is not None:
            if connected_key == self._pending_connection_key:
                self._selected_profile_key = connected_key
                self._pending_connection_key = None
                self._selection_is_user_override = False
            elif self._pending_connection_key in keys:
                self._selected_profile_key = self._pending_connection_key
                return
            else:
                self._pending_connection_key = None

        if connected_key == self._selected_profile_key:
            self._selection_is_user_override = False
        if self._selection_is_user_override and self._selected_profile_key in keys:
            return
        if connected_key is not None:
            self._selected_profile_key = connected_key
            self._selection_is_user_override = False
            return
        if snapshot.connected and not self._selection_is_user_override:
            self._selected_profile_key = None
            return
        if self._selected_profile_key not in keys:
            self._selected_profile_key = (
                _profile_key(self._profiles[0]) if self._profiles else None
            )

    def _selected_profile(self) -> WifiProfileInfo | None:
        key = self._selected_profile_key
        if key is None:
            return None
        return next(
            (profile for profile in self._profiles if _profile_key(profile) == key),
            None,
        )

    def _sync_profile_selector(self) -> None:
        profile = self._selected_profile()
        self._profile_selector.set_profile_name(
            profile.profile_name if profile is not None else None,
            has_profiles=bool(self._profiles),
        )

    def toggle_selector(self, item_id: str) -> bool:
        """Open or close the saved-profile selector without forcing a choice."""

        if item_id != "profile_selector":
            return False
        action = selector_toggle_action(
            "profile_selector" if self._choice_popup is not None else None,
            item_id,
        )
        if action is SelectorToggleAction.CLOSE:
            self._close_choice_popup()
            return True
        if not self._profiles:
            self._show_error("No saved Wi-Fi profiles are available.")
            return True
        selected = self._selected_profile_key
        selected_index = next(
            (
                index
                for index, profile in enumerate(self._profiles)
                if _profile_key(profile) == selected
            ),
            0,
        )
        popup = SelectorPopup(
            self,
            anchor=self._profile_selector,
            option_labels=tuple(profile.profile_name for profile in self._profiles),
            selected_index=selected_index,
            object_prefix="wifi",
            option_selected=self._select_profile,
        )
        self._choice_popup = popup
        self._profile_selector.set_selector_open(True)
        popup.show_anchored()
        return True

    def _select_profile(self, index: int) -> None:
        if not 0 <= index < len(self._profiles):
            return
        profile = self._profiles[index]
        self._selected_profile_key = _profile_key(profile)
        connected_key = _connected_profile_key(self._snapshot, self._profiles)
        self._selection_is_user_override = self._selected_profile_key != connected_key
        self._pending_connection_key = None
        self._close_choice_popup()
        self._sync_profile_selector()

    def _close_choice_popup(self) -> None:
        popup = self._choice_popup
        self._choice_popup = None
        self._profile_selector.set_selector_open(False)
        if popup is not None:
            popup.dispose()

    def _open_settings(self) -> bool:
        try:
            self._backend.open_wifi_settings()
        except WifiControlError as exc:
            self._show_error(str(exc))
        return True

    def _show_error(self, detail: str) -> None:
        self._error_label.setText(detail)
        self._error_label.show()
        _LOGGER.debug("Wi-Fi widget status: %s", detail)


def _profile_key(profile: WifiProfileInfo) -> tuple[str, str]:
    return profile.interface_id, profile.profile_name.casefold()


def _connected_profile_key(
    snapshot: WifiSnapshot,
    profiles: tuple[WifiProfileInfo, ...],
) -> tuple[str, str] | None:
    name = snapshot.connected_profile_name
    if not snapshot.connected or not name:
        return None
    normalized = name.casefold()
    profile = next(
        (item for item in profiles if item.profile_name.casefold() == normalized),
        None,
    )
    return _profile_key(profile) if profile is not None else None
