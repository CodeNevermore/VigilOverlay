"""Application logging and uncaught-exception capture."""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Final, Literal

LOG_NAME: Final = "vigil_overlay"
MAX_LOG_BYTES: Final = 5 * 1024 * 1024
BACKUP_LOG_COUNT: Final = 5

LoggingMode = Literal["diagnostic", "production"]


def resolve_logging_mode(
    *,
    diagnostic_requested: bool,
    production_requested: bool,
    console_available: bool,
) -> LoggingMode:
    """Resolve the runtime logging profile without coupling it to a build tool."""

    if diagnostic_requested and production_requested:
        raise ValueError(
            "Diagnostic and production logging modes are mutually exclusive"
        )
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
    """

    log_root.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOG_NAME)
    configured_level = _parse_level(level)
    effective_level = (
        configured_level
        if mode == "diagnostic"
        else max(configured_level, logging.WARNING)
    )
    logger.setLevel(effective_level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    file_handler = RotatingFileHandler(
        log_root / "vigil-overlay.log",
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
    logger.debug(
        "Logging configured mode=%s level=%s console=%s",
        mode,
        logging.getLevelName(logger.level),
        mode == "diagnostic" and console,
    )
    return logger


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
