"""Xbox Compact Mode-inspired Settings widget surface."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from math import ceil
from time import monotonic

from PySide6.QtCore import Qt, QTimer
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

from vigil_overlay.core.controller_shortcuts import (
    ControllerShortcutBinding,
    ControllerShortcutCaptureResult,
)
from vigil_overlay.core.hotkeys import (
    SUPPORTED_HOTKEY_PRIMARY_KEYS,
    parse_hotkey_combination,
)
from vigil_overlay.ui.controls import VigilSelectorButton, VigilToggleSwitch
from vigil_overlay.ui.dialog_surface import ControllerVigilDialog
from vigil_overlay.ui.modal_guard import ModalInputSource
from vigil_overlay.ui.selector_popup import SelectorPopup
from vigil_overlay.widgets.registry import WidgetDefinition, WidgetItemDefinition

HotkeyChangeCallback = Callable[[str], tuple[bool, str]]
HotkeyCaptureCallback = Callable[[bool], None]
HotkeyProbeCallback = Callable[[str], tuple[bool, str]]
ControllerShortcutChangeCallback = Callable[[ControllerShortcutBinding], tuple[bool, str]]
ControllerShortcutCaptureCallback = Callable[[bool], int | None]
ControllerShortcutEditingCallback = Callable[[bool], None]


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
            "Vigil could not save the shortcut setting. The incomplete change was rolled back."
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
        explanation = f"{normalized_detail.rstrip('.') or 'The selected combination is invalid'}."

    if "previous hotkey also could not be restored" in folded:
        explanation += (
            f" Vigil could not restore the previous shortcut "
            f"{current_combination}; choose another combination before closing Vigil."
        )
    elif "previous hotkey" in folded and "was restored" in folded:
        explanation += (
            f" Your previous shortcut {current_combination} was restored and remains active."
        )
    else:
        explanation += f" Your saved shortcut {current_combination} was not changed."

    return HotkeyFailureMessage(kind, heading, explanation)


class HotkeyFailureDialog(ControllerVigilDialog):
    """Controller-safe explanation and retry prompt for a failed hotkey change."""

    def __init__(
        self,
        candidate: str,
        current_combination: str,
        detail: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Global hotkey problem", parent, width=480)
        self.setObjectName("hotkeyFailureDialog")
        self.message = describe_hotkey_failure(
            candidate,
            current_combination,
            detail,
        )

        self.add_title(self.message.heading)
        self.explanation_label = self.add_message(self.message.explanation)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Retry | QDialogButtonBox.StandardButton.Cancel,
            parent=self.surface,
        )
        buttons.setObjectName("vigilDialogButtons")
        retry_button = buttons.button(QDialogButtonBox.StandardButton.Retry)
        cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if retry_button is None or cancel_button is None:
            raise RuntimeError("hotkey failure dialog buttons could not be created")
        retry_button.setText("Try Again")
        self.style_button(retry_button, kind="primary")
        self.style_button(cancel_button)
        retry_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)
        self.content_layout.addWidget(buttons)
        self.set_controller_buttons((retry_button, cancel_button))


class HotkeyEditorDialog(ControllerVigilDialog):
    """Modal editor that validates one conservative global hotkey combination."""

    def __init__(
        self,
        current_combination: str,
        apply_callback: HotkeyChangeCallback,
        probe_callback: HotkeyProbeCallback | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Global hotkey", parent, width=480)
        self.setObjectName("hotkeyEditorDialog")
        self._apply_callback = apply_callback
        self._probe_callback = probe_callback
        self._combination = current_combination
        self._failure_dialog: HotkeyFailureDialog | None = None
        self._controller_activation_in_progress = False
        self._candidate_available = probe_callback is None
        self._validated_candidate: str | None = None
        self._picker_sync_active = False
        self._key_popup: SelectorPopup | None = None

        layout = self.content_layout
        self.add_title("Change global hotkey")
        self.add_message(
            "Press the modifier and key combination you want to use. "
            "Vigil requires at least one modifier key."
        )

        self.sequence_edit = QKeySequenceEdit(QKeySequence(current_combination), self)
        self.sequence_edit.setObjectName("hotkeySequenceEdit")
        self.sequence_edit.setMaximumSequenceLength(1)
        layout.addWidget(self.sequence_edit)

        self.add_detail(
            "If Windows or another app consumes the chord, choose its modifiers and "
            "primary key here instead."
        )

        picker_row = QHBoxLayout()
        picker_row.setSpacing(8)
        parsed_current = parse_hotkey_combination(current_combination)
        self.modifier_buttons: dict[str, QPushButton] = {}
        for modifier in ("Ctrl", "Alt", "Shift", "Win"):
            button = QPushButton(modifier, self)
            button.setObjectName(f"hotkeyModifier{modifier}")
            button.setCheckable(True)
            button.setChecked(modifier in parsed_current.modifiers)
            button.setAccessibleName(f"{modifier} modifier")
            self.style_button(button, kind="toggle")
            picker_row.addWidget(button)
            self.modifier_buttons[modifier] = button
        layout.addLayout(picker_row)
        self._primary_key = parsed_current.key
        self.primary_key_picker = VigilSelectorButton(self.surface)
        self.primary_key_picker.setObjectName("hotkeySelectorButton")
        self.primary_key_picker.setAccessibleName("Global hotkey primary key")
        self.primary_key_picker.setText(self._primary_key)
        self.primary_key_picker.clicked.connect(self._toggle_key_popup)
        layout.addWidget(self.primary_key_picker)
        self.finished.connect(self._close_key_popup)

        self.error_label = self.add_error()
        self.error_label.hide()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            parent=self.surface,
        )
        buttons.setObjectName("vigilDialogButtons")
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_button is None:
            raise RuntimeError("hotkey editor Apply button could not be created")
        self._save_button = save_button
        self._save_button.setText("Apply")
        self._save_button.setEnabled(self._candidate_available)
        self.style_button(self._save_button, kind="primary")
        cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_button is not None:
            self.style_button(cancel_button)
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        controller_buttons = [
            button
            for button in (
                *self.modifier_buttons.values(),
                self.primary_key_picker,
                buttons.button(QDialogButtonBox.StandardButton.Save),
                buttons.button(QDialogButtonBox.StandardButton.Cancel),
            )
            if button is not None
        ]
        self.set_controller_buttons(controller_buttons)
        self.sequence_edit.keySequenceChanged.connect(self._on_sequence_changed)
        for button in self.modifier_buttons.values():
            button.toggled.connect(self._on_picker_changed)

    def notify_controller_activation_released(self) -> None:
        if self._failure_dialog is not None:
            self._failure_dialog.notify_controller_activation_released()
            return
        super().notify_controller_activation_released()

    def handle_controller_command(self, command: object) -> bool:
        if self._failure_dialog is not None:
            return self._failure_dialog.handle_controller_command(command)
        value = getattr(command, "value", command)
        popup = self._key_popup
        if popup is not None:
            if value in {"move_left", "move_up"}:
                popup.move_selection(-1)
            elif value in {"move_right", "move_down"}:
                popup.move_selection(1)
            elif value == "activate" and self._guard.accepts_activation():
                self._controller_activation_in_progress = True
                try:
                    popup.activate_selection()
                finally:
                    self._controller_activation_in_progress = False
            elif value == "back":
                self._close_key_popup()
                self.primary_key_picker.setFocus()
            return True
        return super().handle_controller_command(command)

    def controller_back(self) -> None:
        if self._key_popup is not None:
            self._close_key_popup()
            self.primary_key_picker.setFocus()
        else:
            super().controller_back()

    def _toggle_key_popup(self) -> None:
        if self._key_popup is not None:
            self._close_key_popup()
            return
        self._key_popup = SelectorPopup(
            self.surface,
            anchor=self.primary_key_picker,
            option_labels=SUPPORTED_HOTKEY_PRIMARY_KEYS,
            selected_index=SUPPORTED_HOTKEY_PRIMARY_KEYS.index(self._primary_key),
            object_prefix="hotkey",
            option_selected=self._select_primary_key,
        )
        self.primary_key_picker.set_selector_open(True)
        self._key_popup.show_anchored()
        self._guard.begin(
            ModalInputSource.CONTROLLER
            if self._controller_activation_in_progress
            else ModalInputSource.UNKNOWN
        )

    def _close_key_popup(self, _result: int = 0) -> None:
        popup = self._key_popup
        self._key_popup = None
        if popup is not None:
            popup.dispose()
        self.primary_key_picker.set_selector_open(False)

    def _select_primary_key(self, index: int) -> None:
        self._primary_key = SUPPORTED_HOTKEY_PRIMARY_KEYS[index]
        self.primary_key_picker.setText(self._primary_key)
        self._close_key_popup()
        self.primary_key_picker.setFocus()
        self._guard.begin(
            ModalInputSource.CONTROLLER
            if self._controller_activation_in_progress
            else ModalInputSource.UNKNOWN
        )
        self._on_picker_changed()

    def activate_controller_selection(self) -> None:
        self._controller_activation_in_progress = True
        try:
            super().activate_controller_selection()
        finally:
            self._controller_activation_in_progress = False

    @property
    def combination(self) -> str:
        return self._combination

    def begin_availability_checks(self) -> None:
        """Validate the displayed candidate after the active registration is released."""

        self._validate_candidate(show_failure=True)

    def _on_sequence_changed(self, _sequence: QKeySequence) -> None:
        if self._picker_sync_active:
            return
        candidate = self._candidate_text()
        try:
            parsed = parse_hotkey_combination(candidate)
        except ValueError:
            self._validate_candidate(
                show_failure=self._probe_callback is not None and bool(candidate.strip())
            )
            return
        self._picker_sync_active = True
        try:
            for modifier, button in self.modifier_buttons.items():
                button.setChecked(modifier in parsed.modifiers)
            self._primary_key = parsed.key
            self.primary_key_picker.setText(parsed.key)
        finally:
            self._picker_sync_active = False
        self._validate_candidate(show_failure=self._probe_callback is not None)

    def _on_picker_changed(self, _value: object = None) -> None:
        if self._picker_sync_active:
            return
        modifiers = [
            modifier
            for modifier in ("Ctrl", "Alt", "Shift", "Win")
            if self.modifier_buttons[modifier].isChecked()
        ]
        candidate = "+".join((*modifiers, self._primary_key))
        self._picker_sync_active = True
        try:
            self.sequence_edit.setKeySequence(QKeySequence(candidate))
        finally:
            self._picker_sync_active = False
        self._validate_candidate(show_failure=self._probe_callback is not None)

    def _candidate_text(self) -> str:
        return self.sequence_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)

    def _validate_candidate(self, *, show_failure: bool) -> bool:
        candidate = self._candidate_text()
        self._candidate_available = False
        self._validated_candidate = None
        self._save_button.setEnabled(False)
        try:
            canonical = parse_hotkey_combination(candidate).canonical
        except ValueError as exc:
            detail = str(exc)
            self.error_label.setText(detail)
            self.error_label.show()
            folded = detail.casefold()
            if show_failure and "missing its primary key" not in folded:
                self._show_failure(candidate, detail)
            return False

        if self._probe_callback is not None:
            available, detail = self._probe_callback(canonical)
            if not available:
                self.error_label.setText(detail)
                self.error_label.show()
                if show_failure:
                    self._show_failure(canonical, detail)
                return False

        self._candidate_available = True
        self._validated_candidate = canonical
        self._save_button.setEnabled(True)
        self.error_label.hide()
        return True

    def _apply(self) -> None:
        candidate = self._candidate_text()
        try:
            canonical = parse_hotkey_combination(candidate).canonical
        except ValueError as exc:
            self._show_failure(candidate, str(exc))
            return

        if (
            not self._candidate_available or self._validated_candidate != canonical
        ) and not self._validate_candidate(show_failure=True):
            return

        success, detail = self._apply_callback(canonical)
        if not success:
            self._show_failure(canonical, detail)
            return

        self._combination = canonical
        self.accept()

    def _show_failure(self, candidate: str, detail: str) -> None:
        restore_index = self._controller_index
        restore_keyboard_entry = self.sequence_edit.hasFocus()
        self._close_key_popup()
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
        if restore_keyboard_entry:
            self.sequence_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self._controller_index = restore_index
            self.sync_controller_focus()


class ControllerShortcutEditorDialog(ControllerVigilDialog):
    """Explicit bounded capture followed by controller-owned review."""

    def __init__(
        self,
        current_binding: ControllerShortcutBinding,
        apply_callback: ControllerShortcutChangeCallback,
        capture_callback: ControllerShortcutCaptureCallback,
        parent: QWidget | None = None,
        *,
        editing_callback: ControllerShortcutEditingCallback | None = None,
    ) -> None:
        super().__init__("Controller shortcut", parent, width=480)
        self.setObjectName("controllerShortcutEditorDialog")
        self._binding = current_binding
        self._apply_callback = apply_callback
        self._capture_callback = capture_callback
        self._editing_callback = editing_callback
        self._session_active = False
        self._listening = False
        self._has_candidate = False
        self._attempt_id: int | None = None
        self._deadline = 0.0
        self._capture_timer = QTimer(self)
        self._capture_timer.setInterval(100)
        self._capture_timer.timeout.connect(self._update_countdown)

        layout = self.content_layout
        self.add_title("Capture controller shortcut")
        self._status = self.add_message(
            "Choose Start capture to record a controller shortcut, or Cancel to go back."
        )
        self._captured = self.add_detail(f"Current shortcut: {current_binding.display_label}")
        self._error = self.add_error()
        self._error.hide()

        row = QHBoxLayout()
        self._apply_button = QPushButton("Apply", self.surface)
        self._start_button = QPushButton("Start capture", self.surface)
        self._cancel_button = QPushButton("Cancel", self.surface)
        self._apply_button.clicked.connect(self._apply)
        self._start_button.clicked.connect(self._start_capture)
        self._cancel_button.clicked.connect(self.reject)
        row.addWidget(self._apply_button)
        row.addWidget(self._start_button)
        row.addWidget(self._cancel_button)
        layout.addLayout(row)
        self._review_buttons = [self._apply_button, self._start_button, self._cancel_button]
        self.style_button(self._apply_button, kind="primary")
        self.style_button(self._start_button)
        self.style_button(self._cancel_button)
        self.finished.connect(self._finish_session)
        self._show_actions()

    @property
    def binding(self) -> ControllerShortcutBinding:
        return self._binding

    def begin_controller_ownership(self, source: ModalInputSource) -> None:
        if not self._session_active:
            self._session_active = True
            if self._editing_callback is not None:
                self._editing_callback(True)
        super().begin_controller_ownership(source)

    def set_captured_binding(self, result: ControllerShortcutCaptureResult) -> None:
        if not self._listening or result.attempt_id != self._attempt_id:
            return
        if monotonic() >= self._deadline:
            self._capture_timed_out(
                "Capture timed out. Choose Start capture or Retry to try again."
            )
            return
        self._stop_capture()
        self._binding = result.binding
        self._has_candidate = True
        self._captured.setText(result.binding.display_label)
        self._status.setText("Review the detected shortcut, then Apply or Retry.")
        self._guard.begin(ModalInputSource.UNKNOWN)
        self._show_actions()

    def handle_controller_command(self, command: object) -> bool:
        value = getattr(command, "value", command)
        if self._listening:
            return True
        return super().handle_controller_command(value)

    def _start_capture(self) -> None:
        if self._listening:
            return
        self._listening = True
        self.set_controller_buttons(())
        for button in self._review_buttons:
            button.hide()
        self._error.hide()
        self._captured.setText("Release all controls, then press and release your shortcut.")
        self._deadline = monotonic() + 10.0
        self._attempt_id = self._capture_callback(True)
        if self._attempt_id is None:
            self._capture_timed_out("Controller capture is unavailable. Try again.")
            return
        self._update_countdown()
        if self._listening:
            self._capture_timer.start()

    def _show_actions(self) -> None:
        self._apply_button.setVisible(self._has_candidate)
        self._start_button.setText("Retry" if self._has_candidate else "Start capture")
        self._start_button.show()
        self._cancel_button.show()
        self.set_controller_buttons(
            self._review_buttons
            if self._has_candidate
            else (self._start_button, self._cancel_button)
        )

    def _update_countdown(self) -> None:
        remaining = max(0, ceil(self._deadline - monotonic()))
        if remaining == 0:
            self._capture_timed_out(
                "Capture timed out. Choose Start capture or Retry to try again."
            )
        else:
            self._status.setText(f"Capturing controller shortcut… {remaining} seconds remaining.")

    def _capture_timed_out(self, message: str) -> None:
        self._stop_capture()
        self._status.setText(message)
        self._captured.setText(
            self._binding.display_label
            if self._has_candidate
            else f"Current shortcut: {self._binding.display_label}"
        )
        self._guard.begin(ModalInputSource.UNKNOWN)
        self._show_actions()

    def _stop_capture(self) -> None:
        self._capture_timer.stop()
        was_listening = self._listening
        self._listening = False
        self._attempt_id = None
        if was_listening:
            self._capture_callback(False)

    def _finish_session(self, _result: int = 0) -> None:
        self._stop_capture()
        if self._session_active:
            self._session_active = False
            if self._editing_callback is not None:
                self._editing_callback(False)

    def _apply(self) -> None:
        if not self._has_candidate or self._listening:
            return
        success, detail = self._apply_callback(self._binding)
        if success:
            self.accept()
            return
        self._error.setText(detail)
        self._error.show()


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
            self.trailing_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
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
        _SectionSpec(
            "Overlay",
            ("start_with_windows", "start_minimized", "run_in_background"),
        ),
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
        start_with_windows_enabled: bool = True,
        start_with_windows_available: bool = True,
        start_minimized_enabled: bool = True,
        start_minimized_available: bool = True,
        run_in_background_enabled: bool = True,
        run_in_background_available: bool = True,
        safe_mode_active: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settingsWidgetPage")
        self.setProperty("widgetId", definition.widget_id)
        self.setProperty("compactPage", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        expected = tuple(item_id for section in self._SECTIONS for item_id in section.item_ids)
        actual = tuple(item.item_id for item in definition.items)
        if actual != expected:
            raise ValueError("Settings widget item contract does not match section layout")

        self._buttons: list[QPushButton] = []
        self._buttons_by_item: dict[str, SettingsRowButton] = {}
        self._guide_button_enabled = guide_button_enabled
        self._controller_shortcut_binding = (
            controller_shortcut_binding or ControllerShortcutBinding()
        )
        self._active_dialog: (
            HotkeyEditorDialog | HotkeyFailureDialog | ControllerShortcutEditorDialog | None
        ) = None
        self._next_input_source = ModalInputSource.UNKNOWN
        self._allow_mouse_navigation_while_controller_connected = (
            allow_mouse_navigation_while_controller_connected
        )
        self._hotkey_combination = hotkey_combination
        self._start_with_windows_enabled = start_with_windows_enabled
        self._start_with_windows_available = start_with_windows_available
        self._start_minimized_enabled = start_minimized_enabled
        self._start_minimized_available = start_minimized_available
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

    def set_allow_mouse_navigation_while_controller_connected(self, enabled: bool) -> None:
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

    def set_start_minimized_enabled(self, enabled: bool) -> None:
        self._start_minimized_enabled = enabled
        row = self._buttons_by_item["start_minimized"]
        row.set_toggle_checked(enabled)
        row.update()

    def set_start_minimized_available(self, available: bool) -> None:
        self._start_minimized_available = available
        row = self._buttons_by_item["start_minimized"]
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
        row.setEnabled(True)
        if row.trailing_label is not None:
            row.trailing_label.setText("Turn off" if active else "Turn on")
        row.setAccessibleName(
            "Turn off Safe Mode and restart with saved settings"
            if active
            else "Turn on Safe Mode and restart with temporary defaults"
        )

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

    def set_controller_shortcut_binding(self, binding: ControllerShortcutBinding) -> None:
        self._controller_shortcut_binding = binding
        row = self._buttons_by_item["controller_shortcut"]
        if row.trailing_label is not None:
            row.trailing_label.setText(binding.display_label)

    def deliver_controller_shortcut(self, result: object) -> None:
        dialog = self._active_dialog
        if isinstance(dialog, ControllerShortcutEditorDialog) and isinstance(
            result, ControllerShortcutCaptureResult
        ):
            dialog.set_captured_binding(result)

    def cancel_active_dialog(self) -> None:
        if isinstance(self._active_dialog, HotkeyEditorDialog):
            failure = self._active_dialog._failure_dialog
            if failure is not None:
                failure.reject()
        if self._active_dialog is not None:
            self._active_dialog.reject()

    def open_hotkey_editor(
        self,
        apply_callback: HotkeyChangeCallback,
        *,
        capture_callback: HotkeyCaptureCallback | None = None,
        probe_callback: HotkeyProbeCallback | None = None,
    ) -> bool:
        dialog = HotkeyEditorDialog(
            self._hotkey_combination,
            apply_callback,
            probe_callback=probe_callback,
            parent=self,
        )
        self._active_dialog = dialog
        source = self._consume_input_source()
        dialog.begin_controller_ownership(source)
        if capture_callback is not None:
            capture_callback(True)
        dialog.begin_availability_checks()
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            self.set_hotkey_combination(dialog.combination)
            return True
        finally:
            self._active_dialog = None
            if capture_callback is not None:
                capture_callback(False)

    def show_hotkey_failure(
        self,
        candidate: str,
        detail: str,
        *,
        retry_callback: Callable[[], None] | None = None,
    ) -> bool:
        """Show one non-blocking startup failure prompt owned by Settings."""

        if self._active_dialog is not None:
            return False
        dialog = HotkeyFailureDialog(
            candidate,
            self._hotkey_combination,
            detail,
            self,
        )
        self._active_dialog = dialog
        dialog.begin_controller_ownership(ModalInputSource.UNKNOWN)

        def finished(result: int) -> None:
            if self._active_dialog is dialog:
                self._active_dialog = None
            dialog.deleteLater()
            if result == QDialog.DialogCode.Accepted and retry_callback is not None:
                QTimer.singleShot(0, retry_callback)

        dialog.finished.connect(finished)
        dialog.open()
        return True

    def open_controller_shortcut_editor(
        self,
        apply_callback: ControllerShortcutChangeCallback,
        capture_callback: ControllerShortcutCaptureCallback,
        editing_callback: ControllerShortcutEditingCallback | None = None,
    ) -> bool:
        dialog = ControllerShortcutEditorDialog(
            self._controller_shortcut_binding,
            apply_callback,
            capture_callback,
            self,
            editing_callback=editing_callback,
        )
        self._active_dialog = dialog
        dialog.begin_controller_ownership(self._consume_input_source())
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            self.set_controller_shortcut_binding(dialog.binding)
            return True
        finally:
            dialog._finish_session()
            self._active_dialog = None
            dialog.deleteLater()

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
        self.set_start_minimized_enabled(self._start_minimized_enabled)
        self.set_start_minimized_available(self._start_minimized_available)
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
        if item.item_id == "start_minimized":
            return SettingsRowButton(item, self, toggle=True)
        if item.item_id == "run_in_background":
            return SettingsRowButton(item, self, toggle=True)
        if item.item_id == "widgets":
            return SettingsRowButton(item, self, trailing_text=">>")
        if item.item_id == "safe_mode":
            return SettingsRowButton(item, self, trailing_text="Turn on")
        if item.item_id == "reset_window_position":
            return SettingsRowButton(item, self, trailing_text="Reset")
        raise ValueError(f"unsupported settings item: {item.item_id}")
