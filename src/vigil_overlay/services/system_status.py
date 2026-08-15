"""Failure-isolated live status boundary for Vigil's top-right header indicators."""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

from PySide6.QtCore import QObject, Signal

from vigil_overlay.core.worker_lifecycle import join_worker
from vigil_overlay.services.audio_control import (
    AudioControlBackend,
    AudioControlError,
    create_platform_audio_control_backend,
)

_LOGGER = logging.getLogger("vigil_overlay")
_AC_LINE_OFFLINE: Final[int] = 0
_AC_LINE_ONLINE: Final[int] = 1
_BATTERY_FLAG_NO_BATTERY: Final[int] = 128
_BATTERY_FLAG_UNKNOWN: Final[int] = 255
_BATTERY_PERCENT_UNKNOWN: Final[int] = 255


@dataclass(frozen=True, slots=True)
class OverlayStatusSnapshot:
    """Current state shown by the compact header status indicators."""

    microphone_muted: bool | None = None
    battery_present: bool | None = None
    battery_percent: int | None = None
    power_plugged: bool | None = None
    network_connected: bool | None = None


class OverlayStatusBackend(Protocol):
    """Read-only system-status contract used by the overlay header."""

    def snapshot(self) -> OverlayStatusSnapshot: ...

    def close(self) -> None: ...


class UnavailableOverlayStatusBackend:
    """Failure-isolated backend used outside Windows or after setup failure."""

    def snapshot(self) -> OverlayStatusSnapshot:
        return OverlayStatusSnapshot()

    def close(self) -> None:
        return


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = (
        ("ac_line_status", ctypes.c_ubyte),
        ("battery_flag", ctypes.c_ubyte),
        ("battery_life_percent", ctypes.c_ubyte),
        ("system_status_flag", ctypes.c_ubyte),
        ("battery_life_time", wintypes.DWORD),
        ("battery_full_life_time", wintypes.DWORD),
    )


class WindowsOverlayStatusBackend:
    """Windows local-state backend with no network requests or private databases."""

    def __init__(self, *, audio_backend: AudioControlBackend | None = None) -> None:
        if sys.platform != "win32" or os.name != "nt":
            raise RuntimeError("Overlay system status is available on Windows only.")
        windll_type = cast(Any, ctypes).WinDLL
        self._kernel32: Any = windll_type("kernel32", use_last_error=True)
        self._wininet: Any = windll_type("wininet", use_last_error=True)
        self._audio_backend: AudioControlBackend = (
            audio_backend or create_platform_audio_control_backend()
        )
        self._configure_api()

    def snapshot(self) -> OverlayStatusSnapshot:
        microphone_muted = self._read_microphone_muted()
        battery_present, battery_percent, power_plugged = self._read_power_status()
        network_connected = self._read_network_connected()
        return OverlayStatusSnapshot(
            microphone_muted=microphone_muted,
            battery_present=battery_present,
            battery_percent=battery_percent,
            power_plugged=power_plugged,
            network_connected=network_connected,
        )

    def close(self) -> None:
        """Release the audio backend on the worker thread that created it."""

        self._audio_backend.close()

    def _configure_api(self) -> None:
        self._kernel32.GetSystemPowerStatus.argtypes = (ctypes.POINTER(_SystemPowerStatus),)
        self._kernel32.GetSystemPowerStatus.restype = wintypes.BOOL
        self._wininet.InternetGetConnectedState.argtypes = (
            ctypes.POINTER(wintypes.DWORD),
            wintypes.DWORD,
        )
        self._wininet.InternetGetConnectedState.restype = wintypes.BOOL

    def _read_microphone_muted(self) -> bool | None:
        try:
            return self._audio_backend.microphone_muted()
        except AudioControlError:
            _LOGGER.debug("Header microphone status unavailable", exc_info=True)
            return None
        except Exception:
            _LOGGER.debug("Header microphone status failed", exc_info=True)
            return None

    def _read_power_status(self) -> tuple[bool | None, int | None, bool | None]:
        status = _SystemPowerStatus()
        try:
            if not self._kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                return None, None, None
        except Exception:
            _LOGGER.debug("Header power status failed", exc_info=True)
            return None, None, None

        battery_flag = int(status.battery_flag)
        battery_present = (
            None
            if battery_flag == _BATTERY_FLAG_UNKNOWN
            else battery_flag != _BATTERY_FLAG_NO_BATTERY
        )
        percent = int(status.battery_life_percent)
        battery_percent = (
            percent
            if battery_present is True
            and percent != _BATTERY_PERCENT_UNKNOWN
            and 0 <= percent <= 100
            else None
        )
        ac_line = int(status.ac_line_status)
        power_plugged = (
            True if ac_line == _AC_LINE_ONLINE else False if ac_line == _AC_LINE_OFFLINE else None
        )
        return battery_present, battery_percent, power_plugged

    def _read_network_connected(self) -> bool | None:
        flags = wintypes.DWORD(0)
        try:
            return bool(self._wininet.InternetGetConnectedState(ctypes.byref(flags), 0))
        except Exception:
            _LOGGER.debug("Header network status failed", exc_info=True)
            return None


