"""Windows-native overlay hit testing and topmost z-order coordination."""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

_LOGGER = logging.getLogger("vigil_overlay")

_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_WM_NCHITTEST = 0x0084
_WM_MOUSEACTIVATE = 0x0021
_WM_DISPLAYCHANGE = 0x007E
_WM_DPICHANGED = 0x02E0
_HTCLIENT = 1
_MA_NOACTIVATE = 3

_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020


class _WindowsMessage(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("w_param", wintypes.WPARAM),
        ("l_param", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("point", wintypes.POINT),
        ("private", wintypes.DWORD),
    ]


def overlay_extended_style(current_style: int) -> int:
    """Return a focusable tool-window style that never clicks through."""

    return (
        (current_style | _WS_EX_TOOLWINDOW) & ~_WS_EX_TRANSPARENT & ~_WS_EX_NOACTIVATE
    )


def backdrop_extended_style(current_style: int) -> int:
    """Return a non-activating, input-blocking topmost backdrop style."""

    return (current_style | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE) & ~_WS_EX_TRANSPARENT


def configure_native_overlay_window(window_id: int) -> bool:
    """Remove click-through styles and refresh the native frame on Windows."""

    if sys.platform != "win32":
        return False

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    get_window_long = user32.GetWindowLongPtrW
    get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
    get_window_long.restype = ctypes.c_ssize_t
    set_window_long = user32.SetWindowLongPtrW
    set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    set_window_long.restype = ctypes.c_ssize_t

    hwnd = wintypes.HWND(window_id)
    ctypes.set_last_error(0)
    current_style = int(get_window_long(hwnd, _GWL_EXSTYLE))
    get_error = ctypes.get_last_error()
    if current_style == 0 and get_error:
        _LOGGER.error("Could not read overlay window style: WinError %s", get_error)
        return False

    requested_style = overlay_extended_style(current_style)
    style_changed = requested_style != current_style
    if style_changed:
        ctypes.set_last_error(0)
        previous_style = int(set_window_long(hwnd, _GWL_EXSTYLE, requested_style))
        set_error = ctypes.get_last_error()
        if previous_style == 0 and set_error:
            _LOGGER.error(
                "Could not configure overlay window style: WinError %s", set_error
            )
            return False

    return enforce_native_topmost(window_id, frame_changed=style_changed)


def configure_native_backdrop_window(window_id: int) -> bool:
    """Configure the dim backdrop as non-activating while preserving hit testing."""

    if sys.platform != "win32":
        return False

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    get_window_long = user32.GetWindowLongPtrW
    get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
    get_window_long.restype = ctypes.c_ssize_t
    set_window_long = user32.SetWindowLongPtrW
    set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    set_window_long.restype = ctypes.c_ssize_t

    hwnd = wintypes.HWND(window_id)
    ctypes.set_last_error(0)
    current_style = int(get_window_long(hwnd, _GWL_EXSTYLE))
    get_error = ctypes.get_last_error()
    if current_style == 0 and get_error:
        _LOGGER.error("Could not read backdrop window style: WinError %s", get_error)
        return False

    requested_style = backdrop_extended_style(current_style)
    style_changed = requested_style != current_style
    if style_changed:
        ctypes.set_last_error(0)
        previous_style = int(set_window_long(hwnd, _GWL_EXSTYLE, requested_style))
        set_error = ctypes.get_last_error()
        if previous_style == 0 and set_error:
            _LOGGER.error(
                "Could not configure backdrop window style: WinError %s", set_error
            )
            return False

    return enforce_native_topmost(window_id, frame_changed=style_changed)


def native_topmost_flags(*, frame_changed: bool = False) -> int:
    """Return non-activating z-order flags without forcing a show operation."""

    flags = _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE
    if frame_changed:
        flags |= _SWP_FRAMECHANGED
    return flags


def enforce_native_topmost(window_id: int, *, frame_changed: bool = False) -> bool:
    """Move the overlay into the native topmost band without stealing focus."""

    if sys.platform != "win32":
        return False

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    set_window_pos = user32.SetWindowPos
    set_window_pos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    set_window_pos.restype = wintypes.BOOL

    flags = native_topmost_flags(frame_changed=frame_changed)

    ctypes.set_last_error(0)
    succeeded = bool(
        set_window_pos(
            wintypes.HWND(window_id),
            wintypes.HWND(-1),  # HWND_TOPMOST
            0,
            0,
            0,
            0,
            flags,
        )
    )
    if succeeded:
        return True

    _LOGGER.error(
        "Could not enforce overlay topmost z-order: WinError %s",
        ctypes.get_last_error(),
    )
    return False


def native_overlay_message(message_pointer: int) -> tuple[bool, int] | None:
    """Force the entire overlay surface to remain a client-area hit target."""

    if sys.platform != "win32" or not message_pointer:
        return None
    message = ctypes.cast(message_pointer, ctypes.POINTER(_WindowsMessage)).contents
    if message.message == _WM_NCHITTEST:
        return True, _HTCLIENT
    return None


def native_backdrop_message(message_pointer: int) -> tuple[bool, int] | None:
    """Keep the backdrop hit-testable without activating it on mouse input."""

    if sys.platform != "win32" or not message_pointer:
        return None
    message = ctypes.cast(message_pointer, ctypes.POINTER(_WindowsMessage)).contents
    if message.message == _WM_NCHITTEST:
        return True, _HTCLIENT
    if message.message == _WM_MOUSEACTIVATE:
        return True, _MA_NOACTIVATE
    return None


def is_native_display_change(message_pointer: int) -> bool:
    """Return whether a native message requires geometry/z-order reconciliation."""

    if sys.platform != "win32" or not message_pointer:
        return False
    message = ctypes.cast(message_pointer, ctypes.POINTER(_WindowsMessage)).contents
    return message.message in {_WM_DISPLAYCHANGE, _WM_DPICHANGED}
