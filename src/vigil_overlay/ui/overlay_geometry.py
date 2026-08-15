"""Overlay screen placement, panel sizing, and geometry persistence ownership."""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QPoint, QRect, QTimer
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QFrame, QLayout, QWidget

from vigil_overlay.core.config import AppConfig
from vigil_overlay.core.errors import VigilOverlayError

_LOGGER = logging.getLogger("vigil_overlay")
_MIN_COMPACT_PANEL_HEIGHT = 430
_PANEL_BOTTOM_SAFETY_MARGIN = 28
PersistConfig = Callable[[AppConfig], None]
DimensionProvider = Callable[[], int]
GeometryCallback = Callable[[], None]


class OverlayGeometryController(QObject):
    """Own geometry calculation, display settling, and persisted placement."""

    def __init__(
        self,
        host: QWidget,
        *,
        backdrop: QWidget,
        compact_panel: QFrame,
        root_layout: QLayout,
        config: AppConfig,
        persist_config: PersistConfig,
        panel_width: DimensionProvider,
        natural_panel_height: DimensionProvider,
        refresh_display_values: GeometryCallback,
        configure_native_window: GeometryCallback,
        reassert_topmost: GeometryCallback,
    ) -> None:
        super().__init__(host)
        self._host = host
        self._backdrop = backdrop
        self._compact_panel = compact_panel
        self._root_layout = root_layout
        self._config = config
        self._persist_config = persist_config
        self._panel_width = panel_width
        self._natural_panel_height = natural_panel_height
        self._refresh_display_values = refresh_display_values
        self._configure_native_window = configure_native_window
        self._reassert_topmost = reassert_topmost

        self.persist_timer = QTimer(self)
        self.persist_timer.setSingleShot(True)
        self.persist_timer.setInterval(250)
        self.persist_timer.timeout.connect(self.persist)
        self.display_settle_timer = QTimer(self)
        self.display_settle_timer.setSingleShot(True)
        self.display_settle_timer.setInterval(350)
        self.display_settle_timer.timeout.connect(self.settle_display_geometry)

    def restore(self) -> None:
        """Restore the overlay to its selected full-screen monitor."""

        self._host.setGeometry(self.target_screen_geometry())

    def reset_position(self) -> None:
        """Move to the primary monitor and persist that placement."""

        self._host.setGeometry(self.target_screen_geometry(force_primary=True))
        self.persist()

    def target_screen_geometry(self, *, force_primary: bool = False) -> QRect:
        """Choose the full monitor containing saved placement or current context."""

        screens = QGuiApplication.screens()
        primary = QGuiApplication.primaryScreen()
        if force_primary and primary is not None:
            return primary.geometry()

        settings = self._config.window
        if settings.x is not None and settings.y is not None:
            center = QPoint(
                settings.x + max(settings.width, 1) // 2,
                settings.y + max(settings.height, 1) // 2,
            )
            for screen in screens:
                if screen.geometry().contains(center):
                    return screen.geometry()

        current_screen = self._host.screen()
        if current_screen is not None and current_screen in screens:
            return current_screen.geometry()

        if QGuiApplication.platformName() not in {"offscreen", "minimal"}:
            cursor_screen = QGuiApplication.screenAt(QCursor.pos())
            if cursor_screen is not None:
                return cursor_screen.geometry()

        if primary is not None:
            return primary.geometry()
        return QRect(0, 0, 1280, 720)

    def schedule_panel_refresh(self) -> None:
        """Apply adaptive panel sizing after the current UI event finishes."""

        QTimer.singleShot(0, self.apply_panel_geometry)

    def apply_panel_geometry(self) -> None:
        """Fit the active widget naturally, capped to the monitor height."""

        self._compact_panel.setFixedWidth(self._panel_width())
        margins = self._root_layout.contentsMargins()
        panel_top = self._compact_panel.y()
        if panel_top <= 0:
            panel_top = margins.top()
        bottom_margin = max(margins.bottom(), _PANEL_BOTTOM_SAFETY_MARGIN)
        available_height = max(self._host.height() - panel_top - bottom_margin, 1)
        natural_height = max(
            self._natural_panel_height(),
            _MIN_COMPACT_PANEL_HEIGHT,
        )
        self._compact_panel.setFixedHeight(min(natural_height, available_height))

    def window_geometry_changed(self) -> None:
        """Keep the backdrop aligned and debounce configuration persistence."""

        self._backdrop.setGeometry(self._host.geometry())
        self.schedule_persist()

    def schedule_persist(self) -> None:
        """Debounce persistence while the overlay is visible."""

        if self._host.isVisible():
            self.persist_timer.start()

    def handle_display_change(self) -> None:
        """Apply an immediate display change and schedule one settling pass."""

        if not self._host.isVisible():
            return
        self._apply_display_geometry()
        self.display_settle_timer.start()

    def settle_display_geometry(self) -> None:
        """Reapply display geometry after Windows finishes monitor transitions."""

        if not self._host.isVisible():
            return
        self._apply_display_geometry()

    def _apply_display_geometry(self) -> None:
        target = self.target_screen_geometry()
        self._backdrop.setGeometry(target)
        self._host.setGeometry(target)
        self.apply_panel_geometry()
        self._refresh_display_values()
        self._configure_native_window()
        self._reassert_topmost()

    def persist(self) -> None:
        """Persist changed geometry and roll back in-memory state after failure."""

        geometry = self._host.geometry()
        settings = self._config.window
        current_values = (
            settings.x,
            settings.y,
            settings.width,
            settings.height,
        )
        new_values = (
            geometry.x(),
            geometry.y(),
            geometry.width(),
            geometry.height(),
        )
        if current_values == new_values:
            return

        settings.x, settings.y, settings.width, settings.height = new_values
        try:
            self._persist_config(self._config)
        except (OSError, VigilOverlayError):
            settings.x, settings.y, settings.width, settings.height = current_values
            _LOGGER.exception("Could not persist overlay geometry")
            return
        _LOGGER.debug("Persisted overlay screen geometry: %s", new_values)


__all__ = ["OverlayGeometryController"]
