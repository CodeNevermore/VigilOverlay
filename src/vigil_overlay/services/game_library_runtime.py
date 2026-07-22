"""Non-blocking Qt bridge for provider library discovery and scoped refreshes."""

from __future__ import annotations

import logging
from collections import deque
from threading import Event, Lock, Thread

from PySide6.QtCore import QObject, Signal

from vigil_overlay.services.game_library import GameLibraryAggregator

_LOGGER = logging.getLogger("vigil_overlay")


class GameLibraryService(QObject):
    """Run provider discovery off the Qt UI thread and publish normalized snapshots."""

    library_ready = Signal(object)

    def __init__(self, aggregator: GameLibraryAggregator) -> None:
        super().__init__()
        self._aggregator = aggregator
        self._stop_event = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._pending: deque[str | None] = deque()

    def start(self, provider_id: str | None = None) -> None:
        """Start full discovery or queue one provider-scoped refresh."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if provider_id not in self._pending:
                    self._pending.append(provider_id)
                return
            self._stop_event.clear()
            self._thread = Thread(
                target=self._run,
                args=(provider_id,),
                name="VigilGameLibraryDiscovery",
                daemon=True,
            )
            self._thread.start()

    def refresh_provider(self, provider_id: str) -> None:
        if not provider_id:
            raise ValueError("provider_id must be non-empty")
        self.start(provider_id)

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            self._pending.clear()

    def _run(self, provider_id: str | None) -> None:
        current = provider_id
        while not self._stop_event.is_set():
            try:
                library = self._aggregator.aggregate(
                    provider_id=current,
                    cancellation_event=self._stop_event,
                )
            except Exception:
                _LOGGER.exception("Game library discovery failed unexpectedly")
            else:
                if not self._stop_event.is_set():
                    self.library_ready.emit(library)

            with self._lock:
                if self._stop_event.is_set() or not self._pending:
                    self._thread = None
                    return
                current = self._pending.popleft()


__all__ = ["GameLibraryService"]
