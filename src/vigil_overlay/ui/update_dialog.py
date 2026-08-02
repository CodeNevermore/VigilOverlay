"""Controller-friendly notification for an available Vigil Overlay release."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from vigil_overlay.core.updates import GITHUB_RELEASES_URL, AvailableUpdate
from vigil_overlay.ui.modal_guard import ModalActivationGuard, ModalInputSource

OpenUrlCallback = Callable[[QUrl], bool]


class UpdateAvailableDialog(QDialog):
    """Tell the user about a release and open the project's releases page."""

    def __init__(
        self,
        update: AvailableUpdate,
        parent: QWidget | None = None,
        *,
        open_url: OpenUrlCallback = QDesktopServices.openUrl,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("updateAvailableDialog")
        self.setWindowTitle("Vigil Overlay Update")
        self.setModal(True)
        self.setMinimumWidth(430)
        self._open_url = open_url
        self._guard = ModalActivationGuard(self)
        self._selected_index = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)

        title = QLabel("Update available", self)
        title.setObjectName("hotkeyEditorTitle")
        title.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(title)

        message = QLabel(
            f"Vigil Overlay {update.latest_version} is available. "
            f"You are currently using {update.current_version}.",
            self,
        )
        message.setObjectName("hotkeyEditorHelp")
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setWordWrap(True)
        layout.addWidget(message)

        if update.release_name not in {update.latest_version, ""}:
            release_name = QLabel(update.release_name, self)
            release_name.setTextFormat(Qt.TextFormat.PlainText)
            release_name.setWordWrap(True)
            layout.addWidget(release_name)

        self._error = QLabel("", self)
        self._error.setObjectName("hotkeyEditorError")
        self._error.setTextFormat(Qt.TextFormat.PlainText)
        self._error.setWordWrap(True)
        self._error.hide()
        layout.addWidget(self._error)

        buttons = QDialogButtonBox(self)
        buttons.setObjectName("hotkeyEditorButtons")
        update_button = buttons.addButton(
            "Update", QDialogButtonBox.ButtonRole.AcceptRole
        )
        later_button = buttons.addButton(
            "Later", QDialogButtonBox.ButtonRole.RejectRole
        )
        update_button.clicked.connect(self._open_releases)
        later_button.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self._controller_buttons: list[QPushButton] = [update_button, later_button]
        self._sync_controller_focus()

    def begin_controller_ownership(self, source: ModalInputSource) -> None:
        self._guard.begin(source)
        self._sync_controller_focus()

    def notify_controller_activation_released(self) -> None:
        self._guard.note_controller_activation_released()

    def handle_controller_command(self, command: object) -> bool:
        value = getattr(command, "value", command)
        if value in {"move_left", "move_up"}:
            self._selected_index = (self._selected_index - 1) % len(
                self._controller_buttons
            )
            self._sync_controller_focus()
            return True
        if value in {"move_right", "move_down"}:
            self._selected_index = (self._selected_index + 1) % len(
                self._controller_buttons
            )
            self._sync_controller_focus()
            return True
        if value == "back":
            self.reject()
            return True
        if value == "activate":
            if self._guard.accepts_activation():
                self._controller_buttons[self._selected_index].click()
            return True
        return True

    def _open_releases(self) -> None:
        if self._open_url(QUrl(GITHUB_RELEASES_URL)):
            self.accept()
            return
        self._error.setText("Windows could not open the Vigil Overlay releases page.")
        self._error.show()

    def _sync_controller_focus(self) -> None:
        self._controller_buttons[self._selected_index].setFocus(
            Qt.FocusReason.OtherFocusReason
        )


__all__ = ["UpdateAvailableDialog"]
