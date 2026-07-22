"""Xbox Compact Mode-inspired Display widget surface."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QResizeEvent, QScreen
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget

from vigil_overlay.services.display_configuration import (
    DisplayCapabilities,
    DisplayConfigurationBackend,
    DisplayConfigurationError,
    DisplayMode,
    ProjectionMode,
    create_display_configuration_backend,
)
from vigil_overlay.ui.controls import (
    SelectorToggleAction,
    VigilSelectorButton,
    repolish_widget,
    selector_toggle_action,
)
from vigil_overlay.ui.modal_guard import ModalActivationGuard, ModalInputSource
from vigil_overlay.ui.selector_popup import SelectorPopup
from vigil_overlay.widgets.registry import WidgetDefinition

_LOGGER = logging.getLogger("vigil_overlay")
_CONFIRMATION_SECONDS = 15


@dataclass(frozen=True)
class _Choice:
    label: str
    value: object


@dataclass(frozen=True)
class _PendingDisplayChange:
    kind: str
    previous: DisplayMode | ProjectionMode
    applied: DisplayMode | ProjectionMode


class DisplayWidgetView(QWidget):
    """Controller-first Display panel matching the Compact Mode visual hierarchy."""

    _EXPECTED_ITEMS = ("projection", "resolution", "refresh_rate")

    def __init__(
        self,
        definition: WidgetDefinition,
        parent: QWidget | None = None,
        *,
        backend: DisplayConfigurationBackend | None = None,
    ) -> None:
        super().__init__(parent)
        item_ids = tuple(item.item_id for item in definition.items)
        if item_ids != self._EXPECTED_ITEMS:
            raise ValueError(
                "Display widget requires projection, resolution, and refresh_rate items in order"
            )

        self.setObjectName("displayWidgetPage")
        self.setProperty("widgetId", definition.widget_id)
        self.setProperty("compactPage", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._backend = backend or create_display_configuration_backend()
        self._buttons: list[QPushButton] = []
        self._buttons_by_item: dict[str, QPushButton] = {}
        self._screen: QScreen | None = None
        self._display_name: str | None = None
        self._capabilities = DisplayCapabilities(None, (), None, ())

        self._choice_popup: SelectorPopup | None = None
        self._choices: tuple[_Choice, ...] = ()
        self._choice_item_id: str | None = None

        self._confirmation_popup: QFrame | None = None
        self._confirmation_label: QLabel | None = None
        self._confirmation_buttons: list[QPushButton] = []
        self._confirmation_index = 0
        self._confirmation_seconds = _CONFIRMATION_SECONDS
        self._pending_change: _PendingDisplayChange | None = None
        self._confirmation_timer = QTimer(self)
        self._confirmation_timer.setInterval(1000)
        self._confirmation_timer.timeout.connect(self._tick_confirmation)
        self._confirmation_guard = ModalActivationGuard(self)
        self._confirmation_guard.armed_changed.connect(self._update_confirmation_label)
        self._next_input_source = ModalInputSource.UNKNOWN

        self._build_ui(definition)
        self.refresh_screen_values(QGuiApplication.primaryScreen())

    @property
    def item_buttons(self) -> tuple[QPushButton, ...]:
        return tuple(self._buttons)

    @property
    def interaction_active(self) -> bool:
        return self._choice_popup is not None or self._confirmation_popup is not None

    @property
    def confirmation_active(self) -> bool:
        return self._confirmation_popup is not None

    @property
    def resolved_display_name(self) -> str | None:
        """Exact Win32 display device currently targeted by mode changes when available."""

        return self._display_name

    def refresh_screen_values(self, screen: QScreen | None) -> None:
        """Refresh current values and Windows-reported supported display choices."""

        self._screen = screen
        self._display_name = self._resolve_display_name(screen)
        if self._backend.available:
            try:
                self._capabilities = self._backend.capabilities(self._display_name)
            except DisplayConfigurationError as exc:
                _LOGGER.warning("Could not query Windows display capabilities: %s", exc)
                self._capabilities = DisplayCapabilities(None, (), None, ())
        else:
            self._capabilities = DisplayCapabilities(None, (), None, ())

        projection = self._capabilities.current_projection
        if projection is not None:
            self._buttons_by_item["projection"].setText(projection.label)
        else:
            screen_count = len(QGuiApplication.screens())
            self._buttons_by_item["projection"].setText(
                "PC screen only" if screen_count <= 1 else "Multiple displays"
            )

        mode = self._capabilities.current_mode
        if mode is not None:
            self._set_mode_text(mode)
            return

        if screen is None:
            self._buttons_by_item["resolution"].setText("Unavailable")
            self._buttons_by_item["refresh_rate"].setText("Unavailable")
            return

        size = screen.size()
        self._buttons_by_item["resolution"].setText(f"{size.width()} x {size.height()}")
        refresh = screen.refreshRate()
        self._buttons_by_item["refresh_rate"].setText(self._format_refresh(refresh))

    def toggle_selector(self, item_id: str) -> bool:
        """Open, close, or switch one backend-reported display selector."""

        if self._confirmation_popup is not None:
            return False
        if item_id not in self._EXPECTED_ITEMS:
            return False
        action = selector_toggle_action(self._choice_item_id, item_id)
        if action is SelectorToggleAction.CLOSE:
            self._close_choice_popup()
            return True
        self._close_choice_popup()
        self._display_name = self._resolve_display_name(self._screen)
        if self._backend.available:
            try:
                self._capabilities = self._backend.capabilities(self._display_name)
            except DisplayConfigurationError as exc:
                self._show_error(str(exc))
                return False
        choices = self._choices_for(item_id)
        if not choices:
            self._show_error("No supported choices are available for this display.")
            return False
        self._choice_item_id = item_id
        self._choices = choices
        anchor = self._buttons_by_item[item_id]
        popup = SelectorPopup(
            self,
            anchor=anchor,
            option_labels=tuple(choice.label for choice in choices),
            selected_index=self._current_choice_index(item_id, choices),
            object_prefix="display",
            option_selected=self._select_choice,
        )
        self._choice_popup = popup
        self._sync_selector_open_state()
        popup.show_anchored()
        return True

    def move_interaction(self, delta: int) -> bool:
        """Move inside an open dropdown/confirmation without changing shell focus."""

        if self._confirmation_popup is not None:
            count = len(self._confirmation_buttons)
            if count:
                self._confirmation_index = (self._confirmation_index + delta) % count
                self._sync_confirmation_focus()
            return True
        if self._choice_popup is not None:
            return self._choice_popup.move_selection(delta)
        return False

    def set_next_input_source(self, source: ModalInputSource) -> None:
        """Record the input family for the next interaction activation."""

        self._next_input_source = source

    def notify_controller_activation_released(self) -> None:
        """Arm a controller-opened confirmation after the initiating A press releases."""

        self._confirmation_guard.note_controller_activation_released()

    @property
    def confirmation_armed(self) -> bool:
        return self._confirmation_guard.armed

    def activate_interaction(self) -> bool:
        """Activate the selected dropdown option or Keep/Revert confirmation action."""

        source = self._consume_input_source()
        if self._confirmation_popup is not None:
            self._activate_confirmation_button(self._confirmation_index, source)
            return True
        if self._choice_popup is not None:
            self._select_choice(self._choice_popup.selected_index, source=source)
            return True
        return False

    def cancel_interaction(self) -> bool:
        """Close a dropdown, or safely revert a pending display change."""

        if self._confirmation_popup is not None:
            self.revert_pending_change()
            return True
        if self._choice_popup is not None:
            self._close_choice_popup()
            return True
        return False

    def revert_pending_change(self) -> None:
        """Restore the pre-change Windows display state and close confirmation UI."""

        pending = self._pending_change
        if pending is None:
            self._close_confirmation_popup()
            return
        try:
            if pending.kind == "projection":
                self._backend.apply_projection(self._as_projection(pending.previous))
            else:
                self._backend.apply_mode(
                    self._display_name, self._as_mode(pending.previous)
                )
        except DisplayConfigurationError as exc:
            _LOGGER.error("Could not revert temporary display change: %s", exc)
            self._show_error(f"Could not automatically revert display settings: {exc}")
        finally:
            self._pending_change = None
            self._close_confirmation_popup()
            QTimer.singleShot(350, self._refresh_after_change)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._choice_popup is not None:
            self._choice_popup.reposition()
        self._position_confirmation_popup()

    def _build_ui(self, definition: WidgetDefinition) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 28)
        layout.setSpacing(0)

        title = QLabel(definition.label, self)
        title.setObjectName("displayTitle")
        layout.addWidget(title)
        layout.addSpacing(28)

        section = QLabel("General", self)
        section.setObjectName("displaySectionLabel")
        layout.addWidget(section)
        layout.addSpacing(12)

        underline = QFrame(self)
        underline.setObjectName("displaySectionUnderline")
        underline.setFixedSize(116, 5)
        layout.addWidget(underline, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addSpacing(25)

        hint = QLabel("Changes will affect this screen only", self)
        hint.setObjectName("displayPageHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addSpacing(12)

        labels: Mapping[str, str] = {
            "projection": "Projection",
            "resolution": "Resolution",
            "refresh_rate": "Refresh Rate",
        }
        for index, item in enumerate(definition.items):
            field_label = QLabel(labels[item.item_id], self)
            field_label.setObjectName("displayFieldLabel")
            layout.addWidget(field_label)
            layout.addSpacing(7)

            button = VigilSelectorButton(self)
            button.setObjectName("displaySelectorButton")
            button.setProperty("itemId", item.item_id)
            button.setCheckable(False)
            button.setEnabled(item.enabled)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setToolTip(item.description)
            button.setAccessibleName(f"{labels[item.item_id]}. {item.description}")
            self._buttons.append(button)
            self._buttons_by_item[item.item_id] = button
            layout.addWidget(button)
            if index < len(definition.items) - 1:
                layout.addSpacing(13)

        self._error_label = QLabel(self)
        self._error_label.setObjectName("displayErrorLabel")
        self._error_label.setWordWrap(True)
        self._error_label.hide()
        layout.addSpacing(8)
        layout.addWidget(self._error_label)
        layout.addStretch(1)

    def _choices_for(self, item_id: str) -> tuple[_Choice, ...]:
        capabilities = self._capabilities
        if item_id == "projection":
            if capabilities.current_projection is None:
                return ()
            projections = capabilities.projections
            return tuple(_Choice(mode.label, mode) for mode in projections)

        current = capabilities.current_mode
        if current is None:
            return ()
        if item_id == "resolution":
            resolutions = {(mode.width, mode.height) for mode in capabilities.modes}
            ordered = sorted(
                resolutions,
                key=lambda value: (value[0] * value[1], value[0]),
                reverse=True,
            )
            return tuple(
                _Choice(f"{width} x {height}", (width, height))
                for width, height in ordered
            )

        rates = sorted(
            {
                mode.refresh_hz
                for mode in capabilities.modes
                if mode.width == current.width and mode.height == current.height
            },
            reverse=True,
        )
        return tuple(_Choice(self._format_refresh(rate), rate) for rate in rates)

    def _current_choice_index(self, item_id: str, choices: tuple[_Choice, ...]) -> int:
        current = self._capabilities.current_mode
        projection = self._capabilities.current_projection
        for index, choice in enumerate(choices):
            if item_id == "projection" and choice.value == projection:
                return index
            if current is None:
                continue
            if item_id == "resolution" and choice.value == (
                current.width,
                current.height,
            ):
                return index
            if (
                item_id == "refresh_rate"
                and isinstance(choice.value, float)
                and abs(choice.value - current.refresh_hz) < 0.05
            ):
                return index
        return 0

    def _select_choice(
        self,
        index: int,
        *,
        source: ModalInputSource = ModalInputSource.POINTER,
    ) -> None:
        if not 0 <= index < len(self._choices):
            return
        item_id = self._choice_item_id
        choice = self._choices[index]
        self._close_choice_popup()
        if item_id is None:
            return
        try:
            if item_id == "projection":
                self._apply_projection_choice(choice, source=source)
            elif item_id == "resolution":
                self._apply_resolution_choice(choice, source=source)
            else:
                self._apply_refresh_choice(choice, source=source)
        except DisplayConfigurationError as exc:
            _LOGGER.warning("Display setting selection failed: %s", exc)
            self._show_error(str(exc))

    def _apply_projection_choice(
        self, choice: _Choice, *, source: ModalInputSource
    ) -> None:
        current = self._capabilities.current_projection
        selected = self._as_projection(choice.value)
        if current is None:
            raise DisplayConfigurationError(
                "Windows could not determine the current projection, so Vigil will not "
                "apply an unsafe change"
            )
        if selected == current:
            return
        self._backend.apply_projection(selected)
        self._buttons_by_item["projection"].setText(selected.label)
        self._start_confirmation(
            _PendingDisplayChange("projection", current, selected), source=source
        )

    def _apply_resolution_choice(
        self, choice: _Choice, *, source: ModalInputSource
    ) -> None:
        current = self._require_current_mode()
        if not isinstance(choice.value, tuple) or len(choice.value) != 2:
            raise DisplayConfigurationError("Invalid resolution choice")
        width, height = choice.value
        candidates = [
            mode
            for mode in self._capabilities.modes
            if mode.width == width and mode.height == height
        ]
        if not candidates:
            raise DisplayConfigurationError(
                "Windows no longer reports that resolution as supported"
            )
        selected = min(
            candidates, key=lambda mode: abs(mode.refresh_hz - current.refresh_hz)
        )
        if selected == current:
            return
        self._backend.apply_mode(self._display_name, selected)
        self._set_mode_text(selected)
        self._start_confirmation(
            _PendingDisplayChange("mode", current, selected), source=source
        )

    def _apply_refresh_choice(
        self, choice: _Choice, *, source: ModalInputSource
    ) -> None:
        current = self._require_current_mode()
        if not isinstance(choice.value, (int, float)):
            raise DisplayConfigurationError("Invalid refresh-rate choice")
        selected = next(
            (
                mode
                for mode in self._capabilities.modes
                if mode.width == current.width
                and mode.height == current.height
                and abs(mode.refresh_hz - float(choice.value)) < 0.05
            ),
            None,
        )
        if selected is None:
            raise DisplayConfigurationError(
                "Windows no longer reports that refresh rate as supported"
            )
        if selected == current:
            return
        self._backend.apply_mode(self._display_name, selected)
        self._set_mode_text(selected)
        self._start_confirmation(
            _PendingDisplayChange("mode", current, selected), source=source
        )

    def _start_confirmation(
        self, pending: _PendingDisplayChange, *, source: ModalInputSource
    ) -> None:
        self._pending_change = pending
        self._confirmation_seconds = _CONFIRMATION_SECONDS
        self._confirmation_index = 0
        self._confirmation_guard.begin(source)

        popup = QFrame(self)
        popup.setObjectName("displayConfirmationPopup")
        popup.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(popup)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(9)

        title = QLabel("Keep these display settings?", popup)
        title.setObjectName("displayConfirmationTitle")
        layout.addWidget(title)
        self._confirmation_label = QLabel(popup)
        self._confirmation_label.setObjectName("displayConfirmationCountdown")
        self._confirmation_label.setWordWrap(True)
        layout.addWidget(self._confirmation_label)

        self._confirmation_buttons = []
        for index, label in enumerate(("Keep", "Revert")):
            button = QPushButton(label, popup)
            button.setObjectName("displayConfirmationButton")
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if index == 0:
                button.clicked.connect(
                    lambda checked=False: self._activate_confirmation_button(
                        0, ModalInputSource.POINTER
                    )
                )
            else:
                button.clicked.connect(
                    lambda checked=False: self._activate_confirmation_button(
                        1, ModalInputSource.POINTER
                    )
                )
            self._confirmation_buttons.append(button)
            layout.addWidget(button)

        self._confirmation_popup = popup
        self._update_confirmation_label()
        self._sync_confirmation_focus()
        popup.show()
        popup.raise_()
        self._position_confirmation_popup()
        self._confirmation_timer.start()

    def _position_confirmation_popup(self) -> None:
        popup = self._confirmation_popup
        if popup is None:
            return
        width = min(430, max(300, self.width() - 60))
        height = 210
        popup.setGeometry(
            (self.width() - width) // 2, (self.height() - height) // 2, width, height
        )

    def _sync_confirmation_focus(self) -> None:
        for index, button in enumerate(self._confirmation_buttons):
            active = index == self._confirmation_index
            button.setProperty("navigationFocus", active)
            repolish_widget(button)

    def _tick_confirmation(self) -> None:
        if self._pending_change is None:
            self._confirmation_timer.stop()
            return
        self._confirmation_seconds -= 1
        if self._confirmation_seconds <= 0:
            self.revert_pending_change()
            return
        self._update_confirmation_label()

    def _update_confirmation_label(self, _armed: bool | None = None) -> None:
        if self._confirmation_label is not None:
            guard_text = (
                " Release the controller A button to enable Keep/Revert."
                if not self._confirmation_guard.armed
                and self._confirmation_guard.source is ModalInputSource.CONTROLLER
                else ""
            )
            self._confirmation_label.setText(
                f"Reverting automatically in {self._confirmation_seconds} seconds.{guard_text}"
            )

    def _activate_confirmation_button(
        self, index: int, source: ModalInputSource
    ) -> None:
        del source
        if not self._confirmation_guard.accepts_activation():
            return
        if index == 0:
            self._keep_pending_change()
        else:
            self.revert_pending_change()

    def _consume_input_source(self) -> ModalInputSource:
        source = self._next_input_source
        self._next_input_source = ModalInputSource.UNKNOWN
        return source

    def _keep_pending_change(self) -> None:
        pending = self._pending_change
        if pending is None:
            self._close_confirmation_popup()
            return
        try:
            if pending.kind == "projection":
                self._backend.commit_projection(self._as_projection(pending.applied))
            else:
                self._backend.commit_mode(
                    self._display_name, self._as_mode(pending.applied)
                )
        except DisplayConfigurationError as exc:
            _LOGGER.error("Could not persist display change; reverting: %s", exc)
            self._show_error(f"Could not keep display settings; reverting: {exc}")
            self.revert_pending_change()
            return
        self._pending_change = None
        self._close_confirmation_popup()
        QTimer.singleShot(350, self._refresh_after_change)

    def _close_choice_popup(self) -> None:
        popup = self._choice_popup
        self._choice_popup = None
        self._choices = ()
        self._choice_item_id = None
        self._sync_selector_open_state()
        if popup is not None:
            popup.dispose()

    def _sync_selector_open_state(self) -> None:
        for item_id, button in self._buttons_by_item.items():
            if isinstance(button, VigilSelectorButton):
                button.set_selector_open(
                    self._choice_popup is not None and self._choice_item_id == item_id
                )

    def _close_confirmation_popup(self) -> None:
        self._confirmation_timer.stop()
        self._confirmation_guard.end()
        popup = self._confirmation_popup
        self._confirmation_popup = None
        self._confirmation_label = None
        self._confirmation_buttons = []
        if popup is not None:
            popup.hide()
            popup.deleteLater()

    def _refresh_after_change(self) -> None:
        screen = self._screen
        if screen is None or screen not in QGuiApplication.screens():
            screen = QGuiApplication.primaryScreen()
        self.refresh_screen_values(screen)

    def _require_current_mode(self) -> DisplayMode:
        current = self._capabilities.current_mode
        if current is None:
            raise DisplayConfigurationError(
                "Windows did not report a current display mode"
            )
        return current

    def _set_mode_text(self, mode: DisplayMode) -> None:
        self._buttons_by_item["resolution"].setText(mode.resolution_label)
        self._buttons_by_item["refresh_rate"].setText(mode.refresh_label)

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
        QTimer.singleShot(7000, self._error_label.hide)

    def _resolve_display_name(self, screen: QScreen | None) -> str | None:
        fallback_name = self._screen_name(screen)
        window_handle = self._top_level_window_handle()
        try:
            resolved = self._backend.resolve_display_name(window_handle, fallback_name)
        except DisplayConfigurationError as exc:
            _LOGGER.warning("Could not resolve active Windows display target: %s", exc)
            return fallback_name
        if resolved != fallback_name:
            _LOGGER.info(
                "Display widget target resolved from Qt screen %r to native device %r",
                fallback_name,
                resolved,
            )
        return resolved

    def _top_level_window_handle(self) -> int | None:
        top_level = self.window()
        if top_level is None:
            return None
        try:
            handle = int(top_level.effectiveWinId())
        except (RuntimeError, TypeError, ValueError):
            return None
        return handle or None

    @staticmethod
    def _screen_name(screen: QScreen | None) -> str | None:
        if screen is None:
            return None
        name = screen.name().strip()
        return name or None

    @staticmethod
    def _format_refresh(refresh: float) -> str:
        if refresh <= 0:
            return "Unavailable"
        if abs(refresh - round(refresh)) < 0.05:
            return f"{round(refresh)} Hz"
        return f"{refresh:.2f} Hz"

    @staticmethod
    def _as_mode(value: object) -> DisplayMode:
        if not isinstance(value, DisplayMode):
            raise DisplayConfigurationError("Invalid display mode state")
        return value

    @staticmethod
    def _as_projection(value: object) -> ProjectionMode:
        if not isinstance(value, ProjectionMode):
            raise DisplayConfigurationError("Invalid projection state")
        return value
