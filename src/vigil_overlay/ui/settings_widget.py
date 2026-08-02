"""Xbox Compact Mode-inspired Settings widget surface."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from vigil_overlay.core.controller_shortcuts import ControllerShortcutBinding
from vigil_overlay.core.hotkeys import parse_hotkey_combination
from vigil_overlay.ui.controls import VigilToggleSwitch
from vigil_overlay.ui.modal_guard import ModalActivationGuard, ModalInputSource
from vigil_overlay.widgets.registry import WidgetDefinition, WidgetItemDefinition

HotkeyChangeCallback = Callable[[str], tuple[bool, str]]
HotkeyCaptureCallback = Callable[[bool], None]
ControllerShortcutChangeCallback = Callable[
    [ControllerShortcutBinding], tuple[bool, str]
]
ControllerShortcutCaptureCallback = Callable[[bool], None]


class HotkeyFailureKind(StrEnum):
    """User-facing reason a requested keyboard shortcut was not accepted."""

    ALREADY_IN_USE = "already_in_use"
    RESERVED = "reserved"
    UNSUPPORTED = "unsupported"
    MISSING_PRIMARY_KEY = "missing_primary_key"
    SAVE_FAILED = "save_failed"
    BACKEND_FAILED = "backend_failed"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class HotkeyFailureMessage:
    """Categorized copy shown after a keyboard shortcut change fails."""

    kind: HotkeyFailureKind
    heading: str
    explanation: str


def describe_hotkey_failure(
    candidate: str,
    current_combination: str,
    detail: str,
) -> HotkeyFailureMessage:
    """Translate validation and runtime details into actionable consumer copy."""

    normalized_detail = detail.strip()
    folded = normalized_detail.casefold()
    candidate_label = candidate.strip() or "this shortcut"
    heading = f"Couldn't use {candidate_label}"

    if "missing its primary key" in folded:
        kind = HotkeyFailureKind.MISSING_PRIMARY_KEY
        explanation = (
            "Add a primary key to the modifiers. For example, use "
            "Ctrl + Shift + Alt + A instead of Ctrl + Shift + Alt by itself."
        )
    elif "f12 is reserved by windows" in folded:
        kind = HotkeyFailureKind.RESERVED
        explanation = (
            "Windows reserves F12 for the debugger, so it cannot be used as a "
            "global Vigil shortcut. Choose a different primary key."
        )
    elif (
        "already owned by another application" in folded
        or "already registered" in folded
        or "already in use" in folded
    ):
        kind = HotkeyFailureKind.ALREADY_IN_USE
        explanation = (
            "This shortcut is already registered by Windows or another application. "
            "Windows does not report which application owns it. Try a different "
            "combination."
        )
    elif "unsupported hotkey key" in folded or "unsupported key" in folded:
        kind = HotkeyFailureKind.UNSUPPORTED
        explanation = (
            "Vigil cannot register the selected primary key as a Windows global "
            "shortcut. Use a supported letter, number, punctuation, navigation, "
            "numpad, or function key."
        )
    elif "could not save the global hotkey" in folded:
        kind = HotkeyFailureKind.SAVE_FAILED
        explanation = (
            "Vigil could not save the shortcut setting. The incomplete change was "
            "rolled back."
        )
    elif any(
        marker in folded
        for marker in (
            "hotkey backend",
            "hotkeys are supported only on windows",
            "timed out while registering",
            "registration ended without a result",
            "could not initialize the windows hotkey api",
            "safe mode is read-only",
        )
    ):
        kind = HotkeyFailureKind.BACKEND_FAILED
        explanation = (
            "Vigil's Windows hotkey service could not complete the change. "
            f"{normalized_detail.rstrip('.') or 'No additional detail was available'}."
        )
    else:
        kind = HotkeyFailureKind.INVALID
        explanation = (
            f"{normalized_detail.rstrip('.') or 'The selected combination is invalid'}."
        )

    if "previous hotkey also could not be restored" in folded:
        explanation += (
            f" Vigil could not restore the previous shortcut "
            f"{current_combination}; choose another combination before closing Vigil."
        )
    elif "previous hotkey" in folded and "was restored" in folded:
        explanation += (
            f" Your previous shortcut {current_combination} was restored and remains "
            "active."
        )
    else:
        explanation += f" Your saved shortcut {current_combination} was not changed."

    return HotkeyFailureMessage(kind, heading, explanation)


class HotkeyFailureDialog(QDialog):
    """Controller-safe explanation and retry prompt for a failed hotkey change."""

    def __init__(
        self,
        candidate: str,
        current_combination: str,
        detail: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("hotkeyFailureDialog")
        self.setWindowTitle("Global hotkey problem")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.message = describe_hotkey_failure(
            candidate,
            current_combination,
            detail,
        )
        self._guard = ModalActivationGuard(self)
        self._controller_index = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        heading = QLabel(self.message.heading, self)
        heading.setObjectName("hotkeyEditorTitle")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        self.explanation_label = QLabel(self.message.explanation, self)
        self.explanation_label.setObjectName("hotkeyEditorHelp")
        self.explanation_label.setWordWrap(True)
        layout.addWidget(self.explanation_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Retry
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.setObjectName("hotkeyEditorButtons")
        retry_button = buttons.button(QDialogButtonBox.StandardButton.Retry)
        cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if retry_button is None or cancel_button is None:
            raise RuntimeError("hotkey failure dialog buttons could not be created")
        retry_button.setText("Try Again")
        retry_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self._controller_buttons = [retry_button, cancel_button]

    def begin_controller_ownership(self, source: ModalInputSource) -> None:
        self._guard.begin(source)
        self._sync_controller_focus()

    def notify_controller_activation_released(self) -> None:
        self._guard.note_controller_activation_released()

    def handle_controller_command(self, command: object) -> bool:
        value = getattr(command, "value", command)
        if value in {"move_left", "move_up"}:
            self._controller_index = (self._controller_index - 1) % len(
                self._controller_buttons
            )
            self._sync_controller_focus()
            return True
        if value in {"move_right", "move_down"}:
            self._controller_index = (self._controller_index + 1) % len(
                self._controller_buttons
            )
            self._sync_controller_focus()
            return True
        if value == "back":
            self.reject()
            return True
        if value == "activate":
            if self._guard.accepts_activation():
                self._controller_buttons[self._controller_index].click()
            return True
        return True

    def _sync_controller_focus(self) -> None:
        self._controller_buttons[self._controller_index].setFocus(
            Qt.FocusReason.OtherFocusReason
        )


class HotkeyEditorDialog(QDialog):
    """Modal editor that validates one conservative global hotkey combination."""

    def __init__(
        self,
        current_combination: str,
        apply_callback: HotkeyChangeCallback,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("hotkeyEditorDialog")
        self.setWindowTitle("Global hotkey")
        self.setModal(True)
        self.setMinimumWidth(480)
        self._apply_callback = apply_callback
        self._combination = current_combination
        self._guard = ModalActivationGuard(self)
        self._controller_buttons: list[QPushButton] = []
        self._controller_index = 0
        self._failure_dialog: HotkeyFailureDialog | None = None
        self._controller_activation_in_progress = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        title = QLabel("Change global hotkey", self)
        title.setObjectName("hotkeyEditorTitle")
        layout.addWidget(title)

        help_label = QLabel(
            "Press the modifier and key combination you want to use. "
            "Vigil requires at least one modifier key.",
            self,
        )
        help_label.setObjectName("hotkeyEditorHelp")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)

        self.sequence_edit = QKeySequenceEdit(QKeySequence(current_combination), self)
        self.sequence_edit.setObjectName("hotkeySequenceEdit")
        self.sequence_edit.setMaximumSequenceLength(1)
        layout.addWidget(self.sequence_edit)

        self.error_label = QLabel("", self)
        self.error_label.setObjectName("hotkeyEditorError")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.setObjectName("hotkeyEditorButtons")
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_button is not None:
            save_button.setText("Apply")
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._controller_buttons = [
            button
            for button in (
                buttons.button(QDialogButtonBox.StandardButton.Save),
                buttons.button(QDialogButtonBox.StandardButton.Cancel),
            )
            if button is not None
        ]

    def begin_controller_ownership(self, source: ModalInputSource) -> None:
        self._guard.begin(source)
        self._sync_controller_focus()

    def notify_controller_activation_released(self) -> None:
        if self._failure_dialog is not None:
            self._failure_dialog.notify_controller_activation_released()
            return
        self._guard.note_controller_activation_released()

    def handle_controller_command(self, command: object) -> bool:
        if self._failure_dialog is not None:
            return self._failure_dialog.handle_controller_command(command)
        value = getattr(command, "value", command)
        if value in {"move_left", "move_up"}:
            self._controller_index = (self._controller_index - 1) % len(
                self._controller_buttons
            )
            self._sync_controller_focus()
            return True
        if value in {"move_right", "move_down"}:
            self._controller_index = (self._controller_index + 1) % len(
                self._controller_buttons
            )
            self._sync_controller_focus()
            return True
        if value == "back":
            self.reject()
            return True
        if value == "activate":
            if self._guard.accepts_activation():
                self._controller_activation_in_progress = True
                try:
                    self._controller_buttons[self._controller_index].click()
                finally:
                    self._controller_activation_in_progress = False
            return True
        return True

    def _sync_controller_focus(self) -> None:
        if not self._controller_buttons:
            return
        self._controller_buttons[self._controller_index].setFocus(
            Qt.FocusReason.OtherFocusReason
        )

    @property
    def combination(self) -> str:
        return self._combination

    def _apply(self) -> None:
        candidate = self.sequence_edit.keySequence().toString(
            QKeySequence.SequenceFormat.PortableText
        )
        try:
            canonical = parse_hotkey_combination(candidate).canonical
        except ValueError as exc:
            self._show_failure(candidate, str(exc))
            return

        success, detail = self._apply_callback(canonical)
        if not success:
            self._show_failure(canonical, detail)
            return

        self._combination = canonical
        self.accept()

    def _show_failure(self, candidate: str, detail: str) -> None:
        self.error_label.setText(detail)
        self.error_label.show()
        source = (
            ModalInputSource.CONTROLLER
            if self._controller_activation_in_progress
            else ModalInputSource.UNKNOWN
        )
        failure_dialog = HotkeyFailureDialog(
            candidate,
            self._combination,
            detail,
            self,
        )
        self._failure_dialog = failure_dialog
        failure_dialog.begin_controller_ownership(source)
        try:
            result = failure_dialog.exec()
        finally:
            self._failure_dialog = None
            failure_dialog.deleteLater()

        if result != QDialog.DialogCode.Accepted:
            self.reject()
            return

        self.error_label.hide()
        self._guard.begin(source)
        self.sequence_edit.setFocus(Qt.FocusReason.OtherFocusReason)


class ControllerShortcutEditorDialog(QDialog):
    """Neutral-gated physical controller capture with controller-owned review."""

    def __init__(
        self,
        current_binding: ControllerShortcutBinding,
        apply_callback: ControllerShortcutChangeCallback,
        capture_callback: ControllerShortcutCaptureCallback,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("controllerShortcutEditorDialog")
        self.setWindowTitle("Controller shortcut")
        self.setModal(True)
        self.setMinimumWidth(480)
        self._binding = current_binding
        self._apply_callback = apply_callback
        self._capture_callback = capture_callback
        self._guard = ModalActivationGuard(self)
        self._review_buttons: list[QPushButton] = []
        self._review_index = 0
        self._listening = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        title = QLabel("Capture controller shortcut", self)
        title.setObjectName("hotkeyEditorTitle")
        layout.addWidget(title)
        self._status = QLabel(
            "Release all controls, then press the button or combination you want.",
            self,
        )
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._captured = QLabel("Waiting for controller input…", self)
        self._captured.setObjectName("settingsRowTrailing")
        layout.addWidget(self._captured)
        self._error = QLabel("", self)
        self._error.setWordWrap(True)
        self._error.hide()
        layout.addWidget(self._error)

        row = QHBoxLayout()
        apply_button = QPushButton("Apply", self)
        retry_button = QPushButton("Retry", self)
        cancel_button = QPushButton("Cancel", self)
        apply_button.clicked.connect(self._apply)
        retry_button.clicked.connect(self._retry)
        cancel_button.clicked.connect(self.reject)
        row.addWidget(apply_button)
        row.addWidget(retry_button)
        row.addWidget(cancel_button)
        layout.addLayout(row)
        self._review_buttons = [apply_button, retry_button, cancel_button]
        for button in self._review_buttons:
            button.setEnabled(False)
        cancel_button.setEnabled(True)

    @property
    def binding(self) -> ControllerShortcutBinding:
        return self._binding

    def begin_controller_ownership(self, source: ModalInputSource) -> None:
        self._guard.begin(source)
        self._capture_callback(True)

    def notify_controller_activation_released(self) -> None:
        self._guard.note_controller_activation_released()

    def set_captured_binding(self, binding: ControllerShortcutBinding) -> None:
        self._binding = binding
        self._listening = False
        self._captured.setText(binding.display_label)
        self._status.setText("Review the detected shortcut, then Apply or Retry.")
        for button in self._review_buttons:
            button.setEnabled(True)
        self._review_index = 0
        self._guard.begin(ModalInputSource.UNKNOWN)
        self._sync_focus()

    def handle_controller_command(self, command: object) -> bool:
        value = getattr(command, "value", command)
        if self._listening:
            return True
        if value in {"move_left", "move_up"}:
            self._review_index = (self._review_index - 1) % len(self._review_buttons)
            self._sync_focus()
            return True
        if value in {"move_right", "move_down"}:
            self._review_index = (self._review_index + 1) % len(self._review_buttons)
            self._sync_focus()
            return True
        if value == "back":
            self.reject()
            return True
        if value == "activate":
            if self._guard.accepts_activation():
                self._review_buttons[self._review_index].click()
            return True
        return True

    def _retry(self) -> None:
        self._listening = True
        self._captured.setText("Waiting for controller input…")
        self._status.setText(
            "Release all controls, then press the button or combination you want."
        )
        self._error.hide()
        for button in self._review_buttons:
            button.setEnabled(False)
        self._review_buttons[-1].setEnabled(True)
        self._capture_callback(True)

    def _apply(self) -> None:
        success, detail = self._apply_callback(self._binding)
        if success:
            self.accept()
            return
        self._error.setText(detail)
        self._error.show()

    def _sync_focus(self) -> None:
        self._review_buttons[self._review_index].setFocus(
            Qt.FocusReason.OtherFocusReason
        )


class SettingsToggleSwitch(VigilToggleSwitch):
    """Settings-specific alias for Vigil's shared toggle indicator."""


