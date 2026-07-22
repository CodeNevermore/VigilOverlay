"""Opaque native backdrop used to dim and block the selected monitor."""

from __future__ import annotations

from typing import cast

from PySide6.QtCore import QByteArray, QEvent, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QWheelEvent
from PySide6.QtWidgets import QWidget

from vigil_overlay.ui.windows_windowing import (
    configure_native_backdrop_window,
    enforce_native_topmost,
    native_backdrop_message,
)


class DimBackdropWindow(QWidget):
    """A real opaque surface with constant window opacity for reliable hit testing."""

    BACKDROP_COLOR = QColor(31, 34, 39, 255)
    BACKDROP_OPACITY = 0.50

    def __init__(self) -> None:
        super().__init__(None)
        self.setObjectName("dimBackdropRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWindowTitle("Vigil Overlay Backdrop")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowOpacity(self.BACKDROP_OPACITY)

    @property
    def backdrop_color(self) -> QColor:
        return self.BACKDROP_COLOR

    def show_backdrop(self, geometry: QRect) -> None:
        self.setGeometry(geometry)
        self.show()
        self.configure_native_state()

    def configure_native_state(self) -> None:
        if not self.isVisible():
            return
        configure_native_backdrop_window(int(self.winId()))

    def reassert_topmost(self) -> None:
        if not self.isVisible():
            return
        enforce_native_topmost(int(self.winId()))

    def event(self, event: QEvent) -> bool:
        if event.type() is QEvent.Type.WinIdChange and self.isVisible():
            QTimer.singleShot(0, self.configure_native_state)
        return super().event(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.backdrop_color)
        painter.end()
        super().paintEvent(event)

    def nativeEvent(
        self,
        event_type: QByteArray | bytes | bytearray | memoryview[int],
        message: int,
    ) -> tuple[bool, int]:
        result = native_backdrop_message(int(message))
        if result is not None:
            return result
        return cast(tuple[bool, int], super().nativeEvent(event_type, message))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.accept()
