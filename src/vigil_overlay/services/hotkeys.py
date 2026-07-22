"""Global hotkey service with an isolated Win32 message-loop backend."""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Protocol, cast

from PySide6.QtCore import QMetaObject, QObject, Qt, Signal, Slot

from vigil_overlay.core.hotkeys import HotkeyCombination

_LOGGER = logging.getLogger("vigil_overlay")
_HOTKEY_ID = 0x5647
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_PM_NOREMOVE = 0x0000
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_MOD_NOREPEAT = 0x4000


@dataclass(frozen=True, slots=True)
class HotkeyRegistration:
    """Outcome of a global hotkey registration attempt."""

    active: bool
    combination: str
    detail: str


class HotkeyBackend(Protocol):
    """Backend contract used by the Qt-facing global hotkey service."""

    def start(
        self,
        combination: HotkeyCombination,
        callback: Callable[[], None],
    ) -> HotkeyRegistration: ...

    def stop(self) -> None: ...


class UnsupportedHotkeyBackend:
    """Predictable no-op backend for unsupported operating systems."""

    def start(
        self,
        combination: HotkeyCombination,
        callback: Callable[[], None],
    ) -> HotkeyRegistration:
        del callback
        return HotkeyRegistration(
            active=False,
            combination=combination.canonical,
            detail="Global hotkeys are supported only on Windows",
        )

    def stop(self) -> None:
        return


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _Message(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", _Point),
        ("lPrivate", wintypes.DWORD),
    ]


class Win32HotkeyBackend:
    """Register a process-global hotkey on a dedicated Win32 message thread."""

    def __init__(self, *, startup_timeout_seconds: float = 2.0) -> None:
        self._startup_timeout_seconds = startup_timeout_seconds
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._started = threading.Event()
        self._cancel_requested = threading.Event()
        self._registration: HotkeyRegistration | None = None
        self._lock = threading.Lock()

    def start(
        self,
        combination: HotkeyCombination,
        callback: Callable[[], None],
    ) -> HotkeyRegistration:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if self._registration is None:
                    raise RuntimeError(
                        "hotkey backend thread is active without a registration state"
                    )
                return self._registration
            self._started.clear()
            self._cancel_requested.clear()
            self._registration = None
            self._thread_id = None
            self._thread = threading.Thread(
                target=self._message_loop,
                args=(combination, callback),
                name="VigilOverlayHotkey",
                daemon=True,
            )
            self._thread.start()

        if not self._started.wait(self._startup_timeout_seconds):
            self._cancel_requested.set()
            self.stop()
            return HotkeyRegistration(
                active=False,
                combination=combination.canonical,
                detail="Timed out while registering the global hotkey",
            )
        registration = self._registration
        if registration is None:
            return HotkeyRegistration(
                active=False,
                combination=combination.canonical,
                detail="Global hotkey registration ended without a result",
            )
        return registration

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            thread_id = self._thread_id
        if thread is None:
            return

        self._cancel_requested.set()
        if thread.is_alive() and thread_id is not None:
            try:
                user32 = _load_win32_library("user32")
                post_thread_message = cast(Any, user32.PostThreadMessageW)
                post_thread_message.argtypes = [
                    wintypes.DWORD,
                    wintypes.UINT,
                    wintypes.WPARAM,
                    wintypes.LPARAM,
                ]
                post_thread_message.restype = wintypes.BOOL
                post_thread_message(thread_id, _WM_QUIT, 0, 0)
            except (AttributeError, OSError):
                _LOGGER.exception(
                    "Could not post shutdown message to global hotkey thread"
                )
        thread.join(timeout=2.0)
        if thread.is_alive():
            _LOGGER.warning(
                "Global hotkey thread did not stop within the shutdown timeout"
            )

        with self._lock:
            self._thread = None
            self._thread_id = None
            self._registration = None
            self._started.clear()

    def _message_loop(
        self,
        combination: HotkeyCombination,
        callback: Callable[[], None],
    ) -> None:
        registered = False
        user32: Any | None = None
        try:
            user32 = _load_win32_library("user32")
            kernel32 = _load_win32_library("kernel32")
            register_hotkey = cast(Any, user32.RegisterHotKey)
            unregister_hotkey = cast(Any, user32.UnregisterHotKey)
            get_message = cast(Any, user32.GetMessageW)
            peek_message = cast(Any, user32.PeekMessageW)
            get_current_thread_id = cast(Any, kernel32.GetCurrentThreadId)

            register_hotkey.argtypes = [
                wintypes.HWND,
                ctypes.c_int,
                wintypes.UINT,
                wintypes.UINT,
            ]
            register_hotkey.restype = wintypes.BOOL
            unregister_hotkey.argtypes = [wintypes.HWND, ctypes.c_int]
            unregister_hotkey.restype = wintypes.BOOL
            get_message.argtypes = [
                ctypes.POINTER(_Message),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
            ]
            get_message.restype = wintypes.BOOL
            peek_message.argtypes = [
                ctypes.POINTER(_Message),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
                wintypes.UINT,
            ]
            peek_message.restype = wintypes.BOOL
            get_current_thread_id.argtypes = []
            get_current_thread_id.restype = wintypes.DWORD

            with self._lock:
                self._thread_id = int(get_current_thread_id())

            # Force creation of this thread's Win32 message queue before start()
            # returns, so shutdown can reliably use PostThreadMessageW.
            queue_probe = _Message()
            peek_message(ctypes.byref(queue_probe), None, 0, 0, _PM_NOREMOVE)
            if self._cancel_requested.is_set():
                self._registration = HotkeyRegistration(
                    active=False,
                    combination=combination.canonical,
                    detail="Global hotkey registration was cancelled",
                )
                return

            modifiers = _win32_modifiers(combination)
            registered = bool(
                register_hotkey(
                    None,
                    _HOTKEY_ID,
                    modifiers | _MOD_NOREPEAT,
                    combination.virtual_key,
                )
            )
            if not registered:
                get_last_error = getattr(ctypes, "get_last_error", None)
                error_code = int(get_last_error()) if callable(get_last_error) else 0
                self._registration = HotkeyRegistration(
                    active=False,
                    combination=combination.canonical,
                    detail=f"Windows rejected the hotkey registration (error {error_code})",
                )
                return

            if self._cancel_requested.is_set():
                return

            self._registration = HotkeyRegistration(
                active=True,
                combination=combination.canonical,
                detail="Global hotkey registered",
            )
            self._started.set()
            message = _Message()
            while True:
                result = int(get_message(ctypes.byref(message), None, 0, 0))
                if result == 0:
                    break
                if result == -1:
                    _LOGGER.error("Windows global hotkey message loop failed")
                    break
                if message.message == _WM_HOTKEY and int(message.wParam) == _HOTKEY_ID:
                    try:
                        callback()
                    except Exception:
                        _LOGGER.exception("Global hotkey callback failed")
        except (AttributeError, OSError) as exc:
            self._registration = HotkeyRegistration(
                active=False,
                combination=combination.canonical,
                detail=f"Could not initialize the Windows hotkey API: {exc}",
            )
        finally:
            self._started.set()
            if registered and user32 is not None:
                try:
                    unregister_hotkey = cast(Any, user32.UnregisterHotKey)
                    unregister_hotkey(None, _HOTKEY_ID)
                except (AttributeError, OSError):
                    _LOGGER.exception("Could not unregister the global hotkey")


