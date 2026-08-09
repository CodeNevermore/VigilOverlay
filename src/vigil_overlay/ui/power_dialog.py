"""Controller-owned power menu with a required confirmation step."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QPushButton, QWidget

from vigil_overlay.services.power_controls import PowerAction, PowerCapabilities
from vigil_overlay.ui.dialog_surface import ControllerVigilDialog
from vigil_overlay.ui.modal_guard import ModalInputSource

PowerActionCallback = Callable[[PowerAction], tuple[bool, str]]


class PowerMenuDialog(ControllerVigilDialog):
    """Select and confirm one supported power action."""

    def __init__(
        self,
        capabilities: PowerCapabilities,
        execute_callback: PowerActionCallback,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Power", parent, width=344)
        self.setObjectName("powerMenuDialog")
        self._execute_callback = execute_callback
        self._actions = capabilities.actions()
        self._buttons: list[QPushButton] = []
        self._pending_action: PowerAction | None = None

        self._layout = self.content_layout
        self._title = self.add_title("Power")
        self._message = self.add_message("Choose a power option.")
        self._error = self.add_error()
        self._error.hide()
        self._show_action_buttons()

    def controller_back(self) -> None:
        if self._pending_action is None:
            self.reject()
        else:
            self._show_action_buttons()

    def _show_action_buttons(self) -> None:
        self._clear_buttons()
        self._pending_action = None
        self._title.setText("Power")
        self._message.setText("Choose a power option.")
        self._error.hide()
        for action in self._actions:
            button = QPushButton(action.label, self.surface)
            button.setObjectName("powerMenuAction")
            button.setProperty("powerAction", action.value)
            self.style_button(button, kind="row")
            button.clicked.connect(
                lambda checked=False, selected=action: self._request_confirmation(selected)
            )
            self._layout.addWidget(button)
            self._buttons.append(button)
        cancel = QPushButton("Cancel", self.surface)
        cancel.setObjectName("powerMenuCancel")
        self.style_button(cancel, kind="row")
        cancel.clicked.connect(self.reject)
        self._layout.addWidget(cancel)
        self._buttons.append(cancel)
        self.set_controller_buttons(self._buttons)

    def _request_confirmation(self, action: PowerAction) -> None:
        self._clear_buttons()
        self._pending_action = action
        self._title.setText(f"Confirm {action.label}")
        self._message.setText(f"Are you sure you want to {action.label.casefold()} this PC?")
        confirm = QPushButton(action.label, self.surface)
        confirm.setObjectName("powerMenuConfirm")
        confirm.setProperty("powerAction", action.value)
        self.style_button(confirm, kind="row")
        cancel = QPushButton("Cancel", self.surface)
        cancel.setObjectName("powerMenuCancel")
        self.style_button(cancel, kind="row")
        confirm.clicked.connect(self._execute)
        cancel.clicked.connect(self._show_action_buttons)
        self._layout.addWidget(confirm)
        self._layout.addWidget(cancel)
        self._buttons = [confirm, cancel]
        self.set_controller_buttons(self._buttons, selected_index=1)
        self._guard.begin(ModalInputSource.UNKNOWN)

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
        self.set_controller_buttons(())


__all__ = ["PowerActionCallback", "PowerMenuDialog"]
