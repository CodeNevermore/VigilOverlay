"""Controller-friendly notification for an available Vigil Overlay release."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QWidget,
)

from vigil_overlay.core.updates import GITHUB_RELEASES_URL, AvailableUpdate
from vigil_overlay.ui.dialog_surface import ControllerVigilDialog

OpenUrlCallback = Callable[[QUrl], bool]


class UpdateAvailableDialog(ControllerVigilDialog):
    """Tell the user about a release and open the project's releases page."""

    def __init__(
        self,
        update: AvailableUpdate,
        parent: QWidget | None = None,
        *,
        open_url: OpenUrlCallback = QDesktopServices.openUrl,
    ) -> None:
        super().__init__("Vigil Overlay Update", parent, width=430)
        self.setObjectName("updateAvailableDialog")
        self._open_url = open_url

        self.add_title("Update available")
        self.add_message(
            f"Vigil Overlay {update.latest_version} is available. "
            f"You are currently using {update.current_version}."
        )

        if update.release_name not in {update.latest_version, ""}:
            self.add_detail(update.release_name)

        self._error = self.add_error()
        self._error.hide()

        buttons = self.create_button_box()
        update_button = buttons.addButton("Update", QDialogButtonBox.ButtonRole.AcceptRole)
        later_button = buttons.addButton("Later", QDialogButtonBox.ButtonRole.RejectRole)
        self.style_button(update_button, kind="primary")
        self.style_button(later_button)
        update_button.clicked.connect(self._open_releases)
        later_button.clicked.connect(self.reject)
        self.set_controller_buttons((update_button, later_button))

    def _open_releases(self) -> None:
        if self._open_url(QUrl(GITHUB_RELEASES_URL)):
            self.accept()
            return
        self._error.setText("Windows could not open the Vigil Overlay releases page.")
        self._error.show()


__all__ = ["UpdateAvailableDialog"]
