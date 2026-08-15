"""Host-owned modal creation, input routing, and focus restoration."""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QDialog, QWidget

from vigil_overlay.core.updates import AvailableUpdate
from vigil_overlay.services.power_controls import PowerCapabilities
from vigil_overlay.ui.dialog_surface import VigilMessageDialog
from vigil_overlay.ui.modal_guard import ModalInputSource
from vigil_overlay.ui.power_dialog import PowerActionCallback, PowerMenuDialog
from vigil_overlay.ui.update_dialog import UpdateAvailableDialog

_LOGGER = logging.getLogger("vigil_overlay")
PowerCapabilitiesCallback = Callable[[], PowerCapabilities]
FocusCallback = Callable[[], None]
StatusCallback = Callable[[str], None]
PowerDialogFactory = Callable[
    [PowerCapabilities, PowerActionCallback, QWidget],
    PowerMenuDialog,
]
UpdateDialogFactory = Callable[[AvailableUpdate, QWidget], UpdateAvailableDialog]
MessageDialogFactory = Callable[[str, str, QWidget], VigilMessageDialog]


class OverlayDialogCoordinator(QObject):
    """Own host modals and their controller/focus lifecycle."""

    def __init__(
        self,
        host: QWidget,
        *,
        power_capabilities: PowerCapabilitiesCallback | None,
        execute_power_action: PowerActionCallback | None,
        restore_focus: FocusCallback,
        set_action_status: StatusCallback,
        request_update_handoff: FocusCallback,
    ) -> None:
        super().__init__(host)
        self._host = host
        self._power_capabilities = power_capabilities
        self._execute_power_action = execute_power_action
        self._restore_focus = restore_focus
        self._set_action_status = set_action_status
        self._request_update_handoff = request_update_handoff
        self._power_dialog: PowerMenuDialog | None = None
        self._update_dialog: UpdateAvailableDialog | None = None
        self._fps_failure_dialog: VigilMessageDialog | None = None
        self._startup_safety_dialog: VigilMessageDialog | None = None
        self._next_power_input_source = ModalInputSource.UNKNOWN

    @property
    def power_dialog(self) -> PowerMenuDialog | None:
        return self._power_dialog

    @property
    def update_dialog(self) -> UpdateAvailableDialog | None:
        return self._update_dialog

    @property
    def fps_failure_dialog(self) -> VigilMessageDialog | None:
        return self._fps_failure_dialog

    @property
    def startup_safety_dialog(self) -> VigilMessageDialog | None:
        return self._startup_safety_dialog

    def set_next_power_input_source(self, source: ModalInputSource) -> None:
        """Record which input source activated the next power menu."""

        self._next_power_input_source = source

    def notify_controller_activation_released(self) -> None:
        """Release-gate every host modal that participates in native routing."""

        for dialog in (
            self._power_dialog,
            self._update_dialog,
            self._fps_failure_dialog,
        ):
            if dialog is not None:
                dialog.notify_controller_activation_released()

    def handle_controller_command(self, command: object) -> bool:
        """Route a command to the active host modal, preserving existing priority."""

        for dialog in (
            self._update_dialog,
            self._fps_failure_dialog,
            self._power_dialog,
        ):
            if dialog is not None:
                dialog.handle_controller_command(command)
                return True
        return False

    def open_power_menu(
        self,
        *,
        dialog_factory: PowerDialogFactory = PowerMenuDialog,
    ) -> None:
        """Open one power menu with the input source that activated it."""

        capabilities = self._power_capabilities
        execute_action = self._execute_power_action
        if capabilities is None or execute_action is None:
            self._set_action_status("Power controls are unavailable.")
            return
        dialog = dialog_factory(capabilities(), execute_action, self._host)
        self._power_dialog = dialog
        source = self._next_power_input_source
        self._next_power_input_source = ModalInputSource.UNKNOWN
        dialog.begin_controller_ownership(source)
        try:
            dialog.exec()
        finally:
            self._power_dialog = None
            self._restore_focus()

    def show_available_update(
        self,
        update: object,
        *,
        dialog_factory: UpdateDialogFactory = UpdateAvailableDialog,
    ) -> None:
        """Show one validated update prompt and request handoff after acceptance."""

        if not isinstance(update, AvailableUpdate):
            _LOGGER.warning("Ignored invalid available-update payload: %r", type(update))
            return
        if self._update_dialog is not None:
            return

        dialog = dialog_factory(update, self._host)
        self._update_dialog = dialog
        dialog.begin_controller_ownership(ModalInputSource.UNKNOWN)
        release_page_opened = False
        try:
            release_page_opened = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            self._update_dialog = None
            self._restore_focus()
        if release_page_opened:
            self._request_update_handoff()

    def show_fps_runtime_failure(
        self,
        detail: str,
        *,
        dialog_factory: MessageDialogFactory = VigilMessageDialog,
    ) -> None:
        """Show one controller-dismissible PresentMon failure prompt."""

        if self._fps_failure_dialog is not None:
            return
        dialog = dialog_factory("FPS unavailable", detail, self._host)
        dialog.setObjectName("fpsRuntimeFailureDialog")
        dialog.begin_controller_ownership(ModalInputSource.UNKNOWN)
        self._fps_failure_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._fps_failure_dialog = None
            self._restore_focus()

    def show_startup_safety_warning(
        self,
        detail: str,
        *,
        dialog_factory: MessageDialogFactory = VigilMessageDialog,
    ) -> None:
        """Show one warning for uncertain startup recovery."""

        if self._startup_safety_dialog is not None:
            return
        dialog = dialog_factory(
            "Controller pass-through needs attention",
            detail,
            self._host,
        )
        dialog.setObjectName("startupSafetyWarningDialog")
        dialog.begin_controller_ownership(ModalInputSource.UNKNOWN)
        self._startup_safety_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._startup_safety_dialog = None
            self._restore_focus()


__all__ = [
    "OverlayDialogCoordinator",
    "PowerCapabilitiesCallback",
]