class SettingsRowButton(QPushButton):
    """Controller-focusable settings row with host-owned trailing content."""

    def __init__(
        self,
        item: WidgetItemDefinition,
        parent: QWidget,
        *,
        trailing_text: str | None = None,
        toggle: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settingsRowButton")
        self.setProperty("itemId", item.item_id)
        self.setCheckable(False)
        self.setEnabled(item.enabled)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(item.description)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(12)

        text_box = QWidget(self)
        text_box.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        text_layout = QVBoxLayout(text_box)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(1)
        title = QLabel(item.label, text_box)
        title.setObjectName("settingsRowTitle")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        description = QLabel(item.description, text_box)
        description.setObjectName("settingsRowDescription")
        description.setWordWrap(True)
        description.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        text_layout.addWidget(title)
        text_layout.addWidget(description)
        layout.addWidget(text_box, 1)

        self.toggle_switch: SettingsToggleSwitch | None = None
        self.trailing_label: QLabel | None = None
        if toggle:
            self.toggle_switch = SettingsToggleSwitch(self)
            layout.addWidget(self.toggle_switch, 0, Qt.AlignmentFlag.AlignVCenter)
        elif trailing_text is not None:
            self.trailing_label = QLabel(trailing_text, self)
            self.trailing_label.setObjectName("settingsRowTrailing")
            self.trailing_label.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
            )
            layout.addWidget(self.trailing_label, 0, Qt.AlignmentFlag.AlignVCenter)

        self._title_text = item.label
        self.setAccessibleName(f"{item.label}. {item.description}")

    def set_toggle_checked(self, checked: bool) -> None:
        if self.toggle_switch is None:
            raise RuntimeError("settings row does not contain a toggle")
        self.toggle_switch.setChecked(checked)
        state = "On" if checked else "Off"
        self.setAccessibleName(f"{self._title_text}: {state}")


