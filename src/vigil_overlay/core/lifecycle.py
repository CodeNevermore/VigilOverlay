"""Ordered, idempotent lifecycle orchestration for application services."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

_LOGGER = logging.getLogger("vigil_overlay")


@dataclass(frozen=True, slots=True)
class LifecycleAction:
    """One named no-argument startup action."""

    name: str
    callback: Callable[[], None]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("lifecycle action name must be non-empty")

    def run(self) -> None:
        self.callback()


@dataclass(frozen=True, slots=True)
class LifecycleShutdownAction:
    """One named shutdown action that receives the host shutdown reason."""

    name: str
    callback: Callable[[str], None]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("lifecycle shutdown action name must be non-empty")

    @classmethod
    def without_reason(
        cls,
        name: str,
        callback: Callable[[], None],
    ) -> LifecycleShutdownAction:
        """Adapt an ordinary service ``stop`` method to the shutdown contract."""

        def run_without_reason(_reason: str) -> None:
            callback()

        return cls(name, run_without_reason)

    def run(self, reason: str) -> None:
        self.callback(reason)


class ApplicationServiceLifecycle:
    """Own exactly one ordered startup and shutdown pass for host services."""

    def __init__(
        self,
        startup_actions: Sequence[LifecycleAction],
        shutdown_actions: Sequence[LifecycleShutdownAction],
    ) -> None:
        self._startup_actions = tuple(startup_actions)
        self._shutdown_actions = tuple(shutdown_actions)
        _require_unique_names(self._startup_actions, "startup")
        _require_unique_names(self._shutdown_actions, "shutdown")
        self._starting = False
        self._started = False
        self._stopping = False
        self._stopped = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def stopped(self) -> bool:
        return self._stopped

    def start(self) -> bool:
        """Run startup actions once in declaration order."""

        if self._stopped:
            raise RuntimeError("application services cannot restart after shutdown")
        if self._started or self._starting:
            return False

        self._starting = True
        try:
            for action in self._startup_actions:
                _LOGGER.debug("Starting application service lifecycle action: %s", action.name)
                action.run()
        finally:
            self._starting = False
        self._started = True
        return True

    def stop(self, reason: str) -> bool:
        """Run shutdown actions once in declaration order.

        Shutdown remains available before ``start`` because several services are
        initialized and may acquire resources while the application is assembled.
        """

        if self._stopped or self._stopping:
            return False
        if self._starting:
            raise RuntimeError("application services cannot stop during startup")

        self._stopping = True
        try:
            for action in self._shutdown_actions:
                _LOGGER.debug("Stopping application service lifecycle action: %s", action.name)
                action.run(reason)
        finally:
            self._stopping = False
        self._stopped = True
        return True


def _require_unique_names(
    actions: Sequence[LifecycleAction] | Sequence[LifecycleShutdownAction],
    phase: str,
) -> None:
    names = [action.name for action in actions]
    if len(names) != len(set(names)):
        raise ValueError(f"{phase} lifecycle action names must be unique")


__all__ = [
    "ApplicationServiceLifecycle",
    "LifecycleAction",
    "LifecycleShutdownAction",
]
