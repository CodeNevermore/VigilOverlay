"""Background generation-gated integration status discovery."""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Signal

from vigil_overlay.core.worker_lifecycle import join_worker
from vigil_overlay.services.game_library import AggregatedGameLibrary
from vigil_overlay.services.integrations import IntegrationManager

_LOGGER = logging.getLogger("vigil_overlay")


class IntegrationStatusService(QObject):
    """Run all detector, filesystem, and bridge-status work outside the Qt thread."""

    statuses_ready = Signal(object)

    def __init__(self, manager: IntegrationManager) -> None:
        super().__init__()
        self._manager = manager
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._requested_generation = 0
        self._pending: tuple[int, AggregatedGameLibrary | None] | None = None

    def request(self, library: AggregatedGameLibrary | None = None) -> int:
        with self._lock:
            self._requested_generation += 1
            generation = self._requested_generation
            self._pending = (generation, library)
            thread = self._thread
            if thread is not None and thread.is_alive():
                return generation
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="VigilIntegrationStatus",
                daemon=True,
            )
            self._thread.start()
        return generation

    @property
    def running(self) -> bool:
        with self._lock:
            thread = self._thread
            return thread is not None and thread.is_alive()

    def wait_until_idle(self, timeout_seconds: float = 2.0) -> bool:
        """Wait boundedly for the currently requested status pass to finish."""

        with self._lock:
            thread = self._thread
        stopped = join_worker(
            thread,
            timeout_seconds=timeout_seconds,
            worker_name="Integration status worker",
            logger=_LOGGER,
        )
        if stopped:
            with self._lock:
                if self._thread is thread:
                    self._thread = None
        return stopped

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._pending = None
            thread = self._thread
        stopped = join_worker(
            thread,
            timeout_seconds=2.0,
            worker_name="Integration status worker",
            logger=_LOGGER,
        )
        if stopped:
            with self._lock:
                if self._thread is thread:
                    self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                pending = self._pending
                self._pending = None
            if pending is None:
                with self._lock:
                    self._thread = None
                return
            generation, library = pending
            try:
                statuses = self._manager.statuses(library)
            except Exception:
                # Intentional integration boundary: detectors and optional bridges
                # must not take down the Qt host when one status pass fails.
                _LOGGER.exception("Integration status discovery failed unexpectedly")
            else:
                with self._lock:
                    current_generation = self._requested_generation
                if not self._stop.is_set() and generation == current_generation:
                    self.statuses_ready.emit(statuses)


__all__ = ["IntegrationStatusService"]