class GlobalHotkeyService(QObject):
    """Qt-facing owner for one global overlay-toggle hotkey."""

    activated = Signal()
    registration_changed = Signal(bool, str)

    def __init__(self, backend: HotkeyBackend | None = None) -> None:
        super().__init__()
        self._backend = backend or default_hotkey_backend()
        self._registration: HotkeyRegistration | None = None
        self._started = False
        self._accept_activations = False
        self._activations_suspended = False

    @property
    def registration(self) -> HotkeyRegistration | None:
        return self._registration

    def start(self, combination: HotkeyCombination) -> HotkeyRegistration:
        if self._started:
            self.stop()
        registration = self._backend.start(combination, self._queue_activation)
        self._started = True
        self._registration = registration
        self._accept_activations = (
            registration.active and not self._activations_suspended
        )
        self.registration_changed.emit(registration.active, registration.detail)
        return registration

    def stop(self) -> None:
        self._accept_activations = False
        if not self._started:
            self._registration = None
            return
        self._backend.stop()
        self._started = False
        self._registration = None

    def suspend_activations(self) -> None:
        """Temporarily ignore registered-hotkey activations without unregistering it."""

        self._activations_suspended = True
        self._accept_activations = False

    def resume_activations(self) -> None:
        """Resume activations for the currently active registration, if any."""

        self._activations_suspended = False
        self._accept_activations = bool(
            self._started
            and self._registration is not None
            and self._registration.active
        )

    def _queue_activation(self) -> None:
        if not self._accept_activations:
            return
        QMetaObject.invokeMethod(
            self,
            "_emit_activation",
            Qt.ConnectionType.QueuedConnection,
        )

    @Slot()
    def _emit_activation(self) -> None:
        if self._accept_activations:
            self.activated.emit()


def default_hotkey_backend() -> HotkeyBackend:
    """Create the Win32 global-hotkey backend or an unsupported fallback."""

    if sys.platform == "win32":
        return Win32HotkeyBackend()
    return UnsupportedHotkeyBackend()


def _load_win32_library(name: str) -> Any:
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise OSError("Win32 libraries are unavailable on this platform")
    return win_dll(name, use_last_error=True)


def _win32_modifiers(combination: HotkeyCombination) -> int:
    mapping = {
        "Ctrl": _MOD_CONTROL,
        "Alt": _MOD_ALT,
        "Shift": _MOD_SHIFT,
        "Win": _MOD_WIN,
    }
    value = 0
    for modifier in combination.modifiers:
        value |= mapping[modifier]
    return value
