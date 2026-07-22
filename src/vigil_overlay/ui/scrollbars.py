"""Shared Vigil scrollbar primitives for host-owned scroll surfaces."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QShowEvent
from PySide6.QtWidgets import QApplication, QScrollArea, QScrollBar, QWidget


def ensure_controller_target_visible(
    scroll_area: QScrollArea,
    target: QWidget,
    *,
    x_margin: int = 18,
    y_margin: int = 18,
) -> None:
    """Keep one controller-highlighted target visible in a Vigil scroll surface.

    Qt can update layout geometry one event-loop turn after a navigation state change,
    especially for freshly shown popup lists. Reveal the target immediately for normal
    movement, then repeat once after pending layout work so every host-owned scroller
    follows the visible controller highlight deterministically.
    """

    scroll_area.ensureWidgetVisible(target, x_margin, y_margin)
    QTimer.singleShot(
        0,
        lambda area=scroll_area, widget=target: area.ensureWidgetVisible(
            widget,
            x_margin,
            y_margin,
        ),
    )


class VigilVerticalScrollBar(QScrollBar):
    """Host-owned vertical scrollbar with deterministic visibility and rounded painting.

    All Vigil-owned vertical scroll surfaces should use this control so built-in
    widgets, dropdowns, and host-wrapped third-party widgets share the same
    visual treatment and overflow behavior.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        object_name: str = "vigilVerticalScrollBar",
    ) -> None:
        super().__init__(Qt.Orientation.Vertical, parent)
        self.setObjectName(object_name)
        self.setProperty("hostOwnedVigilScrollbar", True)
        # Retain the earlier property name for host-wrapped widget compatibility.
        self.setProperty("hostOwnedWidgetScrollbar", True)
        self.setProperty("customRoundedThumb", True)
        self.setFixedWidth(18)
        # Keep Qt's native scrollbar geometry/hit testing, but own all visible
        # painting so Windows styles cannot flatten the thumb or hide it until hover.
        self.setStyleSheet("""
            QScrollBar:vertical {
                background: transparent;
                width: 18px;
                margin: 6px 2px 6px 2px;
                border: none;
            }
            QScrollBar::handle:vertical {
                background: transparent;
                min-height: 56px;
                margin: 0 2px;
                border: none;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                background: transparent;
                height: 0px;
                border: none;
            }
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                background: transparent;
            }
            """)
        self.rangeChanged.connect(self._schedule_visibility_sync)
        super().setVisible(False)

    @property
    def has_overflow(self) -> bool:
        """Whether the range represents meaningful user-scrollable overflow."""

        # Qt can report a tiny 1-2 px range from frame/layout rounding even when
        # content visually fits. Ignore those phantom ranges.
        return (self.maximum() - self.minimum()) > 4

    def setVisible(self, visible: bool) -> None:
        """Never allow a scroll area/platform style to show an empty scrollbar."""

        super().setVisible(bool(visible and self.has_overflow))

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self.has_overflow:
            QTimer.singleShot(0, self._sync_visibility)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        if not self.has_overflow:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        track_color, handle_color, hover_color, pressed_color = self._colors()
        track_rect = QRectF(self.rect()).adjusted(5.0, 6.0, -5.0, -6.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(
            track_rect,
            track_rect.width() / 2.0,
            track_rect.width() / 2.0,
        )

        minimum = self.minimum()
        maximum = self.maximum()
        page_step = max(self.pageStep(), 1)
        track_height = max(track_rect.height(), 1.0)
        range_span = max(maximum - minimum, 1)
        handle_height = max(56.0, track_height * page_step / (range_span + page_step))
        handle_height = min(handle_height, track_height)
        travel = max(track_height - handle_height, 0.0)
        ratio = (self.value() - minimum) / range_span
        handle_top = track_rect.top() + travel * min(max(ratio, 0.0), 1.0)
        handle_rect = QRectF(
            track_rect.left(),
            handle_top,
            track_rect.width(),
            handle_height,
        )

        if self.isSliderDown():
            color = pressed_color
        elif self.underMouse():
            color = hover_color
        else:
            color = handle_color
        painter.setBrush(color)
        radius = min(handle_rect.width() / 2.0, handle_rect.height() / 2.0)
        painter.drawRoundedRect(handle_rect, radius, radius)

    def _schedule_visibility_sync(self, minimum: int, maximum: int) -> None:
        del minimum, maximum
        QTimer.singleShot(0, self._sync_visibility)

    def _sync_visibility(self) -> None:
        super().setVisible(self.has_overflow)
        self.update()

    @staticmethod
    def _colors() -> tuple[QColor, QColor, QColor, QColor]:
        application = QApplication.instance()
        theme = (
            application.property("vigilResolvedTheme")
            if application is not None
            else None
        )
        if theme == "light":
            return (
                QColor(72, 79, 89, 46),
                QColor("#7d8793"),
                QColor("#626b76"),
                QColor("#4f5761"),
            )
        return (
            QColor(225, 228, 233, 58),
            QColor("#d4d8de"),
            QColor("#eef0f3"),
            QColor("#ffffff"),
        )
