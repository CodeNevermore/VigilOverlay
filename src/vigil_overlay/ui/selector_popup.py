"""Shared host-owned popup used by first-party selector controls."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QPushButton, QScrollArea, QVBoxLayout, QWidget

from vigil_overlay.ui.controls import repolish_widget
from vigil_overlay.ui.scrollbars import VigilVerticalScrollBar, ensure_controller_target_visible

_OPTION_HEIGHT = 46
_POPUP_VERTICAL_PADDING = 12
_POPUP_MIN_HEIGHT = 78
_POPUP_MAX_HEIGHT = 230
_POPUP_EDGE_MARGIN = 8
_POPUP_ANCHOR_GAP = 4


class SelectorPopup(QFrame):
    """One canonical selector popup with controller focus and anchored placement."""

    def __init__(
        self,
        parent: QWidget,
        *,
        anchor: QWidget,
        option_labels: Sequence[str],
        selected_index: int,
        object_prefix: str,
        option_selected: Callable[[int], None],
    ) -> None:
        if not option_labels:
            raise ValueError("selector popup requires at least one option")
        super().__init__(parent)
        self._anchor = anchor
        self._option_selected = option_selected
        self._selected_index = selected_index % len(option_labels)
        self._buttons: list[QPushButton] = []

        self.setObjectName(f"{object_prefix}DropdownPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        popup_layout = QVBoxLayout(self)
        popup_layout.setContentsMargins(6, 6, 6, 6)
        popup_layout.setSpacing(3)

        self._scroll = QScrollArea(self)
        self._scroll.setObjectName(f"{object_prefix}DropdownScroll")
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBar(
            VigilVerticalScrollBar(
                self._scroll,
                object_name=f"{object_prefix}DropdownVerticalScrollBar",
            )
        )

        content = QWidget(self._scroll)
        content.setObjectName(f"{object_prefix}DropdownContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(3)
        for index, label in enumerate(option_labels):
            button = QPushButton(label, content)
            button.setObjectName(f"{object_prefix}DropdownOption")
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(
                lambda checked=False, option_index=index: self._option_selected(
                    option_index
                )
            )
            self._buttons.append(button)
            content_layout.addWidget(button)
        content_layout.addStretch(1)
        self._scroll.setWidget(content)
        popup_layout.addWidget(self._scroll)

    @property
    def selected_index(self) -> int:
        return self._selected_index

    def show_anchored(self) -> None:
        """Show, place, and visually focus the selected option."""

        self.show()
        self.raise_()
        self.reposition()
        self._sync_focus()

    def reposition(self) -> None:
        """Keep the popup aligned to its anchor within the parent widget."""

        parent = self.parentWidget()
        if parent is None:
            return
        top_left = self._anchor.mapTo(parent, self._anchor.rect().bottomLeft())
        height = min(
            _POPUP_MAX_HEIGHT,
            max(
                _POPUP_MIN_HEIGHT,
                len(self._buttons) * _OPTION_HEIGHT + _POPUP_VERTICAL_PADDING,
            ),
        )
        y = top_left.y() + _POPUP_ANCHOR_GAP
        if y + height > parent.height() - _POPUP_EDGE_MARGIN:
            anchor_top = self._anchor.mapTo(parent, self._anchor.rect().topLeft()).y()
            y = max(
                _POPUP_EDGE_MARGIN,
                anchor_top - height - _POPUP_ANCHOR_GAP,
            )
        self.setGeometry(top_left.x(), y, self._anchor.width(), height)

    def move_selection(self, delta: int) -> bool:
        """Move controller focus by *delta*, wrapping through all options."""

        if not self._buttons or delta == 0:
            return False
        self._selected_index = (self._selected_index + delta) % len(self._buttons)
        self._sync_focus()
        return True

    def activate_selection(self) -> bool:
        """Invoke the selected option through the owner's callback."""

        if not self._buttons:
            return False
        self._option_selected(self._selected_index)
        return True

    def dispose(self) -> None:
        """Remove the popup without relying on a close event side effect."""

        self.hide()
        self.deleteLater()

    def _sync_focus(self) -> None:
        for index, button in enumerate(self._buttons):
            button.setProperty("navigationFocus", index == self._selected_index)
            repolish_widget(button)
        selected = self._buttons[self._selected_index]
        selected.ensurePolished()
        ensure_controller_target_visible(
            self._scroll,
            selected,
            x_margin=8,
            y_margin=8,
        )


__all__ = ["SelectorPopup"]
