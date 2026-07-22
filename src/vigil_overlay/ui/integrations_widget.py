"""Controller-first Integrations widget for game-library connection management."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from vigil_overlay.services.integrations import IntegrationStatus
from vigil_overlay.ui.controls import repolish_widget
from vigil_overlay.ui.modal_guard import ModalActivationGuard, ModalInputSource
from vigil_overlay.widgets.registry import WidgetDefinition, WidgetItemDefinition


class IntegrationRowButton(QPushButton):
    """One controller-focusable integration row with status and action affordances."""

    def __init__(self, item: WidgetItemDefinition, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("integrationRowButton")
        self.setProperty("itemId", item.item_id)
        self.setCheckable(False)
        self.setEnabled(item.enabled)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(item.description)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(12)

        text_box = QWidget(self)
        text_box.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        text_layout = QVBoxLayout(text_box)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)

        title = QLabel(item.label, text_box)
        title.setObjectName("integrationRowTitle")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.status_label = QLabel("Checking...", text_box)
        self.status_label.setObjectName("integrationRowStatus")
        self.status_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self.detail_label = QLabel(item.description, text_box)
        self.detail_label.setObjectName("integrationRowDescription")
        self.detail_label.setWordWrap(True)
        self.detail_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        text_layout.addWidget(title)
        text_layout.addWidget(self.status_label)
        text_layout.addWidget(self.detail_label)
        layout.addWidget(text_box, 1)

        self.action_label = QLabel("", self)
        self.action_label.setObjectName("integrationRowAction")
        self.action_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        layout.addWidget(self.action_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setAccessibleName(item.label)

    def set_status(self, status: IntegrationStatus) -> None:
        status_text = status.status_text
        if status.game_count is not None:
            noun = "game" if status.game_count == 1 else "games"
            status_text = f"{status_text} · {status.game_count} {noun}"
        self.status_label.setText(status_text)

        detail = status.detail.rstrip(". ")
        if status.version:
            detail = f"{detail} · v{status.version}"
        if detail:
            detail = f"{detail}."
        self.detail_label.setText(detail)
        self.action_label.setText(status.primary_action_label or "")
        self.setAccessibleName(f"{status.label}. {status_text}. {detail}")


class IntegrationsWidgetView(QWidget):
    """Host-owned integration status and lifecycle management surface."""

    uninstall_confirmed = Signal()

    _EXPECTED_ITEMS = (
        "steam",
        "xbox",
        "epic",
        "battlenet",
        "ea",
        "ubisoft",
        "gog",
        "playnite",
        "manual_games",
        "playnite_remove",
    )

    def __init__(
        self, definition: WidgetDefinition, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("integrationsWidgetPage")
        self.setProperty("widgetId", definition.widget_id)
        self.setProperty("compactPage", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        actual = tuple(item.item_id for item in definition.items)
        if actual != self._EXPECTED_ITEMS:
            raise ValueError(
                "Integrations widget item contract does not match expected layout"
            )

        self._buttons: list[QPushButton] = []
        self._rows: dict[str, IntegrationRowButton] = {}
        self._statuses: dict[str, IntegrationStatus] = {}
        self._confirmation_popup: QFrame | None = None
        self._confirmation_buttons: list[QPushButton] = []
        self._confirmation_index = 0
        self._confirmation_guard = ModalActivationGuard(self)
        self._confirmation_guard.armed_changed.connect(self._sync_confirmation_visuals)
        self._next_input_source = ModalInputSource.UNKNOWN
        self._build_ui(definition)

    @property
    def item_buttons(self) -> tuple[QPushButton, ...]:
        return tuple(self._buttons)

    @property
    def interaction_active(self) -> bool:
        return self._confirmation_popup is not None

    def status(self, integration_id: str) -> IntegrationStatus | None:
        return self._statuses.get(integration_id)

    def set_next_input_source(self, source: ModalInputSource) -> None:
        self._next_input_source = source

    def notify_controller_activation_released(self) -> None:
        self._confirmation_guard.note_controller_activation_released()

    def set_operation_status(self, message: str, *, error: bool = False) -> None:
        self._operation_status.setText(message)
        self._operation_status.setProperty("operationError", error)
        self._operation_status.setVisible(bool(message))
        repolish_widget(self._operation_status)

    def open_uninstall_confirmation(self, message: str) -> None:
        self.cancel_interaction()
        popup = QFrame(self)
        popup.setObjectName("integrationConfirmationPopup")
        popup.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(popup)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        title = QLabel("Uninstall Playnite Integration?", popup)
        title.setObjectName("integrationConfirmationTitle")
        detail = QLabel(message, popup)
        detail.setObjectName("integrationConfirmationDetail")
        detail.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(detail)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 4, 0, 0)
        button_row.setSpacing(10)
        for index, label in enumerate(("Cancel", "Close Playnite and Uninstall")):
            button = QPushButton(label, popup)
            button.setObjectName("integrationConfirmationAction")
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(
                lambda checked=False, target=index: self._activate_confirmation(
                    target,
                    ModalInputSource.POINTER,
                )
            )
            self._confirmation_buttons.append(button)
            button_row.addWidget(button)
        layout.addLayout(button_row)

        self._confirmation_popup = popup
        self._confirmation_index = 0
        source = self._next_input_source
        self._next_input_source = ModalInputSource.UNKNOWN
        self._confirmation_guard.begin(source)
        self._sync_confirmation_visuals()
        popup.show()
        popup.raise_()
        self._position_confirmation_popup()

    def move_interaction(self, delta: int) -> bool:
        if self._confirmation_popup is None or not self._confirmation_buttons:
            return False
        target = max(
            0,
            min(len(self._confirmation_buttons) - 1, self._confirmation_index + delta),
        )
        if target == self._confirmation_index:
            return True
        self._confirmation_index = target
        self._sync_confirmation_visuals()
        return True

    def activate_interaction(self, source: ModalInputSource | None = None) -> bool:
        if self._confirmation_popup is None:
            return False
        self._activate_confirmation(
            self._confirmation_index,
            source or self._confirmation_guard.source,
        )
        return True

    def cancel_interaction(self) -> bool:
        popup = self._confirmation_popup
        if popup is None:
            return False
        self._confirmation_guard.end()
        self._confirmation_popup = None
        self._confirmation_buttons = []
        popup.hide()
        popup.deleteLater()
        return True

    def set_statuses(self, statuses: tuple[IntegrationStatus, ...]) -> None:
        self._statuses = {status.integration_id: status for status in statuses}
        mapping = {
            "steam": "steam",
            "xbox": "xbox",
            "epic": "epic",
            "battlenet": "battlenet",
            "ea": "ea",
            "ubisoft": "ubisoft",
            "gog": "gog",
            "playnite": "playnite",
            "manual_games": "manual",
        }
        for item_id, integration_id in mapping.items():
            status = self._statuses.get(integration_id)
            if status is not None:
                self._rows[item_id].set_status(status)

        playnite = self._statuses.get("playnite")
        remove_row = self._rows["playnite_remove"]
        if playnite is None:
            remove_row.status_label.setText("Unavailable")
            remove_row.detail_label.setText(
                "Playnite integration status is unavailable."
            )
            remove_row.action_label.setText("")
        elif playnite.state.value in {
            "connected",
            "restart_required",
            "update_available",
            "needs_repair",
            "error",
        }:
            remove_row.status_label.setText("Installed")
            remove_row.detail_label.setText(
                "Remove the Vigil bridge and cached snapshot."
            )
            remove_row.action_label.setText("Uninstall")
        else:
            remove_row.status_label.setText("Not installed")
            remove_row.detail_label.setText("No Vigil Playnite bridge is installed.")
            remove_row.action_label.setText("")

    def primary_action_for_item(self, item_id: str) -> str | None:
        if item_id in {
            "steam",
            "xbox",
            "epic",
            "battlenet",
            "ea",
            "ubisoft",
            "gog",
            "playnite",
        }:
            status = self._statuses.get(item_id)
            return status.primary_action if status is not None else f"refresh_{item_id}"
        if item_id == "playnite_remove":
            status = self._statuses.get("playnite")
            if status is None or status.state.value in {
                "not_detected",
                "ready_to_connect",
                "unavailable",
            }:
                return None
            return "playnite_remove"
        return None

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_confirmation_popup()

    def _activate_confirmation(self, index: int, _source: ModalInputSource) -> None:
        if self._confirmation_popup is None:
            return
        if index == 0:
            self.cancel_interaction()
            return
        if not self._confirmation_guard.accepts_activation():
            return
        self.cancel_interaction()
        self.uninstall_confirmed.emit()

    def _sync_confirmation_visuals(self) -> None:
        for index, button in enumerate(self._confirmation_buttons):
            button.setProperty("navigationFocus", index == self._confirmation_index)
            button.setProperty("activationArmed", self._confirmation_guard.armed)
            repolish_widget(button)

    def _position_confirmation_popup(self) -> None:
        popup = self._confirmation_popup
        if popup is None:
            return
        width = min(500, max(340, self.width() - 44))
        popup.setFixedWidth(width)
        popup.adjustSize()
        x = max(0, (self.width() - popup.width()) // 2)
        y = max(18, min(110, (self.height() - popup.height()) // 3))
        popup.move(x, y)
        popup.raise_()

    def _build_ui(self, definition: WidgetDefinition) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(8)

        title = QLabel(definition.label, self)
        title.setObjectName("integrationsTitle")
        description = QLabel(definition.description, self)
        description.setObjectName("integrationsDescription")
        description.setWordWrap(True)
        self._operation_status = QLabel("", self)
        self._operation_status.setObjectName("integrationOperationStatus")
        self._operation_status.setWordWrap(True)
        self._operation_status.hide()
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addWidget(self._operation_status)
        layout.addSpacing(4)

        section = QLabel("Game Libraries", self)
        section.setObjectName("settingsSectionLabel")
        layout.addWidget(section)
        underline = QFrame(self)
        underline.setObjectName("settingsSectionUnderline")
        underline.setFixedHeight(3)
        underline.setFixedWidth(118)
        layout.addWidget(underline)
        layout.addSpacing(2)

        items = {item.item_id: item for item in definition.items}
        for item_id in (
            "steam",
            "xbox",
            "epic",
            "battlenet",
            "ea",
            "ubisoft",
            "gog",
            "playnite",
            "manual_games",
        ):
            row = IntegrationRowButton(items[item_id], self)
            row.pressed.connect(
                lambda: self.set_next_input_source(ModalInputSource.POINTER)
            )
            self._buttons.append(row)
            self._rows[item_id] = row
            layout.addWidget(row)

        layout.addSpacing(8)
        management = QLabel("Management", self)
        management.setObjectName("settingsSectionLabel")
        layout.addWidget(management)
        management_underline = QFrame(self)
        management_underline.setObjectName("settingsSectionUnderline")
        management_underline.setFixedHeight(3)
        management_underline.setFixedWidth(118)
        layout.addWidget(management_underline)
        layout.addSpacing(2)

        remove_row = IntegrationRowButton(items["playnite_remove"], self)
        remove_row.pressed.connect(
            lambda: self.set_next_input_source(ModalInputSource.POINTER)
        )
        self._buttons.append(remove_row)
        self._rows["playnite_remove"] = remove_row
        layout.addWidget(remove_row)
        layout.addStretch(1)


__all__ = ["IntegrationsWidgetView"]
