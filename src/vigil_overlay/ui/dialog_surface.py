"""Shared host-owned dialog surface, styling roles, and controller behavior."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from vigil_overlay.ui.controls import controller_target_available
from vigil_overlay.ui.modal_guard import ModalActivationGuard, ModalInputSource
from vigil_overlay.ui.scrollbars import VigilVerticalScrollBar, ensure_controller_target_visible


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

    def create_scroll_area(
        self, *, minimum_height: int = 220, maximum_height: int = 300
    ) -> QScrollArea:
        """Use the host scrollbar and overflow rules for a bounded modal list."""

        scroll = QScrollArea(self.surface)
        scroll.setObjectName("vigilDialogScroll")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBar(VigilVerticalScrollBar(scroll))
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setMinimumHeight(minimum_height)
        scroll.setMaximumHeight(maximum_height)
        self.content_layout.addWidget(scroll)
        return scroll

    @staticmethod
    def create_scroll_content(scroll: QScrollArea) -> tuple[QWidget, QVBoxLayout]:
        content = QWidget(scroll)
        content.setObjectName("vigilDialogScrollContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(6)
        # Preserve each row's size hint and let the viewport scroll the overflow.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        return content, layout

    @staticmethod
    def style_detail_label(label: QLabel) -> QLabel:
        label.setObjectName("vigilDialogDetail")
        label.setProperty("dialogTextRole", "detail")
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        return label

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
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
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
        for button in self._controller_buttons:
            button.removeEventFilter(self)
        self._controller_buttons = list(buttons)
        for button in self._controller_buttons:
            button.installEventFilter(self)
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
        if value == "back":
            self.controller_back()
            return True
        if value in {"move_left", "move_up"}:
            self._move_controller_focus(-1)
            return True
        if value in {"move_right", "move_down"}:
            self._move_controller_focus(1)
            return True
        if value == "activate":
            if self._guard.accepts_activation():
                self.activate_controller_selection()
            return True
        return True

    def controller_back(self) -> None:
        self.reject()

    def activate_controller_selection(self) -> None:
        if self._normalize_controller_selection():
            self._controller_buttons[self._controller_index].click()

    def sync_controller_focus(self) -> None:
        if not self._normalize_controller_selection():
            return
        button = self._controller_buttons[self._controller_index]
        button.setFocus(Qt.FocusReason.OtherFocusReason)
        self._reveal_button(button)

    def _eligible_controller_button(self, button: QPushButton) -> bool:
        return controller_target_available(button, self)

    def _normalize_controller_selection(self) -> bool:
        count = len(self._controller_buttons)
        for offset in range(count):
            index = (self._controller_index + offset) % count
            if self._eligible_controller_button(self._controller_buttons[index]):
                self._controller_index = index
                return True
        return False

    def _move_controller_focus(self, delta: int) -> None:
        count = len(self._controller_buttons)
        for offset in range(1, count + 1):
            index = (self._controller_index + delta * offset) % count
            if self._eligible_controller_button(self._controller_buttons[index]):
                self._controller_index = index
                self.sync_controller_focus()
                return

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched in self._controller_buttons and event.type() in {
            QEvent.Type.EnabledChange,
            QEvent.Type.Hide,
            QEvent.Type.Show,
        }:
            previous_index = self._controller_index
            focused = self.focusWidget()
            if (
                self._normalize_controller_selection()
                and self._controller_index != previous_index
                and (focused is None or focused in self._controller_buttons)
            ):
                self.sync_controller_focus()
        if event.type() == QEvent.Type.FocusIn and watched in self._controller_buttons:
            self._controller_index = self._controller_buttons.index(watched)
            self._reveal_button(self._controller_buttons[self._controller_index])
        if (
            event.type() == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
            and event.key()
            in {
                Qt.Key.Key_Up,
                Qt.Key.Key_Down,
                Qt.Key.Key_Left,
                Qt.Key.Key_Right,
                Qt.Key.Key_Return,
                Qt.Key.Key_Enter,
                Qt.Key.Key_Space,
            }
        ):
            self.keyPressEvent(event)
            return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.controller_back()
            event.accept()
            return
        commands: dict[int, str] = {
            Qt.Key.Key_Up: "move_up",
            Qt.Key.Key_Down: "move_down",
            Qt.Key.Key_Left: "move_left",
            Qt.Key.Key_Right: "move_right",
        }
        command = commands.get(event.key())
        if command is not None:
            self.handle_controller_command(command)
            event.accept()
            return
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            self.handle_controller_command("activate")
            event.accept()
            return
        super().keyPressEvent(event)

    def _reveal_button(self, button: QPushButton) -> None:
        parent = button.parentWidget()
        while parent is not None and parent is not self:
            if isinstance(parent, QScrollArea):
                ensure_controller_target_visible(parent, button)
                return
            parent = parent.parentWidget()


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
