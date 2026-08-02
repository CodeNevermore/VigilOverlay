"""Windows WH_MOUSE_LL / WH_KEYBOARD_LL containment backend.

The hook callbacks intentionally contain no Qt/application calls. They only read an
immutable containment plan, classify Windows-provided injection flags, optionally
queue a bounded diagnostic record, and return either 1 or CallNextHookEx.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
from ctypes import wintypes
from typing import Any, Final, cast

from vigil_overlay.core.input_routing import OverlayInputMode
from vigil_overlay.services.input_containment import (
    HookDiagnosticRecord,
    InputContainmentPlan,
    InputInjectionClass,
    classify_keyboard_hook_flags,
    classify_mouse_hook_flags,
    should_swallow_keyboard,
    should_swallow_mouse,
)

_LOGGER = logging.getLogger("vigil_overlay")
_WH_KEYBOARD_LL: Final[int] = 13
_WH_MOUSE_LL: Final[int] = 14
_WM_QUIT: Final[int] = 0x0012
_PM_NOREMOVE: Final[int] = 0x0000
_HOOK_START_TIMEOUT_SECONDS: Final[float] = 3.0
_HOOK_STOP_TIMEOUT_SECONDS: Final[float] = 3.0
_DIAGNOSTIC_QUEUE_LIMIT: Final[int] = 256

_LRESULT = ctypes.c_ssize_t
_WPARAM = ctypes.c_size_t
_LPARAM = ctypes.c_ssize_t
_HOOK_FACTORY = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_HOOKPROC = _HOOK_FACTORY(_LRESULT, ctypes.c_int, _WPARAM, _LPARAM)


def _windows_last_error() -> int:
    getter = getattr(ctypes, "get_last_error", None)
    if getter is None:
        return 0
    return int(cast(Any, getter)())


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _MouseLowLevelHookStruct(ctypes.Structure):
    _fields_ = [
        ("pt", _Point),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KeyboardLowLevelHookStruct(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class WindowsLowLevelHookContainmentBackend:
    """Own global low-level hooks on a dedicated native message-loop thread."""

    def __init__(self, *, diagnostics_enabled: bool | None = None) -> None:
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except (AttributeError, OSError) as exc:
            raise OSError("Windows low-level hook APIs are unavailable") from exc

        self._user32 = cast(Any, user32)
        self._kernel32 = cast(Any, kernel32)
        self._configure_api_signatures()
        self._diagnostics_enabled = (
            _LOGGER.isEnabledFor(logging.DEBUG)
            if diagnostics_enabled is None
            else diagnostics_enabled
        )
        self._diagnostics: queue.Queue[HookDiagnosticRecord] = queue.Queue(
            maxsize=_DIAGNOSTIC_QUEUE_LIMIT
        )
        self._plan = InputContainmentPlan(
            mode=OverlayInputMode.HIDDEN,
            install_hooks=False,
            swallow_mouse=False,
            swallow_injected_keyboard=False,
        )
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._stop_requested = threading.Event()
        self._callback_faulted = threading.Event()
        self._startup_complete = threading.Event()
        self._startup_succeeded = False
        self._mouse_hook: int | None = None
        self._keyboard_hook: int | None = None
        self._hook_lock = threading.Lock()
        self._mouse_callback: Any | None = None
        self._keyboard_callback: Any | None = None
        self._detail = "Windows low-level input containment idle"

    @property
    def supported(self) -> bool:
        return True

    @property
    def detail(self) -> str:
        return self._detail

    @property
    def healthy(self) -> bool:
        thread = self._thread
        with self._hook_lock:
            hooks_active = (
                self._mouse_hook is not None and self._keyboard_hook is not None
            )
        return bool(
            thread is not None
            and thread.is_alive()
            and self._startup_succeeded
            and hooks_active
            and not self._callback_faulted.is_set()
            and not self._stop_requested.is_set()
        )

    def start(self, plan: InputContainmentPlan) -> bool:
        if not plan.install_hooks:
            self.stop()
            return False
        if self._thread is not None and self._thread.is_alive():
            self.update_plan(plan)
            return self._startup_succeeded

        self._plan = plan
        self._stop_requested.clear()
        self._callback_faulted.clear()
        self._startup_complete.clear()
        self._startup_succeeded = False
        self._thread = threading.Thread(
            target=self._thread_main,
            name="VigilInputContainment",
            daemon=True,
        )
        self._thread.start()
        if not self._startup_complete.wait(_HOOK_START_TIMEOUT_SECONDS):
            self._detail = "Timed out while installing Windows low-level input hooks"
            _LOGGER.error(self._detail)
            self.stop()
            return False
        if not self._startup_succeeded:
            self.stop()
            return False
        return True

    def update_plan(self, plan: InputContainmentPlan) -> None:
        self._plan = plan
        if not plan.install_hooks:
            self.stop()

    def stop(self) -> None:
        """Unhook first, then stop the native loop; never leave swallowing behind."""

        self._stop_requested.set()
        self._release_hooks()
        thread_id = self._thread_id
        if thread_id:
            posted = bool(self._user32.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0))
            if not posted:
                error = _windows_last_error()
                _LOGGER.warning(
                    "Could not post quit to input-containment hook thread (error=%d)",
                    error,
                )
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(_HOOK_STOP_TIMEOUT_SECONDS)
            if thread.is_alive():
                _LOGGER.error(
                    "Input-containment hook thread did not stop promptly; hooks were already "
                    "explicitly released fail-open"
                )
        self._thread = None
        self._thread_id = 0
        self._startup_succeeded = False
        self._detail = "Windows low-level input containment stopped"

    def drain_diagnostics(self) -> tuple[HookDiagnosticRecord, ...]:
        records: list[HookDiagnosticRecord] = []
        while True:
            try:
                records.append(self._diagnostics.get_nowait())
            except queue.Empty:
                return tuple(records)

    def _configure_api_signatures(self) -> None:
        self._user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            _HOOKPROC,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        self._user32.SetWindowsHookExW.restype = ctypes.c_void_p
        self._user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        self._user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        self._user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            _WPARAM,
            _LPARAM,
        ]
        self._user32.CallNextHookEx.restype = _LRESULT
        self._user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._user32.GetMessageW.restype = wintypes.BOOL
        self._user32.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._user32.PeekMessageW.restype = wintypes.BOOL
        self._user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD,
            wintypes.UINT,
            _WPARAM,
            _LPARAM,
        ]
        self._user32.PostThreadMessageW.restype = wintypes.BOOL
        self._kernel32.GetCurrentThreadId.argtypes = []
        self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def _thread_main(self) -> None:
        self._thread_id = int(self._kernel32.GetCurrentThreadId())
        message = wintypes.MSG()
        # Force creation of this thread's message queue before start() can post WM_QUIT.
        self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, _PM_NOREMOVE)
        try:
            self._mouse_callback = _HOOKPROC(self._mouse_hook_callback)
            self._keyboard_callback = _HOOKPROC(self._keyboard_hook_callback)
            mouse_hook = self._user32.SetWindowsHookExW(
                _WH_MOUSE_LL, self._mouse_callback, None, 0
            )
            if not mouse_hook:
                self._detail = (
                    "WH_MOUSE_LL installation failed with Windows error "
                    f"{_windows_last_error()}"
                )
                _LOGGER.error(self._detail)
                return
            with self._hook_lock:
                self._mouse_hook = int(mouse_hook)

            keyboard_hook = self._user32.SetWindowsHookExW(
                _WH_KEYBOARD_LL, self._keyboard_callback, None, 0
            )
            if not keyboard_hook:
                self._detail = (
                    "WH_KEYBOARD_LL installation failed with Windows error "
                    f"{_windows_last_error()}"
                )
                _LOGGER.error(self._detail)
                self._release_hooks()
                return
            with self._hook_lock:
                self._keyboard_hook = int(keyboard_hook)

            self._startup_succeeded = True
            self._detail = "WH_MOUSE_LL + WH_KEYBOARD_LL active for controller-primary"
            _LOGGER.debug("%s", self._detail)
        except Exception:
            self._detail = "Unexpected exception while installing low-level input hooks"
            _LOGGER.exception(self._detail)
            self._release_hooks()
            return
        finally:
            self._startup_complete.set()

        try:
            while not self._stop_requested.is_set():
                result = int(
                    self._user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                )
                if result <= 0:
                    break
        except Exception:
            _LOGGER.exception("Input-containment native message loop failed")
        finally:
            if self._callback_faulted.is_set():
                self._detail = (
                    "Low-level input hook callback failed; containment released"
                )
                _LOGGER.error(
                    "A low-level input hook callback failed; containment was released fail-open"
                )
            elif not self._stop_requested.is_set():
                self._detail = (
                    "Input-containment native message loop stopped unexpectedly"
                )
            self._release_hooks()
            self._startup_succeeded = False
            self._thread_id = 0

    def _release_hooks(self) -> None:
        with self._hook_lock:
            handles = (self._mouse_hook, self._keyboard_hook)
            self._mouse_hook = None
            self._keyboard_hook = None
        for handle in handles:
            if handle is None:
                continue
            try:
                released = bool(
                    self._user32.UnhookWindowsHookEx(ctypes.c_void_p(handle))
                )
            except Exception:
                _LOGGER.exception(
                    "Unexpected error while releasing a Windows low-level input hook"
                )
                continue
            if not released:
                _LOGGER.warning(
                    "Windows reported a low-level input hook teardown failure (error=%d)",
                    _windows_last_error(),
                )

    def _mouse_hook_callback(self, n_code: int, w_param: int, l_param: int) -> int:
        try:
            if n_code >= 0:
                data = ctypes.cast(
                    l_param, ctypes.POINTER(_MouseLowLevelHookStruct)
                ).contents
                classification = classify_mouse_hook_flags(int(data.flags))
                swallowed = should_swallow_mouse(self._plan, classification)
                self._queue_diagnostic("mouse", classification, swallowed)
                if swallowed:
                    return 1
        except Exception:
            self._fail_open_from_callback()
        return int(self._user32.CallNextHookEx(None, n_code, w_param, l_param))

    def _keyboard_hook_callback(self, n_code: int, w_param: int, l_param: int) -> int:
        try:
            if n_code >= 0:
                data = ctypes.cast(
                    l_param, ctypes.POINTER(_KeyboardLowLevelHookStruct)
                ).contents
                classification = classify_keyboard_hook_flags(int(data.flags))
                swallowed = should_swallow_keyboard(self._plan, classification)
                self._queue_diagnostic("keyboard", classification, swallowed)
                if swallowed:
                    return 1
        except Exception:
            self._fail_open_from_callback()
        return int(self._user32.CallNextHookEx(None, n_code, w_param, l_param))

    def _fail_open_from_callback(self) -> None:
        """Disable future swallowing and ask the native loop to unwind hooks."""

        self._plan = InputContainmentPlan(
            mode=self._plan.mode,
            install_hooks=False,
            swallow_mouse=False,
            swallow_injected_keyboard=False,
        )
        self._callback_faulted.set()
        self._stop_requested.set()
        thread_id = self._thread_id
        if thread_id:
            self._user32.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0)

    def _queue_diagnostic(
        self, source: str, classification: InputInjectionClass, swallowed: bool
    ) -> None:
        if not self._diagnostics_enabled:
            return
        try:
            self._diagnostics.put_nowait(
                HookDiagnosticRecord(
                    source=source,
                    classification=classification,
                    swallowed=swallowed,
                )
            )
        except queue.Full:
            return
