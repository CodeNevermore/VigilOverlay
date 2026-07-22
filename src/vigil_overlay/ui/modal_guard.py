"""Reusable activation guard for controller-safe modal prompts."""

from __future__ import annotations

from enum import StrEnum

from PySide6.QtCore import QObject, QTimer, Signal


class ModalInputSource(StrEnum):
    """Input family that opened or activated a host-owned modal."""

    UNKNOWN = "unknown"
    CONTROLLER = "controller"
    KEYBOARD = "keyboard"
    POINTER = "pointer"


class ModalActivationGuard(QObject):
    """Disarm modal activation until the initiating input can no longer leak through."""

    armed_changed = Signal(bool)

    def __init__(
        self, parent: QObject | None = None, *, quiet_milliseconds: int = 450
    ) -> None:
        super().__init__(parent)
        if quiet_milliseconds < 0:
            raise ValueError("quiet_milliseconds must be non-negative")
        self._quiet_elapsed = False
        self._release_seen = True
        self._active = False
        self._source = ModalInputSource.UNKNOWN
        self._armed = False
        self._quiet_timer = QTimer(self)
        self._quiet_timer.setSingleShot(True)
        self._quiet_timer.setInterval(quiet_milliseconds)
        self._quiet_timer.timeout.connect(self._on_quiet_elapsed)

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def source(self) -> ModalInputSource:
        return self._source

    def begin(self, source: ModalInputSource) -> None:
        self._source = source
        self._active = True
        self._quiet_elapsed = False
        self._release_seen = source is not ModalInputSource.CONTROLLER
        self._set_armed(False)
        self._quiet_timer.start()

    def end(self) -> None:
        self._quiet_timer.stop()
        self._active = False
        self._source = ModalInputSource.UNKNOWN
        self._quiet_elapsed = False
        self._release_seen = True
        self._set_armed(False)

    def note_controller_activation_released(self) -> None:
        if not self._active or self._source is not ModalInputSource.CONTROLLER:
            return
        self._release_seen = True
        self._sync_armed()

    def accepts_activation(self) -> bool:
        return self._active and self._armed

    def _on_quiet_elapsed(self) -> None:
        self._quiet_elapsed = True
        self._sync_armed()

    def _sync_armed(self) -> None:
        self._set_armed(self._active and self._quiet_elapsed and self._release_seen)

    def _set_armed(self, armed: bool) -> None:
        if self._armed == armed:
            return
        self._armed = armed
        self.armed_changed.emit(armed)
