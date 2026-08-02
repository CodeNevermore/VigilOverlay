"""Controller-owned power menu with a required confirmation step."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout, QWidget

from vigil_overlay.services.power_controls import PowerAction, PowerCapabilities
from vigil_overlay.ui.modal_guard import ModalActivationGuard, ModalInputSource

PowerActionCallback = Callable[[PowerAction], tuple[bool, str]]


class PowerMenuDialog(QDialog):
    """Select and confirm one supported power action."""

    def __init__(
        self,
        capabilities: PowerCapabilities,
        execute_callback: PowerActionCallback,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("powerMenuDialog")
        self.setWindowTitle("Power")
        self.setModal(True)
        self.setMinimumWidth(390)
        self._execute_callback = execute_callback
        self._actions = capabilities.actions()
        self._buttons: list[QPushButton] = []
        self._selected_index = 0
        self._pending_action: PowerAction | None = None
        self._guard = ModalActivationGuard(self)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(22, 20, 22, 20)
        self._layout.setSpacing(10)
        self._title = QLabel("Power", self)
        self._title.setObjectName("hotkeyEditorTitle")
        self._layout.addWidget(self._title)
        self._message = QLabel("Choose a power option.", self)
        self._message.setWordWrap(True)
        self._layout.addWidget(self._message)
        self._error = QLabel("", self)
        self._error.setWordWrap(True)
        self._error.hide()
        self._layout.addWidget(self._error)
        self._show_action_buttons()

    def begin_controller_ownership(self, source: ModalInputSource) -> None:
        self._guard.begin(source)
        self._sync_focus()

    def notify_controller_activation_released(self) -> None:
        self._guard.note_controller_activation_released()

    def handle_controller_command(self, command: object) -> bool:
        value = getattr(command, "value", command)
        if value in {"move_left", "move_up"}:
            self._selected_index = (self._selected_index - 1) % len(self._buttons)
            self._sync_focus()
            return True
        if value in {"move_right", "move_down"}:
            self._selected_index = (self._selected_index + 1) % len(self._buttons)
            self._sync_focus()
            return True
        if value == "back":
            if self._pending_action is None:
                self.reject()
            else:
                self._show_action_buttons()
            return True
        if value == "activate":
            if self._guard.accepts_activation():
                self._buttons[self._selected_index].click()
            return True
        return True

    def _show_action_buttons(self) -> None:
        self._clear_buttons()
        self._pending_action = None
        self._title.setText("Power")
        self._message.setText("Choose a power option.")
        self._error.hide()
        for action in self._actions:
            button = QPushButton(action.label, self)
            button.clicked.connect(
                lambda checked=False, selected=action: self._request_confirmation(
                    selected
                )
            )
            self._layout.addWidget(button)
            self._buttons.append(button)
        cancel = QPushButton("Cancel", self)
        cancel.clicked.connect(self.reject)
        self._layout.addWidget(cancel)
        self._buttons.append(cancel)
        self._selected_index = 0
        self._sync_focus()

    def _request_confirmation(self, action: PowerAction) -> None:
        self._clear_buttons()
        self._pending_action = action
        self._title.setText(f"Confirm {action.label}")
        self._message.setText(
            f"Are you sure you want to {action.label.casefold()} this PC?"
        )
        confirm = QPushButton(action.label, self)
        cancel = QPushButton("Cancel", self)
        confirm.clicked.connect(self._execute)
        cancel.clicked.connect(self._show_action_buttons)
        self._layout.addWidget(confirm)
        self._layout.addWidget(cancel)
        self._buttons = [confirm, cancel]
        self._selected_index = 1
        self._guard.begin(ModalInputSource.UNKNOWN)
        self._sync_focus()

    def _execute(self) -> None:
        action = self._pending_action
        if action is None:
            return
        success, detail = self._execute_callback(action)
        if success:
            self.accept()
            return
        self._error.setText(detail)
        self._error.show()

    def _clear_buttons(self) -> None:
        for button in self._buttons:
            self._layout.removeWidget(button)
            button.deleteLater()
        self._buttons.clear()

    def _sync_focus(self) -> None:
        if not self._buttons:
            return
        self._buttons[self._selected_index].setFocus(Qt.FocusReason.OtherFocusReason)


__all__ = ["PowerActionCallback", "PowerMenuDialog"]
