"""Controller-accessible FPS target selection and remembered-game corrections."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PureWindowsPath

from PySide6.QtWidgets import QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from vigil_overlay.services.fps import FpsSelectionSnapshot, FpsTarget
from vigil_overlay.ui.dialog_surface import ControllerVigilDialog

FpsSnapshotCallback = Callable[[], FpsSelectionSnapshot]
FpsActionCallback = Callable[[str, FpsTarget | str | None], tuple[bool, str]]


class FpsOptionsDialog(ControllerVigilDialog):
    """Keep process choices temporary and executable corrections explicit."""

    def __init__(
        self,
        snapshot_callback: FpsSnapshotCallback,
        action_callback: FpsActionCallback,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("FPS games", parent, width=520)
        self.setObjectName("fpsOptionsDialog")
        self._snapshot_callback = snapshot_callback
        self._action_callback = action_callback
        self._saved_page = 0
        self.add_title("FPS games")
        self.add_message(
            "Choose a running game or use automatic selection. A manual choice lasts until "
            "that process exits. Programs are remembered automatically after FPS verifies; "
            "ignore apps you do not want tracked."
        )
        self._status = self.add_detail("")
        self._error = self.add_error()
        self._error.hide()
        self._scroll = QScrollArea(self.surface)
        self._scroll.setWidgetResizable(True)
        self._scroll.setMinimumHeight(220)
        self._scroll.setMaximumHeight(300)
        self.content_layout.addWidget(self._scroll)
        self._refresh_button = QPushButton("Refresh running games", self.surface)
        self.style_button(self._refresh_button)
        self._refresh_button.clicked.connect(self.refresh)
        self.content_layout.addWidget(self._refresh_button)
        self._close_button = QPushButton("Close", self.surface)
        self.style_button(self._close_button)
        self._close_button.clicked.connect(self.reject)
        self.content_layout.addWidget(self._close_button)
        self.refresh()

    def refresh(self) -> None:
        try:
            snapshot = self._snapshot_callback()
        except OSError:
            self._error.setText("Running games could not be refreshed. Try again.")
            self._error.show()
            self.set_controller_buttons((self._refresh_button, self._close_button))
            return
        self._error.hide()
        self.set_controller_buttons(())
        old = self._scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        content = QWidget(self._scroll)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(6)
        buttons: list[QPushButton] = []
        mode = "Manual" if snapshot.manual_target is not None else "Automatic"
        active = snapshot.active_target
        self._status.setText(
            f"{mode} · {active.executable_name if active is not None else 'Finding game'}"
        )

        def add_action(
            label: str, action: str, value: FpsTarget | str | None = None, *, enabled: bool = True
        ) -> None:
            button = QPushButton(label, content)
            button.setProperty("fpsAction", action)
            button.setEnabled(enabled)
            self.style_button(button, kind="row")
            if isinstance(value, FpsTarget):
                button.setProperty("processId", value.process_id)
                button.setToolTip(value.executable_path or value.executable_name)
            elif isinstance(value, str):
                button.setProperty("executablePath", value)
                button.setToolTip(value)
            button.clicked.connect(
                lambda checked=False, selected_action=action, selected_value=value: self._apply(
                    selected_action, selected_value
                )
            )
            layout.addWidget(button)
            if enabled:
                buttons.append(button)

        add_action("Automatic selection", "automatic")
        add_action("Remember current game", "remember", enabled=snapshot.can_remember)
        for option in snapshot.candidates:
            target = option.target
            add_action(
                f"{target.executable_name} (PID {target.process_id})\n{option.reason}",
                "select",
                target,
            )
            if target.executable_path is not None:
                path_label = QLabel(target.executable_path, content)
                path_label.setWordWrap(True)
                layout.addWidget(path_label)
                add_action(f"Ignore {target.executable_name}", "ignore", target.executable_path)
        if not snapshot.candidates:
            layout.addWidget(QLabel("No eligible running game or app was found.", content))
        page_size = 8
        page_count = max(
            1, (max(len(snapshot.learned_paths), len(snapshot.ignored_paths)) + 7) // 8
        )
        self._saved_page %= page_count
        page_start = self._saved_page * page_size
        for heading, paths, actions in (
            ("Remembered games", snapshot.learned_paths, ("forget", "ignore")),
            ("Ignored apps", snapshot.ignored_paths, ("restore",)),
        ):
            if paths:
                layout.addWidget(QLabel(heading, content))
            for path in paths[page_start : page_start + page_size]:
                label = QLabel(path, content)
                label.setWordWrap(True)
                layout.addWidget(label)
                for action in actions:
                    add_action(f"{action.title()} {PureWindowsPath(path).name}", action, path)
        if page_count > 1:
            more = QPushButton(
                f"More saved games / ignored apps ({self._saved_page + 1}/{page_count})", content
            )
            self.style_button(more, kind="row")
            more.clicked.connect(self._next_saved_page)
            layout.addWidget(more)
            buttons.append(more)
        layout.addStretch(1)
        self._scroll.setWidget(content)
        self.set_controller_buttons((*buttons, self._refresh_button, self._close_button))
        self._guard.begin(self._guard.source)

    def _next_saved_page(self) -> None:
        self._saved_page += 1
        self.refresh()

    def _apply(self, action: str, value: FpsTarget | str | None) -> None:
        try:
            success, detail = self._action_callback(action, value)
        except OSError:
            success, detail = (
                False,
                "This change could not be saved. Your existing choice is unchanged.",
            )
        if success:
            self._error.hide()
            self.refresh()
            self._status.setText(detail)
        else:
            self._error.setText(detail)
            self._error.show()

    def sync_controller_focus(self) -> None:
        super().sync_controller_focus()
        if self._controller_buttons:
            button = self._controller_buttons[self._controller_index]
            content = self._scroll.widget()
            if content is not None and content.isAncestorOf(button):
                self._scroll.ensureWidgetVisible(button)


__all__ = ["FpsActionCallback", "FpsOptionsDialog", "FpsSnapshotCallback"]
