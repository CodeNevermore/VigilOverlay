"""Header status-cluster presentation and refresh ownership."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from vigil_overlay.contracts.controller import ControllerBatterySnapshot
from vigil_overlay.services.system_status import (
    OverlayStatusBackend,
    OverlayStatusRuntime,
    OverlayStatusSnapshot,
)
from vigil_overlay.ui.controls import repolish_widget

_LOGGER = logging.getLogger("vigil_overlay")
ControllerBatteryStatus = Callable[[], ControllerBatterySnapshot]
StatusClock = Callable[[], datetime]
HideCallback = Callable[[], None]


class OverlayStatusClusterController(QObject):
    """Own the header status view, sampling lifecycle, and rendering rules."""

    def __init__(
        self,
        parent: QWidget,
        *,
        hide_callback: HideCallback,
        status_backend: OverlayStatusBackend | None = None,
        controller_battery_status: ControllerBatteryStatus | None = None,
        clock: StatusClock = datetime.now,
    ) -> None:
        super().__init__(parent)
        self._status_backend = status_backend
        self._controller_battery_status = controller_battery_status
        self._clock = clock
        self._last_snapshot = OverlayStatusSnapshot()

        self.frame = QFrame(parent)
        self.frame.setObjectName("overlayStatusCluster")
        layout = QHBoxLayout(self.frame)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(13)

        self.microphone_label = self._status_label("?", self.frame)
        self.power_label = self._status_label("?", self.frame)
        self.controller_battery_label = self._status_label("", self.frame)
        self.controller_battery_label.setVisible(False)
        self.network_label = self._status_label("?", self.frame)
        self.clock_label = QLabel(self.frame)
        self.clock_label.setObjectName("overlayClock")
        hide_button = QPushButton("X", self.frame)
        hide_button.setObjectName("overlayHideButton")
        hide_button.setToolTip("Hide Vigil Overlay")
        hide_button.setAccessibleName("Hide Vigil Overlay")
        hide_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        hide_button.clicked.connect(hide_callback)

        layout.addWidget(self.microphone_label)
        layout.addWidget(self.power_label)
        layout.addWidget(self.controller_battery_label)
        layout.addWidget(self.network_label)
        layout.addWidget(self.clock_label)
        layout.addWidget(hide_button)
        self.frame.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh)
        self._status_runtime = (
            None if status_backend is not None else OverlayStatusRuntime(parent=self)
        )
        if self._status_runtime is not None:
            self._status_runtime.snapshot_ready.connect(self._set_snapshot)

    @staticmethod
    def _status_label(text: str, parent: QWidget) -> QLabel:
        label = QLabel(text, parent)
        label.setObjectName("statusGlyph")
        return label

    def start(self) -> None:
        """Start visible refreshes and render an immediate sample."""

        self.timer.start()
        self.refresh()

    def stop(self) -> None:
        """Stop periodic refreshes while the overlay is hidden."""

        self.timer.stop()

    def shutdown(self) -> None:
        """Stop refreshes and close the optional background sampler."""

        self.stop()
        if self._status_runtime is not None:
            self._status_runtime.close()

    def refresh(self) -> None:
        """Request or read the current status and render known values."""

        if self._status_runtime is not None:
            self._status_runtime.request_refresh()
            snapshot = self._last_snapshot
        else:
            try:
                backend = self._status_backend
                snapshot = backend.snapshot() if backend is not None else OverlayStatusSnapshot()
            except Exception:
                _LOGGER.debug("Overlay header status refresh failed", exc_info=True)
                snapshot = OverlayStatusSnapshot()
        self._render(snapshot)

    def _set_snapshot(self, snapshot: object) -> None:
        if not isinstance(snapshot, OverlayStatusSnapshot):
            return
        self._last_snapshot = snapshot
        self._render(snapshot)

    def _render(self, snapshot: OverlayStatusSnapshot) -> None:
        self._apply_microphone_status(snapshot.microphone_muted)
        self._apply_power_status(snapshot)
        self._apply_controller_battery_status(self._read_controller_battery_status())
        self._apply_network_status(snapshot.network_connected)
        self.update_clock()

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
        self._set_status_label(self.microphone_label, text, tooltip, state)

    def _apply_power_status(self, snapshot: OverlayStatusSnapshot) -> None:
        if snapshot.battery_present is False:
            text = "⚡" if snapshot.power_plugged is not False else "▰"
            tooltip = (
                "AC power" if snapshot.power_plugged is not False else "Power status available"
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
                tooltip = f"Battery {percent}%" if percent is not None else "On battery power"
                state = "warning" if percent is not None and percent <= 20 else "active"
        elif snapshot.power_plugged is True:
            text = "⚡"
            tooltip = "Plugged in"
            state = "active"
        else:
            text = "?"
            tooltip = "Power status unavailable"
            state = "unknown"
        self._set_status_label(self.power_label, text, tooltip, state)

    def _read_controller_battery_status(self) -> ControllerBatterySnapshot:
        provider = self._controller_battery_status
        if provider is None:
            return ControllerBatterySnapshot()
        try:
            return provider()
        except Exception:
            _LOGGER.debug("Controller battery status refresh failed", exc_info=True)
            return ControllerBatterySnapshot(connected=True)

    def _apply_controller_battery_status(self, snapshot: ControllerBatterySnapshot) -> None:
        if not snapshot.connected:
            self.controller_battery_label.setVisible(False)
            return

        self.controller_battery_label.setVisible(True)
        percent = snapshot.battery_percent
        approximate_prefix = "~" if snapshot.approximate_percent and percent is not None else ""
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
        self._set_status_label(self.controller_battery_label, text, tooltip, state)

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
        self._set_status_label(self.network_label, text, tooltip, state)

    @staticmethod
    def _set_status_label(label: QLabel, text: str, tooltip: str, state: str) -> None:
        label.setText(text)
        label.setToolTip(tooltip)
        label.setAccessibleName(tooltip)
        label.setProperty("statusState", state)
        repolish_widget(label)

    def update_clock(self) -> None:
        """Render the current local time in the status cluster."""

        self.clock_label.setText(self._clock().strftime("%H:%M"))


__all__ = [
    "ControllerBatteryStatus",
    "OverlayStatusClusterController",
]
