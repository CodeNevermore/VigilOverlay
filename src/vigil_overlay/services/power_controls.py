"""Documented Windows power/session actions behind a testable service boundary."""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast


class PowerAction(StrEnum):
    SLEEP = "sleep"
    HIBERNATE = "hibernate"
    RESTART = "restart"
    SHUT_DOWN = "shut_down"

    @property
    def label(self) -> str:
        return {
            PowerAction.SLEEP: "Sleep",
            PowerAction.HIBERNATE: "Hibernate",
            PowerAction.RESTART: "Restart",
            PowerAction.SHUT_DOWN: "Shut down",
        }[self]


@dataclass(frozen=True, slots=True)
class PowerCapabilities:
    sleep: bool = False
    hibernate: bool = False
    restart: bool = False
    shut_down: bool = False

    def actions(self) -> tuple[PowerAction, ...]:
        supported = {
            PowerAction.SLEEP: self.sleep,
            PowerAction.HIBERNATE: self.hibernate,
            PowerAction.RESTART: self.restart,
            PowerAction.SHUT_DOWN: self.shut_down,
        }
        return tuple(action for action, enabled in supported.items() if enabled)


class PowerControlBackend(Protocol):
    def capabilities(self) -> PowerCapabilities: ...

    def execute(self, action: PowerAction) -> None: ...


class UnsupportedPowerControlBackend:
    def capabilities(self) -> PowerCapabilities:
        return PowerCapabilities()

    def execute(self, action: PowerAction) -> None:
        raise OSError(f"{action.label} is unavailable on this platform")


