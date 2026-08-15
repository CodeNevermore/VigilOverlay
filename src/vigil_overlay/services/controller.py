"""XInput controller polling, battery reporting, and navigation translation.

Normal navigation remains separate from Guide-button capture so each native
backend has one clear ownership boundary and failure mode.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol, cast

from PySide6.QtCore import QObject, Signal

from vigil_overlay.contracts.controller import ControllerBatterySnapshot
from vigil_overlay.core.worker_lifecycle import join_worker
from vigil_overlay.services.controller_shortcuts import (
    ControllerControlState,
    ControllerShortcutService,
)
from vigil_overlay.ui.navigation import NavigationCommand

_LOGGER = logging.getLogger("vigil_overlay")
_ERROR_SUCCESS: Final[int] = 0
_ERROR_DEVICE_NOT_CONNECTED: Final[int] = 1167
_MAX_CONTROLLERS: Final[int] = 4
_DEFAULT_POLL_INTERVAL_SECONDS: Final[float] = 1.0 / 120.0
_DEFAULT_RECONNECT_SCAN_SECONDS: Final[float] = 0.5
_DEFAULT_REPEAT_INITIAL_SECONDS: Final[float] = 0.35
_DEFAULT_REPEAT_INTERVAL_SECONDS: Final[float] = 0.10
_DEFAULT_STICK_ENGAGE: Final[int] = 16_000
_DEFAULT_STICK_RELEASE: Final[int] = 10_000
_DEFAULT_STICK_DOMINANCE_RATIO: Final[float] = 1.10
_XINPUT_DEVTYPE_GAMEPAD: Final[int] = 0x00
_XINPUT_BATTERY_TYPE_DISCONNECTED: Final[int] = 0x00
_XINPUT_BATTERY_TYPE_WIRED: Final[int] = 0x01
_XINPUT_BATTERY_TYPE_ALKALINE: Final[int] = 0x02
_XINPUT_BATTERY_TYPE_NIMH: Final[int] = 0x03
_XINPUT_BATTERY_TYPE_UNKNOWN: Final[int] = 0xFF
_XINPUT_BATTERY_LEVEL_EMPTY: Final[int] = 0x00
_XINPUT_BATTERY_LEVEL_LOW: Final[int] = 0x01
_XINPUT_BATTERY_LEVEL_MEDIUM: Final[int] = 0x02
_XINPUT_BATTERY_LEVEL_FULL: Final[int] = 0x03

XINPUT_GAMEPAD_DPAD_UP: Final[int] = 0x0001
XINPUT_GAMEPAD_DPAD_DOWN: Final[int] = 0x0002
XINPUT_GAMEPAD_DPAD_LEFT: Final[int] = 0x0004
XINPUT_GAMEPAD_DPAD_RIGHT: Final[int] = 0x0008
XINPUT_GAMEPAD_START: Final[int] = 0x0010
XINPUT_GAMEPAD_BACK: Final[int] = 0x0020
XINPUT_GAMEPAD_LEFT_THUMB: Final[int] = 0x0040
XINPUT_GAMEPAD_RIGHT_THUMB: Final[int] = 0x0080
XINPUT_GAMEPAD_LEFT_SHOULDER: Final[int] = 0x0100
XINPUT_GAMEPAD_RIGHT_SHOULDER: Final[int] = 0x0200
XINPUT_GAMEPAD_A: Final[int] = 0x1000
XINPUT_GAMEPAD_B: Final[int] = 0x2000
XINPUT_GAMEPAD_X: Final[int] = 0x4000
XINPUT_GAMEPAD_Y: Final[int] = 0x8000


@dataclass(frozen=True, slots=True)
class ControllerState:
    """Minimal XInput state used by the command interpreter."""

    packet_number: int
    buttons: int
    left_thumb_x: int
    left_thumb_y: int
    left_trigger: int = 0
    right_trigger: int = 0
    right_thumb_x: int = 0
    right_thumb_y: int = 0


class ControllerBackend(Protocol):
    """Polling contract kept independent from Qt and the overlay widgets."""

    def read_state(self, controller_index: int) -> ControllerState | None: ...

    def close(self) -> None: ...


class UnsupportedControllerBackend:
    """No-op backend used when XInput is unavailable."""

    def read_state(self, controller_index: int) -> ControllerState | None:
        del controller_index
        return None

    def close(self) -> None:
        return


class _XInputGamepad(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class _XInputState(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_ulong),
        ("Gamepad", _XInputGamepad),
    ]


class _XInputBatteryInformation(ctypes.Structure):
    _fields_ = [
        ("BatteryType", ctypes.c_ubyte),
        ("BatteryLevel", ctypes.c_ubyte),
    ]


class XInputControllerBackend:
    """Read standard controller state through the Windows XInput API."""

    def __init__(self) -> None:
        self._library_name, self._library = _load_xinput_library()
        get_state = cast(Any, self._library.XInputGetState)
        get_state.argtypes = [ctypes.c_uint, ctypes.POINTER(_XInputState)]
        get_state.restype = ctypes.c_ulong
        self._get_state = get_state
        get_battery = getattr(self._library, "XInputGetBatteryInformation", None)
        if get_battery is not None:
            get_battery = cast(Any, get_battery)
            get_battery.argtypes = [
                ctypes.c_uint,
                ctypes.c_ubyte,
                ctypes.POINTER(_XInputBatteryInformation),
            ]
            get_battery.restype = ctypes.c_ulong
        self._get_battery = get_battery
        _LOGGER.info("Controller input using %s", self._library_name)

    def read_state(self, controller_index: int) -> ControllerState | None:
        if not 0 <= controller_index < _MAX_CONTROLLERS:
            raise ValueError(f"controller_index must be between 0 and {_MAX_CONTROLLERS - 1}")
        state = _XInputState()
        result = int(self._get_state(controller_index, ctypes.byref(state)))
        if result == _ERROR_DEVICE_NOT_CONNECTED:
            return None
        if result != _ERROR_SUCCESS:
            _LOGGER.debug(
                "XInputGetState failed for controller %d with status %d",
                controller_index,
                result,
            )
            return None
        gamepad = state.Gamepad
        return ControllerState(
            packet_number=int(state.dwPacketNumber),
            buttons=int(gamepad.wButtons),
            left_thumb_x=int(gamepad.sThumbLX),
            left_thumb_y=int(gamepad.sThumbLY),
            left_trigger=int(gamepad.bLeftTrigger),
            right_trigger=int(gamepad.bRightTrigger),
            right_thumb_x=int(gamepad.sThumbRX),
            right_thumb_y=int(gamepad.sThumbRY),
        )

    def read_battery(self, controller_index: int) -> ControllerBatterySnapshot:
        """Read XInput battery state for either Bluetooth or Xbox Wireless transport.

        XInput exposes coarse charge levels rather than a true continuous percentage.
        The displayed percentages use Microsoft's common controller battery buckets
        (100/70/40/10) and are therefore explicitly marked approximate.
        """

        if not 0 <= controller_index < _MAX_CONTROLLERS:
            raise ValueError(f"controller_index must be between 0 and {_MAX_CONTROLLERS - 1}")
        if self._get_battery is None:
            return ControllerBatterySnapshot(connected=True)

        info = _XInputBatteryInformation()
        result = int(
            self._get_battery(
                controller_index,
                _XINPUT_DEVTYPE_GAMEPAD,
                ctypes.byref(info),
            )
        )
        if result == _ERROR_DEVICE_NOT_CONNECTED:
            return ControllerBatterySnapshot()
        if result != _ERROR_SUCCESS:
            _LOGGER.debug(
                "XInputGetBatteryInformation failed for controller %d with status %d",
                controller_index,
                result,
            )
            return ControllerBatterySnapshot(connected=True)

        battery_type = int(info.BatteryType)
        battery_level = int(info.BatteryLevel)
        if battery_type == _XINPUT_BATTERY_TYPE_DISCONNECTED:
            return ControllerBatterySnapshot()
        if battery_type == _XINPUT_BATTERY_TYPE_WIRED:
            return ControllerBatterySnapshot(
                connected=True,
                battery_present=False,
                level_label="wired",
            )
        if battery_type == _XINPUT_BATTERY_TYPE_UNKNOWN:
            return ControllerBatterySnapshot(
                connected=True,
                battery_present=None,
                level_label="unknown",
            )

        if battery_type not in {
            _XINPUT_BATTERY_TYPE_ALKALINE,
            _XINPUT_BATTERY_TYPE_NIMH,
        }:
            return ControllerBatterySnapshot(connected=True)

        level_details = {
            _XINPUT_BATTERY_LEVEL_EMPTY: (10, "critical"),
            _XINPUT_BATTERY_LEVEL_LOW: (40, "low"),
            _XINPUT_BATTERY_LEVEL_MEDIUM: (70, "medium"),
            _XINPUT_BATTERY_LEVEL_FULL: (100, "full"),
        }.get(battery_level)
        if level_details is None:
            return ControllerBatterySnapshot(connected=True, battery_present=True)
        percent, label = level_details
        return ControllerBatterySnapshot(
            connected=True,
            battery_present=True,
            battery_percent=percent,
            approximate_percent=True,
            level_label=label,
        )

    def close(self) -> None:
        return


class _Direction(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    UP = "up"
    DOWN = "down"

    @property
    def command(self) -> NavigationCommand:
        return {
            _Direction.LEFT: NavigationCommand.MOVE_LEFT,
            _Direction.RIGHT: NavigationCommand.MOVE_RIGHT,
            _Direction.UP: NavigationCommand.MOVE_UP,
            _Direction.DOWN: NavigationCommand.MOVE_DOWN,
        }[self]


class ControllerCommandInterpreter:
    """Translate controller state into the shared navigation command vocabulary."""

    def __init__(
        self,
        *,
        repeat_initial_seconds: float = _DEFAULT_REPEAT_INITIAL_SECONDS,
        repeat_interval_seconds: float = _DEFAULT_REPEAT_INTERVAL_SECONDS,
        stick_engage: int = _DEFAULT_STICK_ENGAGE,
        stick_release: int = _DEFAULT_STICK_RELEASE,
        stick_dominance_ratio: float = _DEFAULT_STICK_DOMINANCE_RATIO,
    ) -> None:
        if repeat_initial_seconds <= 0:
            raise ValueError("repeat_initial_seconds must be positive")
        if repeat_interval_seconds <= 0:
            raise ValueError("repeat_interval_seconds must be positive")
        if not 0 < stick_release < stick_engage <= 32_767:
            raise ValueError("stick thresholds must satisfy 0 < release < engage <= 32767")
        if stick_dominance_ratio < 1.0:
            raise ValueError("stick_dominance_ratio must be at least 1.0")
        self._repeat_initial_seconds = repeat_initial_seconds
        self._repeat_interval_seconds = repeat_interval_seconds
        self._stick_engage = stick_engage
        self._stick_release = stick_release
        self._stick_dominance_ratio = stick_dominance_ratio
        self._previous_buttons = 0
        self._held_direction: _Direction | None = None
        self._next_repeat_at: float | None = None
        self._suppressed_repeat_direction: _Direction | None = None
        self._interpreter_lock = threading.Lock()

    def reset(self) -> None:
        """Forget held input after disconnect or service restart."""

        with self._interpreter_lock:
            self._previous_buttons = 0
            self._held_direction = None
            self._next_repeat_at = None
            self._suppressed_repeat_direction = None

    def prime(self, state: ControllerState, *, now: float) -> None:
        """Adopt current held state without generating a reconnect-time command burst."""

        with self._interpreter_lock:
            self._previous_buttons = state.buttons
            self._held_direction = self._direction_for_state(state, allow_hysteresis=False)
            self._next_repeat_at = (
                now + self._repeat_initial_seconds if self._held_direction is not None else None
            )
            self._suppressed_repeat_direction = None

    @property
    def held_direction_command(self) -> NavigationCommand | None:
        with self._interpreter_lock:
            direction = self._held_direction
            return direction.command if direction is not None else None

    def suppress_repeat_until_release(self, command: NavigationCommand) -> None:
        """Suppress repeats for the currently held direction until it is released.

        The initial command has already been delivered. This is used when that command
        changes a top-level widget, preventing a slow widget activation from allowing
        the same physical hold to queue a second widget change.
        """

        direction = {
            NavigationCommand.MOVE_LEFT: _Direction.LEFT,
            NavigationCommand.MOVE_RIGHT: _Direction.RIGHT,
            NavigationCommand.MOVE_UP: _Direction.UP,
            NavigationCommand.MOVE_DOWN: _Direction.DOWN,
        }.get(command)
        if direction is None:
            return
        with self._interpreter_lock:
            if self._held_direction is direction:
                self._suppressed_repeat_direction = direction

    def is_neutral(self, state: ControllerState) -> bool:
        """Return whether all navigation controls have returned to rest."""

        return (
            state.buttons == 0
            and abs(state.left_thumb_x) < self._stick_release
            and abs(state.left_thumb_y) < self._stick_release
        )

    def update(self, state: ControllerState, *, now: float) -> tuple[NavigationCommand, ...]:
        with self._interpreter_lock:
            commands: list[NavigationCommand] = []
            pressed = state.buttons & ~self._previous_buttons

            if pressed & XINPUT_GAMEPAD_A:
                commands.append(NavigationCommand.ACTIVATE)
            if pressed & XINPUT_GAMEPAD_B:
                commands.append(NavigationCommand.BACK)
            if pressed & XINPUT_GAMEPAD_START:
                commands.append(NavigationCommand.OPEN_OPTIONS)
            if pressed & XINPUT_GAMEPAD_LEFT_SHOULDER:
                commands.append(NavigationCommand.PREVIOUS_WIDGET)
            if pressed & XINPUT_GAMEPAD_RIGHT_SHOULDER:
                commands.append(NavigationCommand.NEXT_WIDGET)

            direction = self._direction_for_state(state, allow_hysteresis=True)
            if direction != self._held_direction:
                self._held_direction = direction
                self._suppressed_repeat_direction = None
                if direction is None:
                    self._next_repeat_at = None
                else:
                    commands.append(direction.command)
                    self._next_repeat_at = now + self._repeat_initial_seconds
            elif direction is not None and self._next_repeat_at is not None:
                if now >= self._next_repeat_at:
                    if direction is not self._suppressed_repeat_direction:
                        commands.append(direction.command)
                    while self._next_repeat_at <= now:
                        self._next_repeat_at += self._repeat_interval_seconds

            self._previous_buttons = state.buttons
            return tuple(commands)

    def _direction_for_state(
        self,
        state: ControllerState,
        *,
        allow_hysteresis: bool,
    ) -> _Direction | None:
        dpad = self._dpad_direction(state.buttons)
        if dpad is not None:
            return dpad

        if allow_hysteresis and self._held_direction is not None:
            held_value = self._axis_value_for_direction(state, self._held_direction)
            if held_value >= self._stick_release:
                return self._held_direction

        x = state.left_thumb_x
        y = state.left_thumb_y
        abs_x = abs(x)
        abs_y = abs(y)
        strongest = max(abs_x, abs_y)
        if strongest < self._stick_engage:
            return None

        weakest = min(abs_x, abs_y)
        if weakest > 0 and strongest < weakest * self._stick_dominance_ratio:
            return None

        if abs_x > abs_y:
            return _Direction.RIGHT if x > 0 else _Direction.LEFT
        return _Direction.UP if y > 0 else _Direction.DOWN

    @staticmethod
    def _dpad_direction(buttons: int) -> _Direction | None:
        up = bool(buttons & XINPUT_GAMEPAD_DPAD_UP)
        down = bool(buttons & XINPUT_GAMEPAD_DPAD_DOWN)
        left = bool(buttons & XINPUT_GAMEPAD_DPAD_LEFT)
        right = bool(buttons & XINPUT_GAMEPAD_DPAD_RIGHT)

        if up != down:
            return _Direction.UP if up else _Direction.DOWN
        if left != right:
            return _Direction.LEFT if left else _Direction.RIGHT
        return None

    @staticmethod
    def _axis_value_for_direction(state: ControllerState, direction: _Direction) -> int:
        if direction is _Direction.LEFT:
            return max(-state.left_thumb_x, 0)
        if direction is _Direction.RIGHT:
            return max(state.left_thumb_x, 0)
        if direction is _Direction.UP:
            return max(state.left_thumb_y, 0)
        return max(-state.left_thumb_y, 0)


class ControllerInputService(QObject):
    """Poll controllers on a daemon thread and emit controller-neutral commands."""

    command_ready = Signal(object)
    connection_changed = Signal(bool, int)
    activation_released = Signal()
    direction_released = Signal(object)
    commands_rearmed = Signal()

    def __init__(
        self,
        backend: ControllerBackend,
        *,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        reconnect_scan_seconds: float = _DEFAULT_RECONNECT_SCAN_SECONDS,
        interpreter: ControllerCommandInterpreter | None = None,
        shortcut_service: ControllerShortcutService | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if reconnect_scan_seconds <= 0:
            raise ValueError("reconnect_scan_seconds must be positive")
        self._backend = backend
        self._poll_interval_seconds = poll_interval_seconds
        self._reconnect_scan_seconds = reconnect_scan_seconds
        self._interpreter = interpreter or ControllerCommandInterpreter()
        self._shortcut_service = shortcut_service
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._active_controller_index: int | None = None
        self._commands_armed = threading.Event()
        self._commands_armed.set()
        self._closed = False
        self._shortcut_devices: set[str] = set()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def active_controller_index(self) -> int | None:
        with self._state_lock:
            return self._active_controller_index

    def battery_snapshot(self) -> ControllerBatterySnapshot:
        """Return battery state for the active XInput controller without changing input flow."""

        controller_index = self.active_controller_index
        if controller_index is None:
            return ControllerBatterySnapshot()
        reader = getattr(self._backend, "read_battery", None)
        if not callable(reader):
            return ControllerBatterySnapshot(connected=True)
        try:
            snapshot = reader(controller_index)
        except Exception:
            _LOGGER.debug(
                "Controller battery read failed for index %d",
                controller_index,
                exc_info=True,
            )
            return ControllerBatterySnapshot(connected=True)
        if not isinstance(snapshot, ControllerBatterySnapshot):
            return ControllerBatterySnapshot(connected=True)
        return snapshot

    def start(self) -> None:
        if self._closed or self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="VigilControllerInput",
            daemon=True,
        )
        self._thread.start()

    @property
    def commands_armed(self) -> bool:
        return self._commands_armed.is_set()

    def require_neutral_before_commands(self) -> None:
        """Disarm command delivery until the active controller returns to neutral."""

        self._interpreter.reset()
        self._commands_armed.clear()

    def set_shortcut_service(self, shortcut_service: ControllerShortcutService | None) -> None:
        """Attach the shared shortcut matcher before polling starts."""

        if self.running:
            raise RuntimeError("controller shortcut service cannot change while running")
        self._shortcut_service = shortcut_service

    def suppress_direction_repeat_until_release(self, command: NavigationCommand) -> None:
        """Prevent a delivered directional command from auto-repeating until release."""

        self._interpreter.suppress_repeat_until_release(command)

    def stop(self) -> None:
        if self._closed:
            return
        self._stop_event.set()
        thread = self._thread
        stopped = join_worker(
            thread,
            timeout_seconds=max(2.0, self._reconnect_scan_seconds * 2.0),
            worker_name="Controller input worker",
            logger=_LOGGER,
        )
        if stopped:
            self._thread = None
        self._set_active_controller(None)
        shortcut_service = self._shortcut_service
        if shortcut_service is not None:
            for device_id in tuple(self._shortcut_devices):
                shortcut_service.observe_disconnect(device_id)
        self._shortcut_devices.clear()
        self._interpreter.reset()
        self._commands_armed.set()
        try:
            self._backend.close()
        except Exception:
            _LOGGER.exception("Controller backend cleanup failed")
        self._closed = True

    def _run(self) -> None:
        active_index: int | None = None
        next_scan_at = 0.0
        previous_buttons = 0
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                shortcut_states, shortcut_consumed_indexes = self._poll_shortcut_states()
                if active_index is None:
                    if now >= next_scan_at:
                        active_index, state = self._find_connected_controller()
                        next_scan_at = now + self._reconnect_scan_seconds
                        if active_index is not None and state is not None:
                            self._interpreter.prime(state, now=now)
                            previous_buttons = state.buttons
                            self._set_active_controller(active_index)
                            _LOGGER.info("Controller %d connected", active_index)
                    if self._stop_event.wait(self._poll_interval_seconds):
                        break
                    continue

                state = shortcut_states.get(active_index)
                if self._shortcut_service is None:
                    state = self._safe_read_state(active_index)
                if state is None:
                    disconnected_index = active_index
                    active_index = None
                    next_scan_at = now
                    self._interpreter.reset()
                    previous_buttons = 0
                    self._set_active_controller(None)
                    _LOGGER.info("Controller %d disconnected", disconnected_index)
                    if self._stop_event.wait(self._poll_interval_seconds):
                        break
                    continue

                shortcut_consumed = active_index in shortcut_consumed_indexes

                if previous_buttons & XINPUT_GAMEPAD_A and not state.buttons & XINPUT_GAMEPAD_A:
                    self.activation_released.emit()
                previous_buttons = state.buttons

                if not self._commands_armed.is_set():
                    self._interpreter.prime(state, now=now)
                    if self._interpreter.is_neutral(state):
                        self._commands_armed.set()
                        self.commands_rearmed.emit()
                    if self._stop_event.wait(self._poll_interval_seconds):
                        break
                    continue

                if shortcut_consumed:
                    self._interpreter.prime(state, now=now)
                    if self._stop_event.wait(self._poll_interval_seconds):
                        break
                    continue

                previous_direction = self._interpreter.held_direction_command
                commands = self._interpreter.update(state, now=now)
                current_direction = self._interpreter.held_direction_command
                if previous_direction is not None and current_direction != previous_direction:
                    self.direction_released.emit(previous_direction)
                for command in commands:
                    self.command_ready.emit(command)
                if self._stop_event.wait(self._poll_interval_seconds):
                    break
        except Exception:
            _LOGGER.exception(
                "Controller input worker failed; keyboard navigation remains available"
            )
            self._set_active_controller(None)

    def _poll_shortcut_states(self) -> tuple[dict[int, ControllerState], set[int]]:
        shortcut_service = self._shortcut_service
        if shortcut_service is None:
            return {}, set()
        states: dict[int, ControllerState] = {}
        consumed_indexes: set[int] = set()
        connected: set[str] = set()
        for controller_index in range(_MAX_CONTROLLERS):
            state = self._safe_read_state(controller_index)
            if state is None:
                continue
            states[controller_index] = state
            device_id = f"xinput:{controller_index}"
            connected.add(device_id)
            if shortcut_service.observe_state(
                ControllerControlState(
                    device_id=device_id,
                    controls=_xinput_controls(state),
                )
            ):
                consumed_indexes.add(controller_index)
        for device_id in self._shortcut_devices - connected:
            shortcut_service.observe_disconnect(device_id)
        self._shortcut_devices = connected
        return states, consumed_indexes

    def _find_connected_controller(self) -> tuple[int | None, ControllerState | None]:
        for controller_index in range(_MAX_CONTROLLERS):
            state = self._safe_read_state(controller_index)
            if state is not None:
                return controller_index, state
        return None, None

    def _safe_read_state(self, controller_index: int) -> ControllerState | None:
        try:
            return self._backend.read_state(controller_index)
        except Exception:
            _LOGGER.exception("Controller backend read failed for index %d", controller_index)
            return None

    def _set_active_controller(self, controller_index: int | None) -> None:
        with self._state_lock:
            previous = self._active_controller_index
            if previous == controller_index:
                return
            self._active_controller_index = controller_index
        if previous is not None:
            self.connection_changed.emit(False, previous)
        if controller_index is not None:
            self.connection_changed.emit(True, controller_index)


def create_platform_controller_service() -> ControllerInputService:
    """Create the Windows XInput controller service or a safe no-op fallback."""

    if sys.platform == "win32":
        try:
            return ControllerInputService(XInputControllerBackend())
        except OSError:
            _LOGGER.exception("XInput controller initialization failed")
    return ControllerInputService(UnsupportedControllerBackend())


def _xinput_controls(state: ControllerState) -> frozenset[str]:
    controls: set[str] = set()
    button_controls = {
        XINPUT_GAMEPAD_DPAD_UP: "xinput:dpad_up",
        XINPUT_GAMEPAD_DPAD_DOWN: "xinput:dpad_down",
        XINPUT_GAMEPAD_DPAD_LEFT: "xinput:dpad_left",
        XINPUT_GAMEPAD_DPAD_RIGHT: "xinput:dpad_right",
        XINPUT_GAMEPAD_START: "xinput:start",
        XINPUT_GAMEPAD_BACK: "xinput:back",
        XINPUT_GAMEPAD_LEFT_THUMB: "xinput:left_thumb",
        XINPUT_GAMEPAD_RIGHT_THUMB: "xinput:right_thumb",
        XINPUT_GAMEPAD_LEFT_SHOULDER: "xinput:left_shoulder",
        XINPUT_GAMEPAD_RIGHT_SHOULDER: "xinput:right_shoulder",
        XINPUT_GAMEPAD_A: "xinput:a",
        XINPUT_GAMEPAD_B: "xinput:b",
        XINPUT_GAMEPAD_X: "xinput:x",
        XINPUT_GAMEPAD_Y: "xinput:y",
    }
    controls.update(control for mask, control in button_controls.items() if state.buttons & mask)
    if state.left_trigger >= 128:
        controls.add("xinput:left_trigger")
    if state.right_trigger >= 128:
        controls.add("xinput:right_trigger")
    for axis, negative, positive in (
        (state.left_thumb_x, "xinput:left_stick_left", "xinput:left_stick_right"),
        (state.left_thumb_y, "xinput:left_stick_down", "xinput:left_stick_up"),
        (state.right_thumb_x, "xinput:right_stick_left", "xinput:right_stick_right"),
        (state.right_thumb_y, "xinput:right_stick_down", "xinput:right_stick_up"),
    ):
        if axis <= -_DEFAULT_STICK_ENGAGE:
            controls.add(negative)
        elif axis >= _DEFAULT_STICK_ENGAGE:
            controls.add(positive)
    return frozenset(controls)


def _load_xinput_library() -> tuple[str, Any]:
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise OSError("Win32 XInput libraries are unavailable on this platform")
    errors: list[str] = []
    for library_name in ("xinput1_4.dll", "xinput9_1_0.dll", "xinput1_3.dll"):
        try:
            return library_name, win_dll(library_name)
        except OSError as exc:
            errors.append(f"{library_name}: {exc}")
    raise OSError("Could not load an XInput library: " + "; ".join(errors))
