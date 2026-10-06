"""Retire abandoned Vigil FPS traces without touching active capture sessions."""

from __future__ import annotations

import ctypes
import logging
import os
import re
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, cast

import psutil  # type: ignore[import-untyped]

_LOGGER = logging.getLogger("vigil_overlay")
_SESSION_PATTERN = re.compile(r"VigilOverlayFPS-([1-9][0-9]{0,9})(?:-[0-9a-f]{32})?", re.IGNORECASE)
_NAME_CHARACTERS = 1024
_MAX_SESSIONS = 1024
_ERROR_MORE_DATA = 234
_ERROR_WMI_INSTANCE_NOT_FOUND = 4201
_EVENT_TRACE_CONTROL_STOP = 1


class _WnodeHeader(ctypes.Structure):
    _fields_ = [
        ("BufferSize", wintypes.ULONG),
        ("ProviderId", wintypes.ULONG),
        ("HistoricalContext", ctypes.c_ulonglong),
        ("TimeStamp", ctypes.c_longlong),
        ("Guid", ctypes.c_ubyte * 16),
        ("ClientContext", wintypes.ULONG),
        ("Flags", wintypes.ULONG),
    ]


class _TraceProperties(ctypes.Structure):
    _fields_ = [
        ("Wnode", _WnodeHeader),
        ("BufferSize", wintypes.ULONG),
        ("MinimumBuffers", wintypes.ULONG),
        ("MaximumBuffers", wintypes.ULONG),
        ("MaximumFileSize", wintypes.ULONG),
        ("LogFileMode", wintypes.ULONG),
        ("FlushTimer", wintypes.ULONG),
        ("EnableFlags", wintypes.ULONG),
        ("AgeLimit", wintypes.LONG),
        ("NumberOfBuffers", wintypes.ULONG),
        ("FreeBuffers", wintypes.ULONG),
        ("EventsLost", wintypes.ULONG),
        ("BuffersWritten", wintypes.ULONG),
        ("LogBuffersLost", wintypes.ULONG),
        ("RealTimeBuffersLost", wintypes.ULONG),
        ("LoggerThreadId", wintypes.HANDLE),
        ("LogFileNameOffset", wintypes.ULONG),
        ("LoggerNameOffset", wintypes.ULONG),
    ]


class _TraceBuffer(ctypes.Structure):
    _fields_ = [
        ("properties", _TraceProperties),
        ("session_name", ctypes.c_wchar * _NAME_CHARACTERS),
        ("log_file_name", ctypes.c_wchar * _NAME_CHARACTERS),
    ]


def _new_trace_buffer() -> _TraceBuffer:
    buffer = _TraceBuffer()
    buffer.properties.Wnode.BufferSize = ctypes.sizeof(buffer)
    buffer.properties.LoggerNameOffset = _TraceBuffer.session_name.offset
    buffer.properties.LogFileNameOffset = _TraceBuffer.log_file_name.offset
    return buffer


def _trace_api() -> Any:
    api = cast(Any, ctypes).WinDLL("advapi32", use_last_error=True)
    api.QueryAllTracesW.argtypes = (
        ctypes.POINTER(ctypes.POINTER(_TraceProperties)),
        wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG),
    )
    api.QueryAllTracesW.restype = wintypes.ULONG
    api.ControlTraceW.argtypes = (
        ctypes.c_ulonglong,
        wintypes.LPCWSTR,
        ctypes.POINTER(_TraceProperties),
        wintypes.ULONG,
    )
    api.ControlTraceW.restype = wintypes.ULONG
    return api


def _is_vigil_session(name: str) -> bool:
    match = _SESSION_PATTERN.fullmatch(name)
    return match is not None and int(match[1]) <= 0xFFFFFFFF


