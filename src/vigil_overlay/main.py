"""Vigil Overlay bootstrap entry point."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from vigil_overlay.core.config import (
    AppConfig,
    ConfigLoadNotice,
    load_config_with_notice,
    save_config,
)
from vigil_overlay.core.errors import VigilOverlayError
from vigil_overlay.core.logging_setup import configure_logging, resolve_logging_mode
from vigil_overlay.core.paths import ApplicationPaths
from vigil_overlay.core.single_instance import create_platform_single_instance_guard
from vigil_overlay.core.version import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the supported command-line interface for source and packaged runs."""

    parser = argparse.ArgumentParser(prog="VigilOverlay")
    parser.add_argument("--version", action="version", version=f"Vigil Overlay {__version__}")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Initialize the application and print non-sensitive path/config diagnostics.",
    )
    logging_group = parser.add_mutually_exclusive_group()
    logging_group.add_argument(
        "--diagnostic-mode",
        action="store_true",
        help=(
            "Enable detailed configured-level logging. With a console, diagnostics are mirrored "
            "there; without one, they are written to the rotating diagnostic log."
        ),
    )
    logging_group.add_argument(
        "--production-mode",
        action="store_true",
        help=(
            "Suppress routine DEBUG/INFO logging and keep only WARNING-or-higher records in the "
            "rotating log. No console log handler is installed."
        ),
    )
    parser.add_argument(
        "--safe-mode",
        action="store_true",
        help=(
            "Start with default in-memory settings, plugins disabled, and primary-screen "
            "overlay geometry without overwriting the saved configuration."
        ),
    )
    parser.add_argument(
        "--reset-window-position",
        action="store_true",
        help="Clear saved monitor coordinates before starting the overlay.",
    )
    parser.add_argument(
        "--wait-for-instance-exit",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--controller-isolation-watchdog",
        type=Path,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Initialize Vigil and return its process exit code."""

    args = build_parser().parse_args(argv)
    if args.controller_isolation_watchdog is not None:
        paths = ApplicationPaths.discover()
        from vigil_overlay.services.controller_isolation import (
            run_controller_isolation_watchdog,
        )

        return run_controller_isolation_watchdog(
            args.controller_isolation_watchdog,
            paths.cache_root,
            paths.user_data_root,
        )
    instance_guard = None
    if not args.diagnose:
        instance_guard = create_platform_single_instance_guard()
        wait_milliseconds = 10_000 if args.wait_for_instance_exit else 0
        if not instance_guard.acquire(timeout_milliseconds=wait_milliseconds):
            if not args.wait_for_instance_exit:
                instance_guard.request_activation()
            instance_guard.close()
            return 0
    try:
        paths = ApplicationPaths.discover()
        paths.ensure_user_directories()
        config_path = paths.user_config_root / "settings.json"
        if args.safe_mode:
            config = AppConfig()
            config_notice = None
            config.navigation.selected_widget = "home"
            config.window.x = None
            config.window.y = None
        else:
            config, config_notice = load_config_with_notice(config_path)
            if args.reset_window_position:
                config.window.x = None
                config.window.y = None
                try:
                    save_config(config_path, config)
                except VigilOverlayError as exc:
                    config_notice = ConfigLoadNotice(str(exc), None, False)
        console_available = _console_stream_available()
        logging_mode = resolve_logging_mode(
            diagnostic_requested=args.diagnostic_mode,
            production_requested=args.production_mode,
            console_available=console_available,
        )
        logger = configure_logging(
            paths.log_root,
            level=config.log_level,
            mode=logging_mode,
            console=console_available,
        )
        logger.info(
            "Vigil Overlay starting, version=%s, safe_mode=%s",
            __version__,
            args.safe_mode,
        )
        if config_notice is not None:
            logger.warning(
                "%s Preserved settings: %s; replacement persisted: %s",
                config_notice.reason,
                config_notice.preserved_path or "unavailable",
                config_notice.settings_persisted,
            )

        if args.diagnose:
            _print_diagnostics(paths, config_path, config.schema_version)
            return 0

        try:
            from vigil_overlay.application import run_gui
        except ImportError as exc:
            raise VigilOverlayError(
                "PySide6 is required to start the Vigil Overlay host UI"
            ) from exc
        return run_gui(
            config,
            config_path,
            safe_mode=args.safe_mode,
            read_only_config=args.safe_mode,
            single_instance_guard=instance_guard,
        )
    except (VigilOverlayError, OSError, ValueError) as exc:
        logging.getLogger("vigil_overlay").exception("Application startup failed: %s", exc)
        if _console_stream_available():
            print(f"Vigil Overlay startup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if instance_guard is not None:
            instance_guard.close()


def _console_stream_available() -> bool:
    stream = sys.stderr
    if stream is None or not callable(getattr(stream, "write", None)):
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _print_diagnostics(paths: ApplicationPaths, config_path: Path, schema_version: int) -> None:
    payload = {
        "product": "Vigil Overlay",
        "version": __version__,
        "config_schema_version": schema_version,
        "config_path": str(config_path),
        "paths": {name: str(path) for name, path in paths.as_dict().items()},
        "audio": _audio_diagnostics(),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def _audio_diagnostics() -> dict[str, object]:
    """Return a non-sensitive Core Audio summary from the packaged runtime path."""

    from vigil_overlay.services.audio_runtime import diagnose_audio_backend

    snapshot = diagnose_audio_backend()
    return {
        "available": snapshot.available,
        "detail": snapshot.detail,
        "output_device_count": len(snapshot.output_devices),
        "input_device_count": len(snapshot.input_devices),
        "session_count": len(snapshot.sessions),
    }
