"""Window geometry normalization helpers."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QRect

MINIMUM_VISIBLE_PIXELS = 64
WINDOW_MARGIN = 12


def normalized_window_rect(
    requested: QRect,
    available_screens: Sequence[QRect],
    fallback_screen: QRect,
) -> QRect:
    """Return a usable geometry contained by an available screen.

    Saved coordinates may point to a disconnected monitor. The result keeps the
    requested size where possible and moves the window onto the closest useful
    screen, while enforcing a small margin around the desktop work area.
    """

    screens = [screen for screen in available_screens if screen.isValid()]
    target_screen = _select_target_screen(requested, screens, fallback_screen)

    maximum_width = max(1, target_screen.width() - (WINDOW_MARGIN * 2))
    maximum_height = max(1, target_screen.height() - (WINDOW_MARGIN * 2))
    width = min(max(1, requested.width()), maximum_width)
    height = min(max(1, requested.height()), maximum_height)

    minimum_x = target_screen.left() + WINDOW_MARGIN
    minimum_y = target_screen.top() + WINDOW_MARGIN
    maximum_x = target_screen.right() - WINDOW_MARGIN - width + 1
    maximum_y = target_screen.bottom() - WINDOW_MARGIN - height + 1

    if maximum_x < minimum_x:
        maximum_x = minimum_x
    if maximum_y < minimum_y:
        maximum_y = minimum_y

    x = min(max(requested.x(), minimum_x), maximum_x)
    y = min(max(requested.y(), minimum_y), maximum_y)
    return QRect(x, y, width, height)


def _select_target_screen(
    requested: QRect, screens: Sequence[QRect], fallback: QRect
) -> QRect:
    requested_center = requested.center()
    for screen in screens:
        if screen.contains(requested_center):
            return screen

    for screen in screens:
        intersection = requested.intersected(screen)
        if (
            intersection.width() >= MINIMUM_VISIBLE_PIXELS
            and intersection.height() >= MINIMUM_VISIBLE_PIXELS
        ):
            return screen

    return fallback