def _query_vigil_sessions() -> tuple[str, ...]:
    api = _trace_api()
    capacity = 64
    for _attempt in range(3):
        buffers = [_new_trace_buffer() for _ in range(capacity)]
        pointers = (ctypes.POINTER(_TraceProperties) * capacity)(
            *(ctypes.pointer(buffer.properties) for buffer in buffers)
        )
        count = wintypes.ULONG()
        status = int(api.QueryAllTracesW(pointers, capacity, ctypes.byref(count)))
        if status == _ERROR_MORE_DATA and capacity < count.value <= _MAX_SESSIONS:
            capacity = count.value
            continue
        if status != 0:
            raise OSError(status, "Vigil FPS trace enumeration failed")
        names = tuple(buffer.session_name for buffer in buffers[: min(count.value, capacity)])
        return tuple(name for name in names if _is_vigil_session(name))
    raise OSError(_ERROR_MORE_DATA, "Vigil FPS trace enumeration kept growing")


def _active_collector_sessions() -> frozenset[str] | None:
    """An unreadable collector defers cleanup instead of guessing ownership."""

    sessions: set[str] = set()
    for process in psutil.process_iter(("name",)):
        if process.info.get("name") is None:
            return None
        name = str(process.info.get("name") or "").casefold()
        if not name.startswith("presentmon") or not name.endswith(".exe"):
            continue
        try:
            command = process.cmdline()
        except psutil.NoSuchProcess, psutil.ZombieProcess:
            continue
        except psutil.AccessDenied:
            return None
        if not command:
            return None
        for index, argument in enumerate(command):
            option = argument.casefold()
            if option in {"--session_name", "-session_name"}:
                if index + 1 >= len(command):
                    return None
                sessions.add(command[index + 1].casefold())
            elif option.startswith("--session_name="):
                sessions.add(argument.split("=", 1)[1].casefold())
    return frozenset(sessions)


def _process_session_id(process_id: int) -> int:
    """Read the Windows session of a PID; never guess after native failure."""

    api = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    api.ProcessIdToSessionId.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    api.ProcessIdToSessionId.restype = wintypes.BOOL
    session_id = wintypes.DWORD()
    if not api.ProcessIdToSessionId(process_id, ctypes.byref(session_id)):
        native = cast(Any, ctypes)
        raise native.WinError(native.get_last_error())
    return int(session_id.value)


@dataclass(frozen=True, slots=True)
class _VigilCollector:
    process: Any
    trace_name: str


def _vigil_collectors(windows_session_id: int) -> tuple[_VigilCollector, ...]:
    """Identify private collectors belonging to the caller's Windows session."""

    collectors: list[_VigilCollector] = []
    for process in psutil.process_iter(("name",)):
        name = process.info.get("name")
        if name is None:
            raise OSError("Collector ownership could not be read")
        folded = str(name).casefold()
        if not folded.startswith("presentmon") or not folded.endswith(".exe"):
            continue
        try:
            if _process_session_id(process.pid) != windows_session_id:
                continue
            command = process.cmdline()
        except psutil.NoSuchProcess, psutil.ZombieProcess:
            continue
        except OSError:
            if not process.is_running():
                continue
            raise
        if not command:
            raise OSError("Collector ownership could not be read")
        for index, argument in enumerate(command):
            option = argument.casefold()
            session = ""
            if option in {"--session_name", "-session_name"}:
                if index + 1 >= len(command):
                    raise OSError("Collector session name could not be read")
                session = command[index + 1]
            elif option.startswith("--session_name="):
                session = argument.split("=", 1)[1]
            if _is_vigil_session(session):
                collectors.append(_VigilCollector(process, session))
                break
    return tuple(collectors)


