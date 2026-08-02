"""Verified foreground-process ownership for visible-overlay input control.

Windows does not guarantee that a request to activate a window succeeds.  This
service keeps that native distinction out of the Qt/application layer and makes
foreground ownership an explicit lease condition.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Any, Protocol, cast

_LOGGER = logging.getLogger("vigil_overlay")


class ForegroundOwnershipBackend(Protocol):
    """Native foreground ownership operations independent from Qt."""

    @property
    def supported(self) -> bool: ...

    @property
    def detail(self) -> str: ...

    def request_foreground(self, window_handle: int) -> bool: ...

    def current_process_owns_foreground(self) -> bool: ...


class UnsupportedForegroundOwnershipBackend:
    """Portable fallback that never claims native verification."""

    @property
    def supported(self) -> bool:
        return False

    @property
    def detail(self) -> str:
        return "Foreground-process verification is unavailable on this platform"

    def request_foreground(self, window_handle: int) -> bool:
        del window_handle
        return False

    def current_process_owns_foreground(self) -> bool:
        return False


class WindowsForegroundOwnershipBackend:
    """Verify that the current process owns the Win32 foreground window."""

    def __init__(self) -> None:
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except (AttributeError, OSError) as exc:
            raise OSError("Windows foreground-window APIs are unavailable") from exc
        self._user32 = cast(Any, user32)
        self._kernel32 = cast(Any, kernel32)
        self._configure_api_signatures()
        self._process_id = int(self._kernel32.GetCurrentProcessId())
        self._detail = "Waiting for Vigil to own the Windows foreground process"

    @property
    def supported(self) -> bool:
        return True

    @property
    def detail(self) -> str:
        return self._detail

    def request_foreground(self, window_handle: int) -> bool:
        """Request activation without pretending Windows must grant it."""

        if window_handle <= 0:
            self._detail = "Vigil does not yet have a valid native window handle"
            return False
        requested = bool(self._user32.SetForegroundWindow(wintypes.HWND(window_handle)))
        if requested:
            self._detail = "Windows accepted Vigil's foreground activation request"
        else:
            self._detail = (
                "Windows deferred or rejected Vigil's foreground activation request"
            )
        return requested

    def current_process_owns_foreground(self) -> bool:
        """Accept any foreground HWND owned by Vigil, including host-owned popups."""

        foreground = self._user32.GetForegroundWindow()
        if not foreground:
            self._detail = "Windows currently reports no foreground window"
            return False
        process_id = wintypes.DWORD(0)
        thread_id = int(
            self._user32.GetWindowThreadProcessId(
                foreground,
                ctypes.byref(process_id),
            )
        )
        if thread_id == 0 or process_id.value == 0:
            self._detail = "Windows could not resolve the foreground-window owner"
            return False
        if int(process_id.value) != self._process_id:
            self._detail = "Another process owns the Windows foreground window"
            return False
        self._detail = "Vigil owns the Windows foreground process"
        return True

    def _configure_api_signatures(self) -> None:
        self._user32.GetForegroundWindow.argtypes = []
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self._user32.SetForegroundWindow.restype = wintypes.BOOL
        self._kernel32.GetCurrentProcessId.argtypes = []
        self._kernel32.GetCurrentProcessId.restype = wintypes.DWORD


class ForegroundOwnershipService:
    """Track one fail-open foreground lease around a native backend."""

    def __init__(
        self,
        backend: ForegroundOwnershipBackend,
        *,
        required: bool | None = None,
    ) -> None:
        self._backend = backend
        self._required = backend.supported if required is None else bool(required)
        self._requested = False
        self._verified = False

    @property
    def supported(self) -> bool:
        return self._backend.supported

    @property
    def required(self) -> bool:
        return self._required

    @property
    def requested(self) -> bool:
        return self._requested

    @property
    def verified(self) -> bool:
        return self._verified

    @property
    def detail(self) -> str:
        return self._backend.detail

    def request(self, window_handle: int) -> bool:
        """Request foreground activation; native failures remain non-fatal."""

        self._requested = True
        self._verified = False
        try:
            return self._backend.request_foreground(window_handle)
        except Exception:
            _LOGGER.exception("Vigil foreground activation request failed")
            return False

    def verify(self) -> bool:
        """Refresh the lease from the actual foreground-process owner."""

        if not self.supported:
            self._verified = False
            return False
        try:
            self._verified = bool(self._backend.current_process_owns_foreground())
        except Exception:
            self._verified = False
            _LOGGER.exception("Vigil foreground ownership verification failed")
        return self._verified

    def release(self) -> None:
        """Forget the lease; no persistent native state is owned here."""

        self._requested = False
        self._verified = False


def create_platform_foreground_ownership_service() -> ForegroundOwnershipService:
    """Create Win32 foreground verification or a portable no-op service."""

    if sys.platform != "win32":
        return ForegroundOwnershipService(
            UnsupportedForegroundOwnershipBackend(),
            required=False,
        )
    try:
        return ForegroundOwnershipService(WindowsForegroundOwnershipBackend())
    except OSError as exc:
        _LOGGER.warning(
            "Windows foreground ownership verification is unavailable: %s", exc
        )
        return ForegroundOwnershipService(
            UnsupportedForegroundOwnershipBackend(),
            required=True,
        )


__all__ = [
    "ForegroundOwnershipBackend",
    "ForegroundOwnershipService",
    "UnsupportedForegroundOwnershipBackend",
    "WindowsForegroundOwnershipBackend",
    "create_platform_foreground_ownership_service",
]
