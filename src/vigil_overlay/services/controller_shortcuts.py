"""Thread-safe controller shortcut matching and neutral-gated physical capture."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from vigil_overlay.core.controller_shortcuts import ControllerShortcutBinding


@dataclass(frozen=True, slots=True)
class ControllerControlState:
    """Controls currently pressed on one physical controller route."""

    device_id: str
    controls: frozenset[str]

    def __post_init__(self) -> None:
        if not self.device_id:
            raise ValueError("controller state device_id must be non-empty")


class ControllerShortcutService(QObject):
    """Match one saved chord before navigation and capture any exposed controls."""

    activated = Signal()
    capture_ready = Signal(object)
    capture_armed_changed = Signal(bool)

    def __init__(
        self,
        binding: ControllerShortcutBinding | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._binding = binding or ControllerShortcutBinding()
        self._states: dict[str, frozenset[str]] = {}
        self._latched_devices: set[str] = set()
        self._capture_active = False
        self._capture_armed = False
        self._capture_device_id: str | None = None
        self._capture_controls: set[str] = set()

    @property
    def binding(self) -> ControllerShortcutBinding:
        with self._lock:
            return self._binding

    @property
    def capture_active(self) -> bool:
        with self._lock:
            return self._capture_active

    def set_binding(self, binding: ControllerShortcutBinding) -> None:
        with self._lock:
            self._binding = binding
            self._latched_devices.clear()

    def begin_capture(self) -> None:
        with self._lock:
            self._capture_active = True
            self._capture_armed = not any(self._states.values())
            self._capture_device_id = None
            self._capture_controls.clear()
            armed = self._capture_armed
        self.capture_armed_changed.emit(armed)

    def cancel_capture(self) -> None:
        with self._lock:
            self._capture_active = False
            self._capture_armed = False
            self._capture_device_id = None
            self._capture_controls.clear()

    def observe_state(self, state: ControllerControlState) -> bool:
        """Return whether this state must be consumed before navigation."""

        emit_activation = False
        captured: ControllerShortcutBinding | None = None
        armed_changed: bool | None = None
        with self._lock:
            self._states[state.device_id] = state.controls
            if self._capture_active:
                if not self._capture_armed:
                    if not any(self._states.values()):
                        self._capture_armed = True
                        armed_changed = True
                    consume = True
                else:
                    consume = True
                    if self._capture_device_id is None and state.controls:
                        self._capture_device_id = state.device_id
                    if self._capture_device_id == state.device_id:
                        self._capture_controls.update(state.controls)
                        if self._capture_controls and not state.controls:
                            captured = ControllerShortcutBinding(
                                tuple(sorted(self._capture_controls))
                            )
                            self._capture_active = False
                            self._capture_armed = False
                            self._capture_device_id = None
                            self._capture_controls.clear()
            else:
                binding_controls = frozenset(self._binding.controls)
                pressed = bool(binding_controls) and binding_controls.issubset(
                    state.controls
                )
                if pressed and state.device_id not in self._latched_devices:
                    self._latched_devices.add(state.device_id)
                    emit_activation = True
                elif (
                    state.device_id in self._latched_devices
                    and not binding_controls.intersection(state.controls)
                ):
                    self._latched_devices.remove(state.device_id)
                consume = pressed or state.device_id in self._latched_devices
        if armed_changed is not None:
            self.capture_armed_changed.emit(armed_changed)
        if captured is not None:
            self.capture_ready.emit(captured)
        if emit_activation:
            self.activated.emit()
        return consume

    def observe_disconnect(self, device_id: str) -> None:
        with self._lock:
            self._states.pop(device_id, None)
            self._latched_devices.discard(device_id)
            if self._capture_device_id == device_id:
                self._capture_device_id = None
                self._capture_controls.clear()
            if (
                self._capture_active
                and not self._capture_armed
                and not any(self._states.values())
            ):
                self._capture_armed = True
                emit_armed = True
            else:
                emit_armed = False
        if emit_armed:
            self.capture_armed_changed.emit(True)

    def observe_guide_pulse(self) -> bool:
        """Consume a Guide pulse for capture or a saved Guide-only binding."""

        captured: ControllerShortcutBinding | None = None
        emit_activation = False
        with self._lock:
            if self._capture_active:
                if not self._capture_armed:
                    return True
                captured = ControllerShortcutBinding(("gameinput:guide",))
                self._capture_active = False
                self._capture_armed = False
                self._capture_device_id = None
                self._capture_controls.clear()
            elif self._binding.controls == ("gameinput:guide",):
                emit_activation = True
            else:
                return False
        if captured is not None:
            self.capture_ready.emit(captured)
        if emit_activation:
            self.activated.emit()
        return True


__all__ = ["ControllerControlState", "ControllerShortcutService"]
