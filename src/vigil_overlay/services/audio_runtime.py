"""Single-owner background runtime for Windows Core Audio operations."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, Signal

from vigil_overlay.services.audio_control import (
    AudioControlBackend,
    AudioControlError,
    AudioSnapshot,
    create_platform_audio_control_backend,
)

_LOGGER = logging.getLogger("vigil_overlay")
AudioBackendFactory = Callable[[], AudioControlBackend]
_DIAGNOSTIC_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class AudioOperation:
    """One backend mutation executed by the owning audio worker."""

    method: str
    args: tuple[Any, ...] = ()


def diagnose_audio_backend(
    *,
    backend_factory: AudioBackendFactory | None = None,
    timeout_seconds: float = _DIAGNOSTIC_TIMEOUT_SECONDS,
) -> AudioSnapshot:
    """Probe Core Audio on a dedicated owning thread for packaged diagnostics."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    factory = backend_factory or create_platform_audio_control_backend
    completed: queue.Queue[AudioSnapshot] = queue.Queue(maxsize=1)

    def probe() -> None:
        backend: AudioControlBackend | None = None
        try:
            backend = factory()
            snapshot = backend.snapshot()
        except Exception as exc:
            snapshot = AudioSnapshot(
                False,
                f"Could not initialize or read Windows audio controls: {exc}",
            )
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    _LOGGER.exception("Audio diagnostic backend shutdown failed")
        completed.put(snapshot)

    thread = threading.Thread(
        target=probe,
        name="VigilAudioDiagnostic",
        daemon=True,
    )
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        return AudioSnapshot(False, "Windows audio diagnostic timed out.")
    try:
        return completed.get_nowait()
    except queue.Empty:
        return AudioSnapshot(False, "Windows audio diagnostic returned no result.")


class AudioControlRuntime(QObject):
    """Own one audio backend on a daemon thread and coalesce snapshot requests."""

    snapshot_ready = Signal(int, object)
    error_ready = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        backend_factory: AudioBackendFactory | None = None,
    ) -> None:
        super().__init__(parent)
        self._backend_factory = backend_factory or create_platform_audio_control_backend
        self._queue: queue.Queue[tuple[str, object] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._requested_generation = 0
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
            name="VigilAudioControl",
            daemon=True,
        )
        self._thread.start()

    def request_refresh(self) -> int:
        """Request a current snapshot, coalescing duplicate in-flight work."""

        with self._lock:
            if self._closed:
                return self._requested_generation
            self._requested_generation += 1
            generation = self._requested_generation
            if self._refresh_queued or self._refresh_inflight:
                self._refresh_pending = True
                return generation
            self._refresh_queued = True
        self._queue.put(("refresh", generation))
        return generation

    def submit(self, method: str, *args: object) -> None:
        """Queue one mutation; the worker refreshes observed state afterward."""

        if method.startswith("_"):
            raise ValueError("private audio backend methods cannot be submitted")
        with self._lock:
            if self._closed:
                return
        self._queue.put(("operation", AudioOperation(method, tuple(args))))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        self._queue.put(None)
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if thread is None or not thread.is_alive():
            self._thread = None

    def _run(self) -> None:
        try:
            backend = self._backend_factory()
        except Exception as exc:
            if not self._stop.is_set():
                self.error_ready.emit(f"Could not initialize Windows audio controls: {exc}")
            return

        try:
            while not self._stop.is_set():
                command = self._queue.get()
                if command is None:
                    break
                kind, payload = command
                if kind == "operation":
                    self._execute_operation(backend, payload)
                    self.request_refresh()
                    continue
                if isinstance(payload, int):
                    self._execute_refresh(backend, payload)
        finally:
            try:
                backend.close()
            except Exception:
                _LOGGER.debug("Audio backend shutdown failed", exc_info=True)

    def _execute_operation(self, backend: AudioControlBackend, payload: object) -> None:
        if not isinstance(payload, AudioOperation):
            return
        try:
            operation = getattr(backend, payload.method)
            operation(*payload.args)
        except (AudioControlError, AttributeError, TypeError, OSError) as exc:
            self.error_ready.emit(str(exc))
        except Exception as exc:
            _LOGGER.exception("Audio operation %s failed", payload.method)
            self.error_ready.emit(f"Windows audio operation failed: {exc}")

    def _execute_refresh(self, backend: AudioControlBackend, generation: int) -> None:
        with self._lock:
            self._refresh_queued = False
            self._refresh_inflight = True
        try:
            snapshot = backend.snapshot()
        except AudioControlError as exc:
            snapshot = AudioSnapshot(False, str(exc))
        except Exception as exc:
            _LOGGER.exception("Audio snapshot failed")
            snapshot = AudioSnapshot(False, f"Could not read Windows audio state: {exc}")
        if not self._stop.is_set():
            self.snapshot_ready.emit(generation, snapshot)

        next_generation: int | None = None
        with self._lock:
            self._refresh_inflight = False
            if self._refresh_pending and not self._closed:
                self._refresh_pending = False
                self._refresh_queued = True
                next_generation = self._requested_generation
        if next_generation is not None:
            self._queue.put(("refresh", next_generation))
