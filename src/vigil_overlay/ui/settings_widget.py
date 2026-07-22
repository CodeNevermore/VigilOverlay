"""Xbox Compact Mode-inspired Settings widget surface."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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

from vigil_overlay.core.hotkeys import parse_hotkey_combination
from vigil_overlay.ui.controls import VigilToggleSwitch
from vigil_overlay.widgets.registry import WidgetDefinition, WidgetItemDefinition

HotkeyChangeCallback = Callable[[str], tuple[bool, str]]
HotkeyCaptureCallback = Callable[[bool], None]


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
            self._show_error(str(exc))
            return

        success, detail = self._apply_callback(canonical)
        if not success:
            self._show_error(detail)
            return

        self._combination = canonical
        self.accept()

    def _show_error(self, detail: str) -> None:
        self.error_label.setText(detail)
        self.error_label.show()


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

    def open_hotkey_editor(
        self,
        apply_callback: HotkeyChangeCallback,
        *,
        capture_callback: HotkeyCaptureCallback | None = None,
    ) -> bool:
        dialog = HotkeyEditorDialog(self._hotkey_combination, apply_callback, self)
        if capture_callback is not None:
            capture_callback(True)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            self.set_hotkey_combination(dialog.combination)
            return True
        finally:
            if capture_callback is not None:
                capture_callback(False)

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

    def _create_row(self, item: WidgetItemDefinition) -> SettingsRowButton:
        if item.item_id == "guide_button":
            return SettingsRowButton(item, self, toggle=True)
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
