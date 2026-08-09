"""Shared host-owned dialog surface, styling roles, and controller behavior."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from vigil_overlay.ui.modal_guard import ModalActivationGuard, ModalInputSource


class VigilDialog(QDialog):
    """One themed, frameless surface for every host-owned modal dialog."""

    def __init__(
        self,
        window_title: str,
        parent: QWidget | None = None,
        *,
        width: int = 430,
    ) -> None:
        super().__init__(parent)
        if width < 320:
            raise ValueError("Vigil dialog width must be at least 320 pixels")
        self.setProperty("vigilDialog", True)
        self.setWindowTitle(window_title)
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(width)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        self.surface = QFrame(self)
        self.surface.setObjectName("vigilDialogSurface")
        self.surface.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer_layout.addWidget(self.surface)

        self.content_layout = QVBoxLayout(self.surface)
        self.content_layout.setContentsMargins(20, 18, 20, 20)
        self.content_layout.setSpacing(10)

    def add_title(self, text: str) -> QLabel:
        label = self._add_text_label("vigilDialogTitle", text)
        label.setProperty("dialogTextRole", "title")
        return label

    def add_message(self, text: str) -> QLabel:
        label = self._add_text_label("vigilDialogMessage", text)
        label.setProperty("dialogTextRole", "message")
        return label

    def add_detail(self, text: str) -> QLabel:
        label = self._add_text_label("vigilDialogDetail", text)
        label.setProperty("dialogTextRole", "detail")
        return label

    def add_error(self, text: str = "") -> QLabel:
        label = self._add_text_label("vigilDialogError", text)
        label.setProperty("dialogTextRole", "error")
        return label

    def create_button_box(self) -> QDialogButtonBox:
        box = QDialogButtonBox(self.surface)
        box.setObjectName("vigilDialogButtons")
        self.content_layout.addWidget(box)
        return box

    @staticmethod
    def style_button(button: QPushButton, *, kind: str = "standard") -> QPushButton:
        button.setProperty("vigilDialogButton", True)
        button.setProperty("dialogButtonKind", kind)
        return button

    def _add_text_label(self, object_name: str, text: str) -> QLabel:
        label = QLabel(text, self.surface)
        label.setObjectName(object_name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        self.content_layout.addWidget(label)
        return label


class ControllerVigilDialog(VigilDialog):
    """A Vigil dialog with shared controller focus and activation containment."""

    def __init__(
        self,
        window_title: str,
        parent: QWidget | None = None,
        *,
        width: int = 430,
    ) -> None:
        super().__init__(window_title, parent, width=width)
        self._guard = ModalActivationGuard(self)
        self._controller_buttons: list[QPushButton] = []
        self._controller_index = 0

    def set_controller_buttons(
        self,
        buttons: Sequence[QPushButton],
        *,
        selected_index: int = 0,
    ) -> None:
        self._controller_buttons = list(buttons)
        if not self._controller_buttons:
            self._controller_index = 0
            return
        self._controller_index = min(max(selected_index, 0), len(self._controller_buttons) - 1)
        self.sync_controller_focus()

    def begin_controller_ownership(self, source: ModalInputSource) -> None:
        self._guard.begin(source)
        self.sync_controller_focus()

    def notify_controller_activation_released(self) -> None:
        self._guard.note_controller_activation_released()

    def handle_controller_command(self, command: object) -> bool:
        value = getattr(command, "value", command)
        if not self._controller_buttons:
            return True
        if value in {"move_left", "move_up"}:
            self._controller_index = (self._controller_index - 1) % len(self._controller_buttons)
            self.sync_controller_focus()
            return True
        if value in {"move_right", "move_down"}:
            self._controller_index = (self._controller_index + 1) % len(self._controller_buttons)
            self.sync_controller_focus()
            return True
        if value == "back":
            self.controller_back()
            return True
        if value == "activate":
            if self._guard.accepts_activation():
                self.activate_controller_selection()
            return True
        return True

    def controller_back(self) -> None:
        self.reject()

    def activate_controller_selection(self) -> None:
        if self._controller_buttons:
            self._controller_buttons[self._controller_index].click()

    def sync_controller_focus(self) -> None:
        if not self._controller_buttons:
            return
        self._controller_buttons[self._controller_index].setFocus(Qt.FocusReason.OtherFocusReason)


class VigilMessageDialog(ControllerVigilDialog):
    """Simple controller-dismissible host message using the shared dialog surface."""

    def __init__(
        self,
        title: str,
        message: str,
        parent: QWidget | None = None,
        *,
        width: int = 430,
        button_text: str = "OK",
    ) -> None:
        super().__init__(title, parent, width=width)
        self.setObjectName("vigilMessageDialog")
        self.add_title(title)
        self.add_message(message)
        buttons = self.create_button_box()
        dismiss = buttons.addButton(button_text, QDialogButtonBox.ButtonRole.AcceptRole)
        self.style_button(dismiss, kind="primary")
        dismiss.clicked.connect(self.accept)
        self.set_controller_buttons((dismiss,))


__all__ = ["ControllerVigilDialog", "VigilDialog", "VigilMessageDialog"]