def _reset_trace_names(windows_session_id: int, known_names: frozenset[str]) -> tuple[str, ...]:
    """Select inactive traces with readable current-session ownership evidence.

    Legacy names identify the game PID; current names identify the Vigil PID.
    A dead or unreadable PID alone cannot attribute an orphan to a Windows session.
    Names captured from our collectors remain attributable after those processes exit.
    """

    names = _query_vigil_sessions()
    active = _active_collector_sessions()
    if active is None:
        raise OSError("FPS trace ownership could not be read")
    owned: list[str] = []
    for name in names:
        folded = name.casefold()
        if folded in active:
            continue
        if folded in known_names:
            owned.append(name)
            continue
        match = _SESSION_PATTERN.fullmatch(name)
        if match is None:
            continue
        try:
            if _process_session_id(int(match[1])) == windows_session_id:
                owned.append(name)
        except OSError:
            _LOGGER.warning("FPS reset preserved a trace with unknown ownership: %s", name)
    return tuple(owned)


def reset_vigil_fps_sessions() -> str | None:
    """Explicit recovery: close current-session collectors and attributable traces.

    The caller must own the single-instance guard and must not have started any
    services. Return a visible failure rather than starting competing FPS capture.
    """

    if os.name != "nt":
        return None
    try:
        windows_session_id = _process_session_id(os.getpid())
        inventory = _vigil_collectors(windows_session_id)
        known_names = frozenset(collector.trace_name.casefold() for collector in inventory)
        collectors = tuple(collector.process for collector in inventory)
        for process in collectors:
            with suppress(psutil.NoSuchProcess):
                process.terminate()
        _gone, alive = psutil.wait_procs(collectors, timeout=1.5)
        for process in alive:
            with suppress(psutil.NoSuchProcess):
                process.kill()
        _gone, alive = psutil.wait_procs(alive, timeout=1.5)
        if alive:
            raise OSError("A Vigil FPS collector did not stop")
        # A foreign session can capture concurrently despite our per-session guard.
        for name in _reset_trace_names(windows_session_id, known_names):
            active = _active_collector_sessions()
            if active is None:
                raise OSError("FPS trace ownership could not be refreshed")
            if name.casefold() in active:
                continue
            _stop_vigil_session(name)
        if _vigil_collectors(windows_session_id) or _reset_trace_names(
            windows_session_id, known_names
        ):
            raise OSError("Current-session Vigil FPS capture remains after reset")
    except OSError, psutil.Error:
        _LOGGER.warning("Vigil FPS recovery could not complete", exc_info=True)
        return (
            "Vigil could not close all of its previous FPS sessions. FPS is paused for "
            "this run. Restart Vigil with administrator access and try Safe Mode again."
        )
    _LOGGER.warning("Vigil FPS recovery completed for the current Windows session")
    return None


def _stop_vigil_session(name: str) -> None:
    if not _is_vigil_session(name):
        raise ValueError("Refusing cleanup of a non-Vigil trace session")
    buffer = _new_trace_buffer()
    status = int(
        _trace_api().ControlTraceW(
            0, name, ctypes.byref(buffer.properties), _EVENT_TRACE_CONTROL_STOP
        )
    )
    if status not in {0, _ERROR_WMI_INSTANCE_NOT_FOUND}:
        raise OSError(status, "Vigil FPS trace cleanup failed")


def retire_abandoned_fps_sessions() -> None:
    """Run before the first collector; skip all sessions with a live consumer."""

    if os.name != "nt":
        return
    try:
        names = _query_vigil_sessions()
        for name in names:
            # Refresh before each stop to protect captures started during enumeration.
            active = _active_collector_sessions()
            if active is None:
                _LOGGER.warning(
                    "Old Vigil FPS trace cleanup deferred: collector access unavailable"
                )
                return
            if name.casefold() in active:
                continue
            try:
                _stop_vigil_session(name)
            except OSError:
                _LOGGER.warning(
                    "Could not retire abandoned Vigil FPS trace %s", name, exc_info=True
                )
                continue
            _LOGGER.warning("Retired abandoned Vigil FPS trace from an earlier run: %s", name)
    except OSError, psutil.Error:
        _LOGGER.warning(
            "Old Vigil FPS trace cleanup is unavailable; capture will continue", exc_info=True
        )