class WindowsPowerControlBackend:
    """Use PowrProf and User32 rather than shell or PowerShell commands."""

    _TOKEN_ADJUST_PRIVILEGES = 0x0020
    _TOKEN_QUERY = 0x0008
    _SE_PRIVILEGE_ENABLED = 0x00000002
    _EWX_SHUTDOWN = 0x00000001
    _EWX_REBOOT = 0x00000002
    _EWX_FORCEIFHUNG = 0x00000010
    _SHTDN_REASON_MAJOR_APPLICATION = 0x00040000
    _SHTDN_REASON_MINOR_OTHER = 0x00000000
    _SHTDN_REASON_FLAG_PLANNED = 0x80000000

    def __init__(self) -> None:
        win_dll = vars(ctypes).get("WinDLL")
        if not callable(win_dll):
            raise OSError("Win32 dynamic-library loading is unavailable")
        self._powrprof = win_dll("powrprof", use_last_error=True)
        self._user32 = win_dll("user32", use_last_error=True)
        self._advapi32 = win_dll("advapi32", use_last_error=True)
        self._kernel32 = win_dll("kernel32", use_last_error=True)

        self._powrprof.IsPwrSuspendAllowed.argtypes = []
        self._powrprof.IsPwrSuspendAllowed.restype = wintypes.BOOL
        self._powrprof.IsPwrHibernateAllowed.argtypes = []
        self._powrprof.IsPwrHibernateAllowed.restype = wintypes.BOOL
        self._powrprof.SetSuspendState.argtypes = [
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.BOOL,
        ]
        self._powrprof.SetSuspendState.restype = wintypes.BOOL
        self._user32.ExitWindowsEx.argtypes = [wintypes.UINT, wintypes.DWORD]
        self._user32.ExitWindowsEx.restype = wintypes.BOOL
        self._kernel32.GetCurrentProcess.argtypes = []
        self._kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self._advapi32.OpenProcessToken.restype = wintypes.BOOL
        self._advapi32.LookupPrivilegeValueW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.POINTER(_Luid),
        ]
        self._advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
        self._advapi32.AdjustTokenPrivileges.argtypes = [
            wintypes.HANDLE,
            wintypes.BOOL,
            ctypes.POINTER(_TokenPrivileges),
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL

    def capabilities(self) -> PowerCapabilities:
        return PowerCapabilities(
            sleep=bool(self._powrprof.IsPwrSuspendAllowed()),
            hibernate=bool(self._powrprof.IsPwrHibernateAllowed()),
            restart=True,
            shut_down=True,
        )

    def execute(self, action: PowerAction) -> None:
        if action in {PowerAction.SLEEP, PowerAction.HIBERNATE}:
            hibernate = action is PowerAction.HIBERNATE
            allowed = (
                self._powrprof.IsPwrHibernateAllowed()
                if hibernate
                else self._powrprof.IsPwrSuspendAllowed()
            )
            if not allowed:
                raise OSError(f"{action.label} is not supported or enabled")
            if not self._powrprof.SetSuspendState(hibernate, False, False):
                raise _win_error()
            return

        self._enable_shutdown_privilege()
        flags = (
            self._EWX_REBOOT if action is PowerAction.RESTART else self._EWX_SHUTDOWN
        ) | self._EWX_FORCEIFHUNG
        reason = (
            self._SHTDN_REASON_MAJOR_APPLICATION
            | self._SHTDN_REASON_MINOR_OTHER
            | self._SHTDN_REASON_FLAG_PLANNED
        )
        if not self._user32.ExitWindowsEx(flags, reason):
            raise _win_error()

    def _enable_shutdown_privilege(self) -> None:
        token = wintypes.HANDLE()
        process = self._kernel32.GetCurrentProcess()
        if not self._advapi32.OpenProcessToken(
            process,
            self._TOKEN_ADJUST_PRIVILEGES | self._TOKEN_QUERY,
            ctypes.byref(token),
        ):
            raise _win_error()
        try:
            luid = _Luid()
            if not self._advapi32.LookupPrivilegeValueW(
                None, "SeShutdownPrivilege", ctypes.byref(luid)
            ):
                raise _win_error()
            privileges = _TokenPrivileges(
                privilege_count=1,
                privileges=(_LuidAndAttributes)(
                    luid=luid,
                    attributes=self._SE_PRIVILEGE_ENABLED,
                ),
            )
            _set_last_error(0)
            if not self._advapi32.AdjustTokenPrivileges(
                token,
                False,
                ctypes.byref(privileges),
                0,
                None,
                None,
            ):
                raise _win_error()
            error = _last_error()
            if error:
                raise _win_error(error)
        finally:
            self._kernel32.CloseHandle(token)


class _Luid(ctypes.Structure):
    _fields_ = [("low_part", wintypes.DWORD), ("high_part", wintypes.LONG)]


class _LuidAndAttributes(ctypes.Structure):
    _fields_ = [("luid", _Luid), ("attributes", wintypes.DWORD)]


class _TokenPrivileges(ctypes.Structure):
    _fields_ = [
        ("privilege_count", wintypes.DWORD),
        ("privileges", _LuidAndAttributes),
    ]


def _last_error() -> int:
    getter = cast(Callable[[], int] | None, vars(ctypes).get("get_last_error"))
    return int(getter()) if getter is not None else 0


def _set_last_error(error: int) -> None:
    setter = cast(Callable[[int], None] | None, vars(ctypes).get("set_last_error"))
    if setter is not None:
        setter(error)


def _win_error(error: int | None = None) -> OSError:
    code = _last_error() if error is None else error
    constructor = cast(Callable[[int], OSError] | None, vars(ctypes).get("WinError"))
    if constructor is not None:
        return constructor(code)
    return OSError(code, f"Windows operation failed with error {code}")


class PowerControlService:
    def __init__(self, backend: PowerControlBackend) -> None:
        self._backend = backend

    def capabilities(self) -> PowerCapabilities:
        return self._backend.capabilities()

    def execute(self, action: PowerAction) -> tuple[bool, str]:
        if action not in self.capabilities().actions():
            return False, f"{action.label} is not available on this system."
        try:
            self._backend.execute(action)
        except OSError as exc:
            return False, f"Windows could not {action.label.casefold()}: {exc}"
        return True, f"{action.label} requested."


def create_platform_power_control_service() -> PowerControlService:
    backend: PowerControlBackend = UnsupportedPowerControlBackend()
    if sys.platform == "win32":
        try:
            backend = WindowsPowerControlBackend()
        except (AttributeError, OSError):
            backend = UnsupportedPowerControlBackend()
    return PowerControlService(backend)


__all__ = [
    "PowerAction",
    "PowerCapabilities",
    "PowerControlBackend",
    "PowerControlService",
    "create_platform_power_control_service",
]