@dataclass(frozen=True, slots=True)
class _SectionSpec:
    title: str
    item_ids: tuple[str, ...]


class SettingsWidgetView(QWidget):
    """Controller-first Settings surface with a true Guide-button toggle."""

    _SECTIONS = (
        _SectionSpec(
            "Controls",
            (
                "guide_button",
                "controller_shortcut",
                "allow_mouse_navigation_while_controller_connected",
                "global_hotkey",
            ),
        ),
        _SectionSpec("Overlay", ("start_with_windows", "run_in_background")),
        _SectionSpec("Widgets", ("widgets",)),
        _SectionSpec("Recovery", ("safe_mode", "reset_window_position")),
    )

    def __init__(
        self,
        definition: WidgetDefinition,
        parent: QWidget | None = None,
        *,
        guide_button_enabled: bool,
        controller_shortcut_binding: ControllerShortcutBinding | None = None,
        allow_mouse_navigation_while_controller_connected: bool = False,
        hotkey_combination: str,
        start_with_windows_enabled: bool = False,
        start_with_windows_available: bool = True,
        run_in_background_enabled: bool = True,
        run_in_background_available: bool = True,
        safe_mode_active: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settingsWidgetPage")
        self.setProperty("widgetId", definition.widget_id)
        self.setProperty("compactPage", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        expected = tuple(
            item_id for section in self._SECTIONS for item_id in section.item_ids
        )
        actual = tuple(item.item_id for item in definition.items)
        if actual != expected:
            raise ValueError(
                "Settings widget item contract does not match section layout"
            )

        self._buttons: list[QPushButton] = []
        self._buttons_by_item: dict[str, SettingsRowButton] = {}
        self._guide_button_enabled = guide_button_enabled
        self._controller_shortcut_binding = (
            controller_shortcut_binding or ControllerShortcutBinding()
        )
        self._active_dialog: (
            HotkeyEditorDialog | ControllerShortcutEditorDialog | None
        ) = None
        self._next_input_source = ModalInputSource.UNKNOWN
        self._allow_mouse_navigation_while_controller_connected = (
            allow_mouse_navigation_while_controller_connected
        )
        self._hotkey_combination = hotkey_combination
        self._start_with_windows_enabled = start_with_windows_enabled
        self._start_with_windows_available = start_with_windows_available
        self._run_in_background_enabled = run_in_background_enabled
        self._run_in_background_available = run_in_background_available
        self._safe_mode_active = safe_mode_active
        self._build_ui(definition)

    @property
    def item_buttons(self) -> tuple[QPushButton, ...]:
        return tuple(self._buttons)

    @property
    def guide_button_enabled(self) -> bool:
        return self._guide_button_enabled

    def set_guide_button_enabled(self, enabled: bool) -> None:
        self._guide_button_enabled = enabled
        row = self._buttons_by_item["guide_button"]
        row.set_toggle_checked(enabled)
        row.update()

    @property
    def allow_mouse_navigation_while_controller_connected(self) -> bool:
        return self._allow_mouse_navigation_while_controller_connected

    def set_allow_mouse_navigation_while_controller_connected(
        self, enabled: bool
    ) -> None:
        self._allow_mouse_navigation_while_controller_connected = enabled
        row = self._buttons_by_item["allow_mouse_navigation_while_controller_connected"]
        row.set_toggle_checked(enabled)
        row.update()

    def set_start_with_windows_enabled(self, enabled: bool) -> None:
        self._start_with_windows_enabled = enabled
        row = self._buttons_by_item["start_with_windows"]
        row.set_toggle_checked(enabled)
        row.update()

    def set_start_with_windows_available(self, available: bool) -> None:
        self._start_with_windows_available = available
        row = self._buttons_by_item["start_with_windows"]
        row.setEnabled(available)

    def set_run_in_background_enabled(self, enabled: bool) -> None:
        self._run_in_background_enabled = enabled
        row = self._buttons_by_item["run_in_background"]
        row.set_toggle_checked(enabled)
        row.update()

    def set_run_in_background_available(self, available: bool) -> None:
        self._run_in_background_available = available
        row = self._buttons_by_item["run_in_background"]
        row.setEnabled(available)

    def set_safe_mode_active(self, active: bool) -> None:
        self._safe_mode_active = active
        row = self._buttons_by_item["safe_mode"]
        row.setEnabled(not active)
        if row.trailing_label is not None:
            row.trailing_label.setText("Active" if active else "Restart")

    def set_hotkey_combination(self, combination: str) -> None:
        self._hotkey_combination = combination
        row = self._buttons_by_item["global_hotkey"]
        if row.trailing_label is not None:
            row.trailing_label.setText(combination)

    @property
    def interaction_active(self) -> bool:
        return self._active_dialog is not None

    def set_next_input_source(self, source: ModalInputSource) -> None:
        self._next_input_source = source

    def notify_controller_activation_released(self) -> None:
        dialog = self._active_dialog
        if dialog is not None:
            dialog.notify_controller_activation_released()

    def handle_controller_command(self, command: object) -> bool:
        dialog = self._active_dialog
        if dialog is None:
            return False
        return dialog.handle_controller_command(command)

    def set_controller_shortcut_binding(
        self, binding: ControllerShortcutBinding
    ) -> None:
        self._controller_shortcut_binding = binding
        row = self._buttons_by_item["controller_shortcut"]
        if row.trailing_label is not None:
            row.trailing_label.setText(binding.display_label)

    def deliver_controller_shortcut(self, binding: ControllerShortcutBinding) -> None:
        dialog = self._active_dialog
        if isinstance(dialog, ControllerShortcutEditorDialog):
            dialog.set_captured_binding(binding)

    def open_hotkey_editor(
        self,
        apply_callback: HotkeyChangeCallback,
        *,
        capture_callback: HotkeyCaptureCallback | None = None,
    ) -> bool:
        dialog = HotkeyEditorDialog(self._hotkey_combination, apply_callback, self)
        self._active_dialog = dialog
        source = self._consume_input_source()
        dialog.begin_controller_ownership(source)
        if capture_callback is not None:
            capture_callback(True)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            self.set_hotkey_combination(dialog.combination)
            return True
        finally:
            self._active_dialog = None
            if capture_callback is not None:
                capture_callback(False)

    def open_controller_shortcut_editor(
        self,
        apply_callback: ControllerShortcutChangeCallback,
        capture_callback: ControllerShortcutCaptureCallback,
    ) -> bool:
        dialog = ControllerShortcutEditorDialog(
            self._controller_shortcut_binding,
            apply_callback,
            capture_callback,
            self,
        )
        self._active_dialog = dialog
        dialog.begin_controller_ownership(self._consume_input_source())
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            self.set_controller_shortcut_binding(dialog.binding)
            return True
        finally:
            capture_callback(False)
            self._active_dialog = None

    def _consume_input_source(self) -> ModalInputSource:
        source = self._next_input_source
        self._next_input_source = ModalInputSource.UNKNOWN
        return source

    def _build_ui(self, definition: WidgetDefinition) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(8)

        title = QLabel(definition.label, self)
        title.setObjectName("settingsTitle")
        layout.addWidget(title)

        items = {item.item_id: item for item in definition.items}
        for section_index, section in enumerate(self._SECTIONS):
            if section_index:
                layout.addSpacing(8)
            section_label = QLabel(section.title, self)
            section_label.setObjectName("settingsSectionLabel")
            layout.addWidget(section_label)

            underline = QFrame(self)
            underline.setObjectName("settingsSectionUnderline")
            underline.setFixedHeight(3)
            underline.setFixedWidth(118)
            layout.addWidget(underline)
            layout.addSpacing(2)

            for item_id in section.item_ids:
                item = items[item_id]
                row = self._create_row(item)
                self._buttons.append(row)
                self._buttons_by_item[item_id] = row
                layout.addWidget(row)

        layout.addStretch(1)
        self.set_guide_button_enabled(self._guide_button_enabled)
        self.set_allow_mouse_navigation_while_controller_connected(
            self._allow_mouse_navigation_while_controller_connected
        )
        self.set_start_with_windows_enabled(self._start_with_windows_enabled)
        self.set_start_with_windows_available(self._start_with_windows_available)
        self.set_run_in_background_enabled(self._run_in_background_enabled)
        self.set_run_in_background_available(self._run_in_background_available)
        self.set_safe_mode_active(self._safe_mode_active)
        self.set_controller_shortcut_binding(self._controller_shortcut_binding)

    def _create_row(self, item: WidgetItemDefinition) -> SettingsRowButton:
        if item.item_id == "guide_button":
            return SettingsRowButton(item, self, toggle=True)
        if item.item_id == "controller_shortcut":
            return SettingsRowButton(
                item,
                self,
                trailing_text=self._controller_shortcut_binding.display_label,
            )
        if item.item_id == "allow_mouse_navigation_while_controller_connected":
            return SettingsRowButton(item, self, toggle=True)
        if item.item_id == "global_hotkey":
            return SettingsRowButton(item, self, trailing_text=self._hotkey_combination)
        if item.item_id == "start_with_windows":
            return SettingsRowButton(item, self, toggle=True)
        if item.item_id == "run_in_background":
            return SettingsRowButton(item, self, toggle=True)
        if item.item_id == "widgets":
            return SettingsRowButton(item, self, trailing_text=">>")
        if item.item_id == "safe_mode":
            return SettingsRowButton(item, self, trailing_text="Restart")
        if item.item_id == "reset_window_position":
            return SettingsRowButton(item, self, trailing_text="Reset")
        raise ValueError(f"unsupported settings item: {item.item_id}")
