"""Shared bounded shutdown helpers for Vigil-owned background threads."""

from __future__ import annotations

import logging
import threading


def join_worker(
    thread: threading.Thread | None,
    *,
    timeout_seconds: float,
    worker_name: str,
    logger: logging.Logger,
    timeout_level: int = logging.WARNING,
) -> bool:
    """Wait boundedly for one worker and report whether it has stopped.

    Cancellation or wakeup signaling remains the owning service's responsibility.
    Keeping that ordering local makes native teardown rules explicit while giving all
    services the same join, self-join, and timeout behavior.
    """

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if not worker_name.strip():
        raise ValueError("worker_name must not be empty")
    if thread is None or not thread.is_alive():
        return True
    if thread is threading.current_thread():
        logger.debug("%s shutdown requested from its own worker; join deferred", worker_name)
        return False
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        logger.log(
            timeout_level,
            "%s did not stop within %.2f seconds",
            worker_name,
            timeout_seconds,
        )
        return False
    return True


__all__ = ["join_worker"]
