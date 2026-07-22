"""Shared first-party UI primitives used across Vigil widget surfaces."""

from __future__ import annotations

from enum import StrEnum

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainter, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import QCheckBox, QPushButton, QWidget


class SelectorToggleAction(StrEnum):
    """Host-owned lifecycle action for a selector anchor activation."""

    OPEN = "open"
    CLOSE = "close"
    SWITCH = "switch"


def selector_toggle_action(
    open_selector_id: str | None,
    requested_selector_id: str,
) -> SelectorToggleAction:
    """Return the canonical action for activating a selector anchor."""

    if open_selector_id is None:
        return SelectorToggleAction.OPEN
    if open_selector_id == requested_selector_id:
        return SelectorToggleAction.CLOSE
    return SelectorToggleAction.SWITCH


def repolish_widget(widget: QWidget) -> None:
    """Refresh Qt style state after dynamic properties change."""

    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


class VigilToggleSwitch(QCheckBox):
    """Non-focusable, host-owned toggle indicator with one canonical geometry."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("vigilToggleSwitch")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedSize(48, 26)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        palette = self.palette()
        group = (
            QPalette.ColorGroup.Active
            if self.isEnabled()
            else QPalette.ColorGroup.Disabled
        )
        track_role = (
            QPalette.ColorRole.Highlight if self.isChecked() else QPalette.ColorRole.Mid
        )
        thumb_role = (
            QPalette.ColorRole.HighlightedText
            if self.isChecked()
            else QPalette.ColorRole.ButtonText
        )
        track = palette.color(group, track_role)
        thumb = palette.color(group, thumb_role)

        track_rect = QRectF(1.0, 2.0, 46.0, 22.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(track_rect, 11.0, 11.0)

        diameter = 18.0
        x = 27.0 if self.isChecked() else 3.0
        painter.setBrush(thumb)
        painter.drawEllipse(QRectF(x, 4.0, diameter, diameter))


class VigilSelectorButton(QPushButton):
    """Canonical selector anchor with one host-owned open/close state."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("selectorOpen", False)
        self.setAccessibleDescription("Dropdown closed. Activate to open.")

    @property
    def selector_open(self) -> bool:
        return bool(self.property("selectorOpen"))

    def set_selector_open(self, opened: bool) -> None:
        opened = bool(opened)
        if self.selector_open == opened:
            return
        self.setProperty("selectorOpen", opened)
        self.setAccessibleDescription(
            "Dropdown open. Activate to close."
            if opened
            else "Dropdown closed. Activate to open."
        )
        repolish_widget(self)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(self.palette().buttonText().color())
        pen.setWidthF(2.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        center_x = self.width() - 25.0
        center_y = self.height() / 2.0
        if self.selector_open:
            points = [
                QPointF(center_x - 7.0, center_y + 4.0),
                QPointF(center_x, center_y - 3.0),
                QPointF(center_x + 7.0, center_y + 4.0),
            ]
        else:
            points = [
                QPointF(center_x - 7.0, center_y - 4.0),
                QPointF(center_x, center_y + 3.0),
                QPointF(center_x + 7.0, center_y - 4.0),
            ]
        painter.drawPolyline(points)


__all__ = [
    "SelectorToggleAction",
    "VigilSelectorButton",
    "VigilToggleSwitch",
    "repolish_widget",
    "selector_toggle_action",
]
