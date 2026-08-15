"""Per-session single-instance ownership and activation signaling."""

from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes
from typing import Any, Protocol, cast

_ERROR_ALREADY_EXISTS = 183
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x80
_MUTEX_NAME = r"Local\VigilOverlay.SingleInstance"
_ACTIVATION_EVENT_NAME = r"Local\VigilOverlay.Activate"
_LOGGER = logging.getLogger("vigil_overlay")


class SingleInstanceBackend(Protocol):
    """Native ownership boundary used by :class:`SingleInstanceGuard`."""

    def acquire(self, timeout_milliseconds: int) -> bool: ...

    def request_activation(self) -> bool: ...

    def consume_activation_request(self) -> bool: ...

    def close(self) -> None: ...


class SingleInstanceGuard:
    """Own one Vigil process and expose bounded activation messages."""

    def __init__(self, backend: SingleInstanceBackend) -> None:
        self._backend = backend
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    def acquire(self, *, timeout_milliseconds: int = 0) -> bool:
        self._acquired = self._backend.acquire(max(timeout_milliseconds, 0))
        return self._acquired

    def request_activation(self) -> bool:
        return self._backend.request_activation()

    def consume_activation_request(self) -> bool:
        if not self._acquired:
            return False
        return self._backend.consume_activation_request()

    def close(self) -> None:
        self._backend.close()
        self._acquired = False


class UnavailableSingleInstanceBackend:
    """No-op source-platform backend that always grants ownership."""

    def acquire(self, timeout_milliseconds: int) -> bool:
        del timeout_milliseconds
        return True

    def request_activation(self) -> bool:
        return False

    def consume_activation_request(self) -> bool:
        return False

    def close(self) -> None:
        return


class WindowsSingleInstanceBackend:
    """Use a local-session mutex and auto-reset event without network IPC."""

    def __init__(self) -> None:
        windll_type = cast(Any, ctypes).WinDLL
        self._kernel32: Any = windll_type("kernel32", use_last_error=True)
        self._configure_api()
        self._mutex: int | None = None
        self._activation_event: int | None = None
        self._owns_mutex = False

    def _configure_api(self) -> None:
        self._kernel32.CreateMutexW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        self._kernel32.CreateMutexW.restype = wintypes.HANDLE
        self._kernel32.CreateEventW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        self._kernel32.CreateEventW.restype = wintypes.HANDLE
        self._kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
        self._kernel32.SetEvent.restype = wintypes.BOOL
        self._kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        self._kernel32.ReleaseMutex.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def acquire(self, timeout_milliseconds: int) -> bool:
        if self._mutex is not None:
            return self._owns_mutex
        mutex = self._kernel32.CreateMutexW(None, True, _MUTEX_NAME)
        if not mutex:
            return False
        self._mutex = int(mutex)
        already_exists = cast(Any, ctypes).get_last_error() == _ERROR_ALREADY_EXISTS
        event = self._kernel32.CreateEventW(None, False, False, _ACTIVATION_EVENT_NAME)
        if event:
            self._activation_event = int(event)
        if not already_exists:
            self._owns_mutex = True
            return True
        if timeout_milliseconds <= 0:
            return False
        result = int(
            self._kernel32.WaitForSingleObject(wintypes.HANDLE(self._mutex), timeout_milliseconds)
        )
        self._owns_mutex = result in {_WAIT_OBJECT_0, _WAIT_ABANDONED}
        return self._owns_mutex

    def request_activation(self) -> bool:
        if self._activation_event is None:
            return False
        return bool(self._kernel32.SetEvent(wintypes.HANDLE(self._activation_event)))

    def consume_activation_request(self) -> bool:
        if self._activation_event is None:
            return False
        result = int(self._kernel32.WaitForSingleObject(wintypes.HANDLE(self._activation_event), 0))
        return result == _WAIT_OBJECT_0

    def close(self) -> None:
        if self._mutex is not None and self._owns_mutex:
            self._kernel32.ReleaseMutex(wintypes.HANDLE(self._mutex))
        for handle in (self._activation_event, self._mutex):
            if handle is not None:
                self._kernel32.CloseHandle(wintypes.HANDLE(handle))
        self._activation_event = None
        self._mutex = None
        self._owns_mutex = False


def create_platform_single_instance_guard() -> SingleInstanceGuard:
    """Create a Windows single-instance guard or a portable source-run fallback."""

    if os.name != "nt":
        return SingleInstanceGuard(UnavailableSingleInstanceBackend())
    try:
        return SingleInstanceGuard(WindowsSingleInstanceBackend())
    except (AttributeError, OSError):
        _LOGGER.warning(
            "Windows single-instance primitives are unavailable; using the fail-open "
            "process-local fallback",
            exc_info=True,
        )
        return SingleInstanceGuard(UnavailableSingleInstanceBackend())


__all__ = [
    "SingleInstanceBackend",
    "SingleInstanceGuard",
    "UnavailableSingleInstanceBackend",
    "WindowsSingleInstanceBackend",
    "create_platform_single_instance_guard",
]
