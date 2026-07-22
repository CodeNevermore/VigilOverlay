"""Win32 foreground-process selection used by FPS telemetry targeting."""

from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes
from pathlib import Path
from typing import Any, Final, cast

from vigil_overlay.services.fps import FpsTarget
from vigil_overlay.services.windows_telemetry import sample_process_gpu_usage

_LOGGER = logging.getLogger("vigil_overlay")
_GW_HWNDNEXT: Final[int] = 2
_MAX_Z_ORDER_SCAN: Final[int] = 64


class _FileTime(ctypes.Structure):
    _fields_ = (("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD))


_EXCLUDED_PROCESS_NAMES = frozenset(
    {
        "vigiloverlay.exe",
        "presentmon.exe",
        "presentmon-2.5.1-x64.exe",
        "explorer.exe",
        "dwm.exe",
        "searchhost.exe",
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


def capture_foreground_fps_targets(
    *, max_candidates: int = 64
) -> tuple[FpsTarget, ...]:
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


def rank_fps_targets_by_gpu(
    targets: tuple[FpsTarget, ...],
    gpu_usage_by_pid: dict[int, float],
) -> tuple[FpsTarget, ...]:
    """Rank visible candidates by current process GPU activity, preserving Z-order ties."""

    indexed = tuple(enumerate(targets))
    return tuple(
        target
        for _index, target in sorted(
            indexed,
            key=lambda item: (
                -max(gpu_usage_by_pid.get(item[1].process_id, 0.0), 0.0),
                item[0],
            ),
        )
    )


def capture_ranked_fps_targets(
    *,
    sample_interval_seconds: float = 0.25,
    max_candidates: int = 8,
) -> tuple[FpsTarget, ...]:
    """Return visible FPS candidates ordered by busiest current GPU process first.

    GPU counter collection is best-effort. If PDH counters are unavailable, the original
    foreground/underlay Z-order is preserved. This function is intended for the FPS broker
    worker thread and may wait briefly between PDH rate samples.
    """

    candidates = capture_foreground_fps_targets(max_candidates=max(max_candidates, 1))
    if not candidates:
        return ()
    try:
        gpu_usage = sample_process_gpu_usage(
            sample_interval_seconds=sample_interval_seconds
        )
    except OSError:
        _LOGGER.exception(
            "Per-process GPU counters unavailable; using FPS Z-order candidates"
        )
        return candidates[:max_candidates]
    ranked = rank_fps_targets_by_gpu(candidates, gpu_usage)
    if ranked:
        summary = ", ".join(
            f"{target.executable_name}:{gpu_usage.get(target.process_id, 0.0):.1f}%"
            for target in ranked[:max_candidates]
        )
        _LOGGER.info("FPS GPU-ranked process candidates: %s", summary)
    return ranked[:max_candidates]


def _target_from_window(user32: Any, kernel32: Any, hwnd: Any) -> FpsTarget | None:
    get_pid = user32.GetWindowThreadProcessId
    get_pid.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    get_pid.restype = wintypes.DWORD

    process_id = wintypes.DWORD(0)
    get_pid(hwnd, ctypes.byref(process_id))
    pid = int(process_id.value)
    if pid <= 0 or pid == os.getpid():
        return None

    process_name, process_started_at_100ns = _query_process_details(kernel32, pid)
    if process_name and is_excluded_process_name(process_name):
        return None

    # QueryFullProcessImageNameW / GetProcessTimes can be denied for elevated or protected
    # game processes. PresentMon targets by PID, so losing friendly metadata must not discard
    # an otherwise valid foreground window.
    return FpsTarget(
        pid,
        process_name or f"pid-{pid}.exe",
        process_started_at_100ns=process_started_at_100ns,
    )


def _query_process_details(kernel32: Any, process_id: int) -> tuple[str, int | None]:
    process_query_limited_information = 0x1000
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

    handle = open_process(process_query_limited_information, False, process_id)
    if not handle:
        return ("", None)
    try:
        process_name = ""
        capacity = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        if query_name(handle, 0, buffer, ctypes.byref(capacity)):
            process_name = Path(buffer.value).name

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

        return (process_name, process_started_at_100ns)
    finally:
        close_handle(handle)
