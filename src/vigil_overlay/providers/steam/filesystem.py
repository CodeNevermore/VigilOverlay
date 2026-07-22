"""Bounded filesystem adapter used by the native Steam provider."""

from __future__ import annotations

import glob as glob_module
from collections.abc import Callable
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Protocol, TypeVar

from vigil_overlay.contracts.games import GameDiscoveryContext

_T = TypeVar("_T")


class SteamFileSystem(Protocol):
    """Bounded filesystem operations required by Steam discovery."""

    def read_text(self, path: str, context: GameDiscoveryContext) -> str: ...

    def is_dir(self, path: str, context: GameDiscoveryContext) -> bool: ...

    def is_file(self, path: str, context: GameDiscoveryContext) -> bool: ...

    def glob(self, pattern: str, context: GameDiscoveryContext) -> tuple[str, ...]: ...


class LocalSteamFileSystem:
    """Use daemon-bounded filesystem calls so disconnected libraries cannot block Vigil."""

    def __init__(self, *, operation_timeout_seconds: float = 1.5) -> None:
        if operation_timeout_seconds <= 0.0:
            raise ValueError("operation_timeout_seconds must be positive")
        self._operation_timeout_seconds = operation_timeout_seconds

    def read_text(self, path: str, context: GameDiscoveryContext) -> str:
        return self._bounded(
            lambda: Path(path).read_text(encoding="utf-8-sig", errors="replace"),
            context,
        )

    def is_dir(self, path: str, context: GameDiscoveryContext) -> bool:
        return self._bounded(lambda: Path(path).is_dir(), context)

    def is_file(self, path: str, context: GameDiscoveryContext) -> bool:
        return self._bounded(lambda: Path(path).is_file(), context)

    def glob(self, pattern: str, context: GameDiscoveryContext) -> tuple[str, ...]:
        return self._bounded(lambda: tuple(sorted(glob_module.glob(pattern))), context)

    def _bounded(
        self, operation: Callable[[], _T], context: GameDiscoveryContext
    ) -> _T:
        if context.is_cancelled():
            raise TimeoutError("Steam discovery was cancelled or its deadline expired")
        remaining = context.remaining_seconds()
        timeout = self._operation_timeout_seconds
        if remaining is not None:
            timeout = min(timeout, remaining)
        if timeout <= 0.0:
            raise TimeoutError("Steam discovery deadline expired")

        queue: Queue[tuple[bool, object]] = Queue(maxsize=1)

        def run() -> None:
            try:
                queue.put((True, operation()))
            except Exception as exc:
                queue.put((False, exc))

        thread = Thread(target=run, name="VigilSteamFileSystem", daemon=True)
        thread.start()
        try:
            succeeded, payload = queue.get(timeout=timeout)
        except Empty as exc:
            raise TimeoutError("Steam filesystem operation timed out") from exc
        if not succeeded:
            if isinstance(payload, Exception):
                raise payload
            raise OSError("Steam filesystem operation failed")
        return payload  # type: ignore[return-value]


__all__ = ["LocalSteamFileSystem", "SteamFileSystem"]
