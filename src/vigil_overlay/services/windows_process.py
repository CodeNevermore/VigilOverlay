"""Win32 foreground-process selection used by FPS telemetry targeting."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from typing import Any, Final, cast

from vigil_overlay.services.fps import FpsTarget

_GW_HWNDNEXT: Final[int] = 2
_MAX_Z_ORDER_SCAN: Final[int] = 64
_PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
_TH32CS_SNAPPROCESS: Final[int] = 0x00000002
_MAX_PATH: Final[int] = 260
_INVALID_HANDLE_VALUE: Final[int] = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1
_SYNCHRONIZE: Final[int] = 0x00100000
_WAIT_TIMEOUT: Final[int] = 0x00000102
_WAIT_OBJECT_0: Final[int] = 0x00000000


class _FileTime(ctypes.Structure):
    _fields_ = (("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD))


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * _MAX_PATH),
    )


_EXCLUDED_PROCESS_NAMES = frozenset(
    {
        "vigiloverlay.exe",
        "presentmon.exe",
        "presentmon-2.5.1-x64.exe",
        "explorer.exe",
        "dwm.exe",
        "searchhost.exe",
        "systemsettings.exe",
        "shellexperiencehost.exe",
        "startmenuexperiencehost.exe",
        "applicationframehost.exe",
        "textinputhost.exe",
        "gamebar.exe",
        "gamebarftserver.exe",
        "gamebarpresencewriter.exe",
        "gamebarwidgets.exe",
        "xboxgamebar.exe",
        "pycharm64.exe",
        "code.exe",
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "windowsterminal.exe",
        "taskmgr.exe",
        "chrome.exe",
        "msedge.exe",
        "firefox.exe",
        "brave.exe",
        "opera.exe",
        "discord.exe",
        "ms-teams.exe",
        "spotify.exe",
        "vlc.exe",
        "steam.exe",
        "epicgameslauncher.exe",
        "eadesktop.exe",
        "upc.exe",
        "ubisoftconnect.exe",
        "battle.net.exe",
        "galaxyclient.exe",
        "galaxyclient helper.exe",
        "galaxyclientservice.exe",
        "galaxycommunication.exe",
        "goggalaxy.exe",
    }
)


def is_excluded_process_name(name: str) -> bool:
    """Return whether a process is ineligible for FPS collection."""

    normalized = Path(name).name.casefold()
    if normalized in _EXCLUDED_PROCESS_NAMES:
        return True
    return normalized.startswith("vigilfpsbroker")


def capture_foreground_fps_target() -> FpsTarget | None:
    """Return the first eligible visible foreground/underlay process."""

    targets = capture_foreground_fps_targets(max_candidates=1)
    return targets[0] if targets else None


def capture_foreground_fps_targets(*, max_candidates: int = 64) -> tuple[FpsTarget, ...]:
    """Resolve visible eligible processes from foreground toward the desktop.

    Z-order gives the FPS broker a conservative candidate set tied to what the user can
    actually see. The current foreground window is considered first, followed by visible
    non-minimized windows beneath it. Duplicate process IDs are collapsed.
    """

    if max_candidates <= 0:
        raise ValueError("max_candidates must be positive")
    if os.name != "nt":
        return ()
    windll_type = cast(Any, ctypes).WinDLL
    user32 = windll_type("user32", use_last_error=True)
    kernel32 = windll_type("kernel32", use_last_error=True)

    get_foreground = user32.GetForegroundWindow
    get_foreground.argtypes = ()
    get_foreground.restype = wintypes.HWND
    get_window = user32.GetWindow
    get_window.argtypes = (wintypes.HWND, wintypes.UINT)
    get_window.restype = wintypes.HWND
    is_visible = user32.IsWindowVisible
    is_visible.argtypes = (wintypes.HWND,)
    is_visible.restype = wintypes.BOOL
    is_iconic = user32.IsIconic
    is_iconic.argtypes = (wintypes.HWND,)
    is_iconic.restype = wintypes.BOOL

    hwnd = get_foreground()
    if not hwnd:
        return ()

    seen_windows: set[int] = set()
    seen_processes: set[int] = set()
    targets: list[FpsTarget] = []
    current = hwnd
    for _ in range(_MAX_Z_ORDER_SCAN):
        raw_handle = int(current) if current else 0
        if raw_handle <= 0 or raw_handle in seen_windows:
            break
        seen_windows.add(raw_handle)
        if bool(is_visible(current)) and not bool(is_iconic(current)):
            target = _target_from_window(user32, kernel32, current)
            if target is not None and target.process_id not in seen_processes:
                seen_processes.add(target.process_id)
                targets.append(target)
                if len(targets) >= max_candidates:
                    break
        current = get_window(current, _GW_HWNDNEXT)
    return tuple(targets)


def _target_from_window(user32: Any, kernel32: Any, hwnd: Any) -> FpsTarget | None:
    get_pid = user32.GetWindowThreadProcessId
    get_pid.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    get_pid.restype = wintypes.DWORD

    process_id = wintypes.DWORD(0)
    get_pid(hwnd, ctypes.byref(process_id))
    pid = int(process_id.value)
    if pid <= 0 or pid == os.getpid():
        return None

    process_name, executable_path, process_started_at_100ns = _query_process_details(kernel32, pid)
    if process_name and is_excluded_process_name(process_name):
        return None

    # QueryFullProcessImageNameW / GetProcessTimes can be denied for elevated or protected
    # game processes. PresentMon targets by PID, so losing friendly metadata must not discard
    # an otherwise valid foreground window.
    return FpsTarget(
        pid,
        process_name or f"pid-{pid}.exe",
        process_started_at_100ns=process_started_at_100ns,
        executable_path=executable_path or None,
    )


def _query_process_details(
    kernel32: Any,
    process_id: int,
) -> tuple[str, str, int | None]:
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    query_name = kernel32.QueryFullProcessImageNameW
    query_name.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    query_name.restype = wintypes.BOOL
    get_process_times = getattr(kernel32, "GetProcessTimes", None)
    if get_process_times is not None:
        get_process_times.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
        )
        get_process_times.restype = wintypes.BOOL

    handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    if not handle:
        return (_query_process_snapshot_name(kernel32, process_id), "", None)
    try:
        process_name = ""
        executable_path = ""
        capacity = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        if query_name(handle, 0, buffer, ctypes.byref(capacity)):
            executable_path = buffer.value
            process_name = Path(executable_path).name
        else:
            process_name = _query_process_snapshot_name(kernel32, process_id)

        process_started_at_100ns: int | None = None
        if get_process_times is not None:
            creation = _FileTime()
            exit_time = _FileTime()
            kernel_time = _FileTime()
            user_time = _FileTime()
            if get_process_times(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                process_started_at_100ns = (int(creation.dwHighDateTime) << 32) | int(
                    creation.dwLowDateTime
                )

        return (process_name, executable_path, process_started_at_100ns)
    finally:
        close_handle(handle)


def _query_process_snapshot_name(kernel32: Any, process_id: int) -> str:
    """Resolve an executable name without requiring a process query handle."""

    create_snapshot = getattr(kernel32, "CreateToolhelp32Snapshot", None)
    process_first = getattr(kernel32, "Process32FirstW", None)
    process_next = getattr(kernel32, "Process32NextW", None)
    close_handle = getattr(kernel32, "CloseHandle", None)
    if (
        create_snapshot is None
        or process_first is None
        or process_next is None
        or close_handle is None
    ):
        return ""

    create_snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    create_snapshot.restype = wintypes.HANDLE
    process_first.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W))
    process_first.restype = wintypes.BOOL
    process_next.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W))
    process_next.restype = wintypes.BOOL
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    snapshot = create_snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot in {-1, _INVALID_HANDLE_VALUE}:
        return ""
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        has_entry = bool(process_first(snapshot, ctypes.byref(entry)))
        while has_entry:
            if int(entry.th32ProcessID) == process_id:
                return Path(entry.szExeFile).name
            has_entry = bool(process_next(snapshot, ctypes.byref(entry)))
    finally:
        close_handle(snapshot)
    return ""


def is_fps_target_alive(target: FpsTarget) -> bool:
    """Return whether a PID still names the same live process, failing open on denial."""

    if os.name != "nt":
        return False
    windll_type = cast(Any, ctypes).WinDLL
    kernel32 = windll_type("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_for_single_object.restype = wintypes.DWORD

    handle = open_process(
        _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE,
        False,
        target.process_id,
    )
    if not handle:
        # PresentMon is already configured to terminate when the target exits. A transient,
        # protected-process, or otherwise unavailable liveness handle must not tear down a
        # provider-owned trace session and start a replacement loop.
        return True
    try:
        wait_result = int(wait_for_single_object(handle, 0))
        if wait_result == _WAIT_OBJECT_0:
            return False
        if wait_result != _WAIT_TIMEOUT:
            return True
    finally:
        close_handle(handle)

    expected_started_at = target.process_started_at_100ns
    if expected_started_at is None:
        return True
    _name, _path, observed_started_at = _query_process_details(kernel32, target.process_id)
    return observed_started_at is None or observed_started_at == expected_started_at
