"""Application logging and uncaught-exception capture."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Final, Literal

LOG_NAME: Final = "vigil_overlay"
LOG_FILE_NAME: Final = "vigil-overlay.log"
MAX_LOG_BYTES: Final = 2 * 1024 * 1024
BACKUP_LOG_COUNT: Final = 3
LOG_RETENTION_DAYS: Final = 14
_LOG_RETENTION_SECONDS: Final = LOG_RETENTION_DAYS * 24 * 60 * 60

LoggingMode = Literal["diagnostic", "production"]


def resolve_logging_mode(
    *,
    diagnostic_requested: bool,
    production_requested: bool,
    console_available: bool,
) -> LoggingMode:
    """Resolve the runtime logging profile without coupling it to a build tool."""

    if diagnostic_requested and production_requested:
        raise ValueError("Diagnostic and production logging modes are mutually exclusive")
    if diagnostic_requested:
        return "diagnostic"
    if production_requested:
        return "production"
    return "diagnostic" if console_available else "production"


def configure_logging(
    log_root: Path,
    level: str = "INFO",
    *,
    mode: LoggingMode = "diagnostic",
    console: bool = True,
) -> logging.Logger:
    """Configure Vigil logging for an interactive diagnostic or quiet production run.

    Diagnostic mode honors the configured log level and may mirror records to stderr.
    Production mode suppresses routine DEBUG/INFO records, never installs a console
    handler, and delays opening the rotating log until a WARNING-or-higher record exists.
    Both modes retain at most four 2 MiB files and prune history older than 14 days.
    """

    log_root.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOG_NAME)
    configured_level = _parse_level(level)
    effective_level = (
        configured_level if mode == "diagnostic" else max(configured_level, logging.WARNING)
    )
    logger.setLevel(effective_level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    retention_failures = _apply_log_retention(log_root)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    file_handler = RotatingFileHandler(
        log_root / LOG_FILE_NAME,
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_LOG_COUNT,
        encoding="utf-8",
        delay=mode == "production",
    )
    file_handler.setLevel(effective_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if mode == "diagnostic" and console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(effective_level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    _install_exception_hooks(logger)
    for failure in retention_failures:
        logger.warning("Log retention cleanup incomplete: %s", failure)
    logger.debug(
        "Logging configured mode=%s level=%s console=%s",
        mode,
        logging.getLevelName(logger.level),
        mode == "diagnostic" and console,
    )
    return logger


def _apply_log_retention(
    log_root: Path,
    *,
    now: float | None = None,
) -> tuple[str, ...]:
    """Bound managed log history by age, count, and per-file size."""

    cutoff = (time.time() if now is None else now) - _LOG_RETENTION_SECONDS
    failures: list[str] = []

    try:
        candidates = tuple(log_root.iterdir())
    except OSError as exc:
        return (f"could not inspect {log_root}: {exc}",)

    for path in candidates:
        backup_index = _backup_index(path.name)
        if path.name != LOG_FILE_NAME and backup_index is None:
            continue

        try:
            metadata = path.stat()
            expired = metadata.st_mtime < cutoff
            surplus = backup_index is not None and backup_index > BACKUP_LOG_COUNT
            if expired or surplus:
                path.unlink()
                continue
            if metadata.st_size > MAX_LOG_BYTES:
                _retain_recent_log_lines(path, MAX_LOG_BYTES)
                os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        except OSError as exc:
            failures.append(f"could not prune {path.name}: {exc}")

    return tuple(failures)


def _backup_index(name: str) -> int | None:
    prefix = f"{LOG_FILE_NAME}."
    if not name.startswith(prefix):
        return None
    suffix = name.removeprefix(prefix)
    if not suffix.isdecimal():
        return None
    index = int(suffix)
    return index if index > 0 else None


def _retain_recent_log_lines(path: Path, max_bytes: int) -> None:
    """Trim an oversized legacy log to its newest complete records."""

    with path.open("r+b") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if size <= max_bytes:
            return

        stream.seek(size - max_bytes - 1)
        tail = stream.read(max_bytes + 1)
        first_line_end = tail.find(b"\n")
        retained = tail[first_line_end + 1 :] if first_line_end >= 0 else b""
        stream.seek(0)
        stream.write(retained)
        stream.truncate()


def _parse_level(level: str) -> int:
    normalized = level.strip().upper()
    numeric = logging.getLevelName(normalized)
    if not isinstance(numeric, int):
        raise ValueError(f"Unsupported logging level: {level}")
    return numeric


def _install_exception_hooks(logger: logging.Logger) -> None:
    def exception_hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        traceback: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, traceback)
            return
        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, traceback))

    def thread_exception_hook(args: threading.ExceptHookArgs) -> None:
        thread_name = args.thread.name if args.thread else "unknown"
        if args.exc_type is None or args.exc_value is None:
            logger.critical("Uncaught thread exception in %s", thread_name)
            return
        logger.critical(
            "Uncaught thread exception in %s",
            thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = exception_hook
    threading.excepthook = thread_exception_hook
