"""Pure policy helpers for Vigil background residency and recovery safety."""

from __future__ import annotations


def background_recovery_available(
    *,
    tray_available: bool,
    hotkey_active: bool,
) -> bool:
    """Return whether a hidden Vigil instance has a supported restore path."""

    return tray_available or hotkey_active


def background_residency_available(
    *,
    run_in_background: bool,
    tray_available: bool,
    hotkey_active: bool,
) -> bool:
    """Return whether normal hide/close actions may leave Vigil resident."""

    return run_in_background and background_recovery_available(
        tray_available=tray_available,
        hotkey_active=hotkey_active,
    )
