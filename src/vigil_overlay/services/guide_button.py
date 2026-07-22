"""Xbox/Guide system-button delivery through Microsoft GameInput.

Guide capture is isolated from normal XInput navigation. The native backend
opens the v0 GameInput factory, queries the v2 ABI before using v2-only system
button and background-focus methods, and leaves the global hotkey available as
the recovery path when that interface is unavailable.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable
from typing import Any, Final, Protocol, cast

from PySide6.QtCore import QObject, Signal

from vigil_overlay.ui.navigation import NavigationCommand

_LOGGER = logging.getLogger("vigil_overlay")

_GAMEINPUT_SYSTEM_BUTTON_GUIDE: Final[int] = 0x00000001
_GAMEINPUT_EXCLUSIVE_FOREGROUND_INPUT: Final[int] = 0x00000002
_GAMEINPUT_ENABLE_BACKGROUND_GUIDE_BUTTON: Final[int] = 0x00000080

# IUnknown occupies vtable slots 0-2. These are the v2 IGameInput method slots
# from Microsoft's versioned GameInput v2 interface declaration.
_VTABLE_QUERY_INTERFACE: Final[int] = 0
_VTABLE_RELEASE: Final[int] = 2
_VTABLE_REGISTER_SYSTEM_BUTTON_CALLBACK: Final[int] = 9
_VTABLE_STOP_CALLBACK: Final[int] = 11
_VTABLE_UNREGISTER_CALLBACK: Final[int] = 12
_VTABLE_SET_FOCUS_POLICY: Final[int] = 16


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]


_IID_IGAMEINPUT_V2 = _Guid(
    0xBBAA66D2,
    0x837A,
    0x40F7,
    (ctypes.c_ubyte * 8)(0xA3, 0x03, 0x91, 0x7D, 0x50, 0x09, 0x55, 0xF4),
)

_WINFUNCTYPE = cast(Any, getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE))
_HRESULT = ctypes.c_int32
_E_NOINTERFACE: Final[int] = 0x80004002
_GameInputSystemButtonCallback = _WINFUNCTYPE(
    None,
    ctypes.c_uint64,  # GameInputCallbackToken
    ctypes.c_void_p,  # context
    ctypes.c_void_p,  # IGameInputDevice*
    ctypes.c_uint64,  # timestamp
    ctypes.c_uint32,  # currentButtons
    ctypes.c_uint32,  # previousButtons
)


class GuideButtonBackend(Protocol):
    """Native Guide-button backend independent from Qt/application behavior."""

    @property
    def detail(self) -> str: ...

    def start(self, on_guide_pressed: Callable[[], None]) -> bool: ...

    def set_controller_ownership_active(self, active: bool) -> None: ...

    def stop(self) -> None: ...


class UnsupportedGuideButtonBackend:
    """Safe backend used where GameInput v2 Guide delivery is unavailable."""

    def __init__(
        self, detail: str = "GameInput Guide-button delivery is unavailable"
    ) -> None:
        self._detail = detail

    @property
    def detail(self) -> str:
        return self._detail

    def start(self, on_guide_pressed: Callable[[], None]) -> bool:
        del on_guide_pressed
        return False

    def set_controller_ownership_active(self, active: bool) -> None:
        del active

    def stop(self) -> None:
        return


class GameInputGuideButtonBackend:
    """Register the Xbox/Guide system-button callback through GameInput v2."""

    def __init__(self) -> None:
        self._library: Any | None = None
        self._game_input = ctypes.c_void_p()
        self._callback_token: int | None = None
        self._native_callback: Any | None = None
        self._controller_ownership_active = False
        self._detail = "GameInput v2 Guide callback not started"

    @property
    def detail(self) -> str:
        return self._detail

    def start(self, on_guide_pressed: Callable[[], None]) -> bool:
        if self._game_input.value and self._callback_token is not None:
            return True

        library = self._load_library()
        base_interface = self._create_base_interface(library)
        try:
            v2_interface = self._query_v2_interface(base_interface)
        finally:
            self._release_interface(base_interface)

        if v2_interface is None:
            self._detail = (
                "Installed GameInput runtime does not expose the v2 interface required for "
                "background Guide-button delivery (E_NOINTERFACE 0x80004002); "
                "Ctrl+Alt+Shift+G remains available"
            )
            _LOGGER.warning(self._detail)
            return False

        try:
            self._set_effective_focus_policy(
                v2_interface,
                background_guide_enabled=True,
                controller_ownership_active=self._controller_ownership_active,
            )
            native_callback = _GameInputSystemButtonCallback(
                self._make_system_button_callback(on_guide_pressed)
            )
            callback_token = ctypes.c_uint64(0)
            register_callback = self._method(
                v2_interface,
                _VTABLE_REGISTER_SYSTEM_BUTTON_CALLBACK,
                _HRESULT,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_void_p,
                _GameInputSystemButtonCallback,
                ctypes.POINTER(ctypes.c_uint64),
            )
            result = int(
                register_callback(
                    v2_interface,
                    None,
                    _GAMEINPUT_SYSTEM_BUTTON_GUIDE,
                    None,
                    native_callback,
                    ctypes.byref(callback_token),
                )
            )
            if _hresult_failed(result):
                raise OSError(
                    f"IGameInput::RegisterSystemButtonCallback failed with HRESULT "
                    f"0x{result & 0xFFFFFFFF:08X}"
                )
        except Exception:
            self._release_interface(v2_interface)
            raise

        self._library = library
        self._game_input = v2_interface
        self._callback_token = int(callback_token.value)
        self._native_callback = native_callback
        self._detail = "GameInput v2 Guide callback active with background Guide policy"
        _LOGGER.info(self._detail)
        return True

    def set_controller_ownership_active(self, active: bool) -> None:
        """Keep the process-wide Guide policy coherent with ordinary ownership."""

        self._controller_ownership_active = bool(active)
        interface = self._game_input
        if not interface.value:
            return
        self._set_effective_focus_policy(
            interface,
            background_guide_enabled=True,
            controller_ownership_active=self._controller_ownership_active,
        )

    def stop(self) -> None:
        interface = self._game_input
        token = self._callback_token
        if interface.value and token is not None:
            try:
                stop_callback = self._method(
                    interface,
                    _VTABLE_STOP_CALLBACK,
                    None,
                    ctypes.c_uint64,
                )
                stop_callback(interface, token)
                unregister_callback = self._method(
                    interface,
                    _VTABLE_UNREGISTER_CALLBACK,
                    ctypes.c_bool,
                    ctypes.c_uint64,
                )
                unregister_callback(interface, token)
            except Exception:
                _LOGGER.exception("GameInput Guide callback shutdown failed")

        if interface.value:
            self._release_interface(interface)

        self._game_input = ctypes.c_void_p()
        self._callback_token = None
        self._native_callback = None
        self._library = None
        self._detail = "GameInput v2 Guide callback stopped"

    @staticmethod
    def _make_system_button_callback(
        on_guide_pressed: Callable[[], None],
    ) -> Callable[[int, int | None, int | None, int, int, int], None]:
        def callback(
            callback_token: int,
            context: int | None,
            device: int | None,
            timestamp: int,
            current_buttons: int,
            previous_buttons: int,
        ) -> None:
            del callback_token, context, device, timestamp
            pressed_now = bool(current_buttons & _GAMEINPUT_SYSTEM_BUTTON_GUIDE)
            pressed_before = bool(previous_buttons & _GAMEINPUT_SYSTEM_BUTTON_GUIDE)
            if pressed_now and not pressed_before:
                on_guide_pressed()

        return callback

    @staticmethod
    def _load_library() -> Any:
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise OSError("GameInput is only available through Win32")
        try:
            return win_dll("gameinput.dll")
        except OSError as exc:
            raise OSError("Could not load gameinput.dll") from exc

    @classmethod
    def _create_base_interface(cls, library: Any) -> ctypes.c_void_p:
        try:
            create = cast(Any, library.GameInputCreate)
        except AttributeError as exc:
            raise OSError("gameinput.dll does not export GameInputCreate") from exc
        create.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        create.restype = _HRESULT
        interface = ctypes.c_void_p()
        result = int(create(ctypes.byref(interface)))
        if _hresult_failed(result) or not interface.value:
            raise OSError(
                f"GameInputCreate failed with HRESULT 0x{result & 0xFFFFFFFF:08X}"
            )
        return interface

    @classmethod
    def _query_v2_interface(
        cls,
        base_interface: ctypes.c_void_p,
    ) -> ctypes.c_void_p | None:
        query_interface = cls._method(
            base_interface,
            _VTABLE_QUERY_INTERFACE,
            _HRESULT,
            ctypes.POINTER(_Guid),
            ctypes.POINTER(ctypes.c_void_p),
        )
        v2_interface = ctypes.c_void_p()
        result = int(
            query_interface(
                base_interface,
                ctypes.byref(_IID_IGAMEINPUT_V2),
                ctypes.byref(v2_interface),
            )
        )
        result_code = result & 0xFFFFFFFF
        if result_code == _E_NOINTERFACE:
            return None
        if _hresult_failed(result) or not v2_interface.value:
            raise OSError(
                f"QueryInterface for GameInput v2 failed with HRESULT 0x{result_code:08X}"
            )
        return v2_interface

    @classmethod
    def _set_effective_focus_policy(
        cls,
        interface: ctypes.c_void_p,
        *,
        background_guide_enabled: bool,
        controller_ownership_active: bool,
    ) -> None:
        cls._set_focus_policy(
            interface,
            _effective_gameinput_focus_policy(
                background_guide_enabled=background_guide_enabled,
                controller_ownership_active=controller_ownership_active,
            ),
        )

    @classmethod
    def _set_focus_policy(cls, interface: ctypes.c_void_p, policy: int) -> None:
        set_focus_policy = cls._method(
            interface,
            _VTABLE_SET_FOCUS_POLICY,
            None,
            ctypes.c_uint32,
        )
        set_focus_policy(interface, policy)

    @classmethod
    def _release_interface(cls, interface: ctypes.c_void_p) -> None:
        if not interface.value:
            return
        release = cls._method(interface, _VTABLE_RELEASE, ctypes.c_ulong)
        release(interface)

    @staticmethod
    def _method(
        interface: ctypes.c_void_p,
        index: int,
        restype: Any,
        *argtypes: Any,
    ) -> Any:
        if not interface.value:
            raise OSError("Cannot call a method on a null GameInput interface")
        vtable_pointer = ctypes.cast(
            interface,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        )
        address = vtable_pointer.contents[index]
        if not address:
            raise OSError(f"GameInput vtable slot {index} is null")
        function_type = _WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        return function_type(address)


class ControllerInputOwnershipBackend(Protocol):
    """Best-effort GameInput focus policy backend for controller ownership."""

    @property
    def detail(self) -> str: ...

    def start(self, *, background_guide_enabled: bool) -> bool: ...

    def set_background_guide_enabled(self, enabled: bool) -> None: ...

    def stop(self) -> None: ...


class UnsupportedControllerInputOwnershipBackend:
    """Fail-open controller-ownership backend for unsupported environments."""

    def __init__(
        self, detail: str = "GameInput controller ownership is unavailable"
    ) -> None:
        self._detail = detail

    @property
    def detail(self) -> str:
        return self._detail

    def start(self, *, background_guide_enabled: bool) -> bool:
        del background_guide_enabled
        return False

    def set_background_guide_enabled(self, enabled: bool) -> None:
        del enabled

    def stop(self) -> None:
        return


class GameInputControllerInputOwnershipBackend:
    """Hold GameInput v2 focus policy for exclusive foreground controller delivery."""

    def __init__(self) -> None:
        self._library: Any | None = None
        self._game_input = ctypes.c_void_p()
        self._background_guide_enabled = False
        self._detail = "GameInput controller ownership not started"

    @property
    def detail(self) -> str:
        return self._detail

    def start(self, *, background_guide_enabled: bool) -> bool:
        self._background_guide_enabled = bool(background_guide_enabled)
        if self._game_input.value:
            self.set_background_guide_enabled(background_guide_enabled)
            return True
        library = GameInputGuideButtonBackend._load_library()
        base_interface = GameInputGuideButtonBackend._create_base_interface(library)
        try:
            v2_interface = GameInputGuideButtonBackend._query_v2_interface(
                base_interface
            )
        finally:
            GameInputGuideButtonBackend._release_interface(base_interface)
        if v2_interface is None:
            self._detail = (
                "Installed GameInput runtime does not expose the v2 "
                "focus-policy interface"
            )
            _LOGGER.warning(self._detail)
            return False
        try:
            GameInputGuideButtonBackend._set_effective_focus_policy(
                v2_interface,
                background_guide_enabled=self._background_guide_enabled,
                controller_ownership_active=True,
            )
        except Exception:
            GameInputGuideButtonBackend._release_interface(v2_interface)
            raise
        self._library = library
        self._game_input = v2_interface
        self._detail = "GameInput exclusive foreground controller ownership active"
        _LOGGER.info(self._detail)
        return True

    def set_background_guide_enabled(self, enabled: bool) -> None:
        """Republish the combined process policy after the Guide setting changes."""

        self._background_guide_enabled = bool(enabled)
        interface = self._game_input
        if not interface.value:
            return
        GameInputGuideButtonBackend._set_effective_focus_policy(
            interface,
            background_guide_enabled=self._background_guide_enabled,
            controller_ownership_active=True,
        )

    def stop(self) -> None:
        interface = self._game_input
        if interface.value:
            GameInputGuideButtonBackend._release_interface(interface)
        self._game_input = ctypes.c_void_p()
        self._library = None
        self._detail = "GameInput controller ownership stopped"


class ControllerInputOwnershipService:
    """Activate GameInput ownership only while controller-primary routing needs it."""

    def __init__(self, backend: ControllerInputOwnershipBackend) -> None:
        self._backend = backend
        self._active = False
        self._closed = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def detail(self) -> str:
        return self._backend.detail

    def activate(self, *, background_guide_enabled: bool) -> None:
        if self._closed:
            return
        if self._active:
            self.set_background_guide_enabled(background_guide_enabled)
            return
        try:
            self._active = self._backend.start(
                background_guide_enabled=background_guide_enabled
            )
        except Exception:
            self._active = False
            _LOGGER.exception(
                "GameInput exclusive foreground controller ownership could not start"
            )

    def deactivate(self) -> None:
        if self._closed or not self._active:
            return
        try:
            self._backend.stop()
        except Exception:
            _LOGGER.exception("GameInput controller ownership cleanup failed")
        self._active = False

    def set_background_guide_enabled(self, enabled: bool) -> None:
        if self._closed or not self._active:
            return
        try:
            self._backend.set_background_guide_enabled(enabled)
        except Exception:
            _LOGGER.exception(
                "GameInput controller ownership policy could not be updated"
            )

    def stop(self) -> None:
        if self._closed:
            return
        self.deactivate()
        self._closed = True


def create_platform_controller_input_ownership_service() -> (
    ControllerInputOwnershipService
):
    """Create the GameInput ownership service appropriate for this platform."""

    if sys.platform == "win32":
        return ControllerInputOwnershipService(
            GameInputControllerInputOwnershipBackend()
        )
    return ControllerInputOwnershipService(
        UnsupportedControllerInputOwnershipBackend(
            "GameInput exclusive foreground controller ownership requires Windows"
        )
    )


class GuideButtonInputService(QObject):
    """Expose Guide presses as the shared toggle-overlay navigation command."""

    command_ready = Signal(object)
    availability_changed = Signal(bool, str)

    def __init__(
        self, backend: GuideButtonBackend, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._backend = backend
        self._active = False
        self._closed = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def detail(self) -> str:
        return self._backend.detail

    def start(self) -> None:
        if self._closed or self._active:
            return
        try:
            self._active = self._backend.start(self._on_guide_pressed)
        except Exception:
            self._active = False
            _LOGGER.exception(
                "GameInput Guide-button initialization failed; global hotkey remains available"
            )
        self.availability_changed.emit(self._active, self._backend.detail)

    def set_controller_ownership_active(self, active: bool) -> None:
        """Republish one combined GameInput focus policy on the Guide interface."""

        if self._closed:
            return
        try:
            self._backend.set_controller_ownership_active(active)
        except Exception:
            _LOGGER.exception("GameInput Guide focus policy could not be updated")

    def deactivate(self) -> None:
        """Stop Guide capture without permanently closing the service."""

        if self._closed or not self._active:
            return
        try:
            self._backend.stop()
        except Exception:
            _LOGGER.exception("Guide-button backend cleanup failed")
        self._active = False
        self.availability_changed.emit(False, self._backend.detail)

    def stop(self) -> None:
        if self._closed:
            return
        self.deactivate()
        self._closed = True

    def _on_guide_pressed(self) -> None:
        self.command_ready.emit(NavigationCommand.TOGGLE_OVERLAY)


def create_platform_guide_button_service() -> GuideButtonInputService:
    """Create GameInput Guide support on Windows or a safe no-op fallback."""

    if sys.platform == "win32":
        return GuideButtonInputService(GameInputGuideButtonBackend())
    return GuideButtonInputService(
        UnsupportedGuideButtonBackend(
            "GameInput Guide-button delivery requires Windows"
        )
    )


def _hresult_failed(value: int) -> bool:
    return bool(value & 0x80000000)


def _effective_gameinput_focus_policy(
    *,
    background_guide_enabled: bool,
    controller_ownership_active: bool,
) -> int:
    policy = 0
    if background_guide_enabled:
        policy |= _GAMEINPUT_ENABLE_BACKGROUND_GUIDE_BUTTON
    if controller_ownership_active:
        policy |= _GAMEINPUT_EXCLUSIVE_FOREGROUND_INPUT
    return policy