def create_platform_overlay_status_backend() -> OverlayStatusBackend:
    """Create the Windows status backend without making it a startup dependency."""

    if sys.platform != "win32" or os.name != "nt":
        return UnavailableOverlayStatusBackend()
    try:
        return WindowsOverlayStatusBackend()
    except Exception:
        _LOGGER.warning("Windows overlay status backend unavailable", exc_info=True)
        return UnavailableOverlayStatusBackend()


OverlayStatusBackendFactory = Callable[[], OverlayStatusBackend]


class OverlayStatusRuntime(QObject):
    """Own blocking status and COM work on one background thread."""

    snapshot_ready = Signal(object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        backend_factory: OverlayStatusBackendFactory | None = None,
    ) -> None:
        super().__init__(parent)
        self._backend_factory = backend_factory or create_platform_overlay_status_backend
        self._queue: queue.Queue[bool | None] = queue.Queue()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._refresh_queued = False
        self._refresh_inflight = False
        self._refresh_pending = False
        self._closed = False
        if parent is not None:
            parent.destroyed.connect(self.close)
        self.start()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        if self._closed or self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="VigilOverlayStatus",
            daemon=True,
        )
        self._thread.start()

    def request_refresh(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._refresh_queued or self._refresh_inflight:
                self._refresh_pending = True
                return
            self._refresh_queued = True
        self._queue.put(True)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        self._queue.put(None)
        thread = self._thread
        stopped = join_worker(
            thread,
            timeout_seconds=2.0,
            worker_name="Overlay status worker",
            logger=_LOGGER,
        )
        if stopped:
            self._thread = None

    def _run(self) -> None:
        try:
            backend = self._backend_factory()
        except Exception:
            _LOGGER.warning("Overlay status backend unavailable", exc_info=True)
            if not self._stop.is_set():
                self.snapshot_ready.emit(OverlayStatusSnapshot())
            return
        try:
            while not self._stop.is_set():
                command = self._queue.get()
                if command is None:
                    break
                with self._lock:
                    self._refresh_queued = False
                    self._refresh_inflight = True
                try:
                    snapshot = backend.snapshot()
                except Exception:
                    _LOGGER.debug("Overlay status refresh failed", exc_info=True)
                    snapshot = OverlayStatusSnapshot()
                if not self._stop.is_set():
                    self.snapshot_ready.emit(snapshot)
                queue_again = False
                with self._lock:
                    self._refresh_inflight = False
                    if self._refresh_pending and not self._closed:
                        self._refresh_pending = False
                        self._refresh_queued = True
                        queue_again = True
                if queue_again:
                    self._queue.put(True)
        finally:
            try:
                backend.close()
            except Exception:
                _LOGGER.debug("Overlay status backend shutdown failed", exc_info=True)


__all__ = [
    "OverlayStatusBackend",
    "OverlayStatusBackendFactory",
    "OverlayStatusRuntime",
    "OverlayStatusSnapshot",
    "UnavailableOverlayStatusBackend",
    "WindowsOverlayStatusBackend",
    "create_platform_overlay_status_backend",
]
