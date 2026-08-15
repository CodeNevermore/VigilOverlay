"""Capability-driven generic-controller button polling through Windows Gaming Input."""

from __future__ import annotations

import hashlib
import logging
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from PySide6.QtCore import QObject

from vigil_overlay.core.worker_lifecycle import join_worker
from vigil_overlay.services.controller_shortcuts import (
    ControllerControlState,
    ControllerShortcutService,
)

if TYPE_CHECKING:
    from winrt.system import Array, Double
    from winrt.windows.gaming.input import GameControllerSwitchPosition

_LOGGER = logging.getLogger("vigil_overlay")


@dataclass(frozen=True, slots=True)
class RawControllerState:
    """One generic controller's exposed digital-button state."""

    device_id: str
    pressed_button_indexes: frozenset[int]


class RawControllerBackend(Protocol):
    def read_states(self) -> tuple[RawControllerState, ...]: ...

    def close(self) -> None: ...


class UnsupportedRawControllerBackend:
    def read_states(self) -> tuple[RawControllerState, ...]:
        return ()

    def close(self) -> None:
        return


class WindowsGamingInputBackend:
    """Read non-Gamepad generic controllers without parsing vendor HID reports."""

    def __init__(self) -> None:
        from winrt.system import Array
        from winrt.windows.gaming.input import (
            Gamepad,
            RawGameController,
        )

        self._array_type = Array
        self._gamepad_type = Gamepad
        self._raw_controller_type = RawGameController

    def read_states(self) -> tuple[RawControllerState, ...]:
        states: list[RawControllerState] = []
        for controller in tuple(self._raw_controller_type.raw_game_controllers):
            try:
                if self._gamepad_type.from_game_controller(controller) is not None:
                    # XInput owns standard gamepads. This route is only the generic
                    # capability fallback, preventing duplicate physical activations.
                    continue
                button_count = int(controller.button_count)
                switch_count = int(controller.switch_count)
                axis_count = int(controller.axis_count)
                buttons: Array[bool] = self._array_type("?", button_count)
                switches: Array[GameControllerSwitchPosition] = self._array_type("i", switch_count)
                axes: Array[Double] = self._array_type("d", axis_count)
                controller.get_current_reading(buttons, switches, axes)
                identity = str(controller.non_roamable_id)
                device_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
                states.append(
                    RawControllerState(
                        device_id=f"raw:{device_hash}",
                        pressed_button_indexes=frozenset(
                            index for index, pressed in enumerate(buttons) if pressed
                        ),
                    )
                )
            except Exception:
                _LOGGER.warning(
                    "Could not read one generic controller; other controllers remain active",
                    exc_info=True,
                )
        return tuple(states)

    def close(self) -> None:
        return


class RawControllerInputService(QObject):
    """Poll only generic buttons and feed the shared shortcut matcher."""

    def __init__(
        self,
        backend: RawControllerBackend,
        shortcut_service: ControllerShortcutService,
        *,
        poll_interval_seconds: float = 1.0 / 120.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._backend = backend
        self._shortcut_service = shortcut_service
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._known_devices: set[str] = set()
        self._closed = False

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        if self._closed or self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="VigilRawControllerInput",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._closed:
            return
        self._stop_event.set()
        thread = self._thread
        stopped = join_worker(
            thread,
            timeout_seconds=2.0,
            worker_name="Generic controller input worker",
            logger=_LOGGER,
        )
        if stopped:
            self._thread = None
        for device_id in tuple(self._known_devices):
            self._shortcut_service.observe_disconnect(device_id)
        self._known_devices.clear()
        try:
            self._backend.close()
        except Exception:
            _LOGGER.debug("Generic controller cleanup failed", exc_info=True)
        self._closed = True

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                states = self._backend.read_states()
                current_devices: set[str] = set()
                for state in states:
                    current_devices.add(state.device_id)
                    controls = frozenset(
                        f"{state.device_id}:button:{index}"
                        for index in state.pressed_button_indexes
                        if 0 <= index < 256
                    )
                    self._shortcut_service.observe_state(
                        ControllerControlState(state.device_id, controls)
                    )
                for device_id in self._known_devices - current_devices:
                    self._shortcut_service.observe_disconnect(device_id)
                self._known_devices = current_devices
                if self._stop_event.wait(self._poll_interval_seconds):
                    break
        except Exception:
            _LOGGER.exception(
                "Generic controller input failed; XInput and keyboard remain available"
            )


def create_platform_raw_controller_service(
    shortcut_service: ControllerShortcutService,
) -> RawControllerInputService:
    backend: RawControllerBackend = UnsupportedRawControllerBackend()
    if sys.platform == "win32":
        try:
            backend = WindowsGamingInputBackend()
        except (ImportError, OSError):
            _LOGGER.exception("Windows generic-controller input is unavailable")
    return RawControllerInputService(backend, shortcut_service)


__all__ = [
    "RawControllerBackend",
    "RawControllerInputService",
    "RawControllerState",
    "create_platform_raw_controller_service",
]
