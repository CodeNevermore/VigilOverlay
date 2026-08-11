"""Xbox Compact Mode-inspired first-party Audio widget surface."""

from __future__ import annotations

import hashlib
import logging

from PySide6.QtCore import QFileInfo, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFileIconProvider,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from vigil_overlay.services.audio_control import (
    AudioControlBackend,
    AudioDeviceInfo,
    AudioSessionInfo,
    AudioSnapshot,
)
from vigil_overlay.services.audio_runtime import AudioControlRuntime
from vigil_overlay.ui.controls import (
    SelectorToggleAction,
    VigilSelectorButton,
    VigilToggleSwitch,
    repolish_widget,
    selector_toggle_action,
)
from vigil_overlay.ui.selector_popup import SelectorPopup
from vigil_overlay.widgets.registry import WidgetDefinition, WidgetItemDefinition

_LOGGER = logging.getLogger("vigil_overlay")
_VOLUME_STEP = 5
_REFRESH_MS = 1250


class AudioToggleButton(QPushButton):
    """Large controller-focusable mute toggle."""

    def __init__(self, item: WidgetItemDefinition, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("audioToggleButton")
        self.setProperty("itemId", item.item_id)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(item.description)
        self._label = item.label

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(12)
        label = QLabel(item.label, self)
        label.setObjectName("toggleRowTitle")
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(label, 1)
        self.toggle_switch = VigilToggleSwitch(self)
        layout.addWidget(self.toggle_switch, 0, Qt.AlignmentFlag.AlignVCenter)
        self.set_state(False)

    def set_state(self, muted: bool) -> None:
        enabled = not muted
        self.toggle_switch.setChecked(enabled)
        self.setProperty("muted", muted)
        self.setAccessibleName(f"{self._label}: {'On' if enabled else 'Off'}")
        repolish_widget(self)


class AudioVolumeButton(QPushButton):
    """Controller-focusable volume row with a mouse-adjustable slider."""

    volume_changed = Signal(int)

    def __init__(
        self,
        item: WidgetItemDefinition,
        parent: QWidget,
        *,
        icon: QIcon | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("audioVolumeRowButton")
        self.setProperty("itemId", item.item_id)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(item.description)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        if icon is not None and not icon.isNull():
            icon_label = QLabel(self)
            icon_label.setObjectName("audioSessionIcon")
            icon_label.setPixmap(icon.pixmap(34, 34))
            icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            layout.addWidget(icon_label)

        body = QWidget(self)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        title = QLabel(item.label, body)
        title.setObjectName("audioVolumeRowTitle")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._value_label = QLabel("0", body)
        self._value_label.setObjectName("audioVolumeValue")
        self._value_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        top.addWidget(title, 1)
        top.addWidget(self._value_label)
        self._slider = QSlider(Qt.Orientation.Horizontal, body)
        self._slider.setObjectName("audioVolumeSlider")
        self._slider.setRange(0, 100)
        self._slider.setSingleStep(1)
        self._slider.setPageStep(5)
        self._slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._slider.valueChanged.connect(self.volume_changed.emit)
        body_layout.addLayout(top)
        body_layout.addWidget(self._slider)
        layout.addWidget(body, 1)
        self._title = title

    @property
    def slider(self) -> QSlider:
        return self._slider

    def set_volume_state(self, percent: int, muted: bool) -> None:
        value = min(max(int(percent), 0), 100)
        blocker = QSignalBlocker(self._slider)
        self._slider.setValue(value)
        del blocker
        self._value_label.setText(f"{value}%")
        self.setProperty("muted", muted)
        suffix = "Muted" if muted else f"{value}%"
        self.setAccessibleName(f"{self._title.text()}: {suffix}")
        repolish_widget(self)


class AudioSelectorButton(VigilSelectorButton):
    """Default-device selector row."""

    def __init__(self, item: WidgetItemDefinition, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("audioSelectorButton")
        self.setProperty("itemId", item.item_id)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(item.description)
        self.setText("Unavailable")


class AudioWidgetView(QWidget):
    """Controller-first Audio surface with endpoint and per-app volume controls."""

    items_changed = Signal(object, object)

    _OUTPUT_IDS = frozenset(("output_mute", "output_volume", "output_device"))
    _INPUT_IDS = frozenset(("input_mute", "input_volume", "input_device"))
    _FIXED_IDS = (
        "output_mute",
        "input_mute",
        "output_volume",
        "input_volume",
        "output_device",
        "input_device",
    )

    def __init__(
        self,
        definition: WidgetDefinition,
        parent: QWidget | None = None,
        *,
        backend: AudioControlBackend | None = None,
        runtime: AudioControlRuntime | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("audioWidgetPage")
        self.setProperty("widgetId", definition.widget_id)
        self.setProperty("compactPage", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        actual = tuple(item.item_id for item in definition.items)
        if actual != self._FIXED_IDS:
            raise ValueError("Audio widget item contract does not match the fixed control layout")

        self._base_definition = definition
        self._item_definitions: tuple[WidgetItemDefinition, ...] = definition.items
        if runtime is not None and backend is not None:
            raise ValueError("audio view accepts either backend or runtime, not both")
        if runtime is not None:
            self._runtime = runtime
        elif backend is not None:
            self._runtime = AudioControlRuntime(self, backend_factory=lambda: backend)
        else:
            self._runtime = AudioControlRuntime(self)
        self._snapshot = AudioSnapshot(False, "Audio controls have not been queried yet.")
        self._buttons: list[QPushButton] = []
        self._buttons_by_item: dict[str, QPushButton] = {}
        self._session_backend_ids: dict[str, str] = {}
        self._session_rows: dict[str, AudioVolumeButton] = {}
        self._choice_popup: SelectorPopup | None = None
        self._choice_devices: tuple[AudioDeviceInfo, ...] = ()
        self._choice_kind: str | None = None
        self._error_label: QLabel
        self._mixer_layout: QVBoxLayout
        self._mixer_container: QWidget
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(_REFRESH_MS)
        self._refresh_timer.timeout.connect(self.refresh)
        self._latest_requested_generation = 0
        self._latest_applied_generation = 0
        self._last_logged_error: str | None = None
        self._runtime.snapshot_ready.connect(self._on_snapshot_ready)
        self._runtime.error_ready.connect(self._show_error)
        self._build_ui(definition)
        self._apply_snapshot(self._snapshot)
        self.refresh()

    @property
    def item_buttons(self) -> tuple[QPushButton, ...]:
        return tuple(self._buttons)

    @property
    def item_definitions(self) -> tuple[WidgetItemDefinition, ...]:
        return self._item_definitions

    @property
    def interaction_active(self) -> bool:
        return self._choice_popup is not None

    def showEvent(self, event: object) -> None:
        super().showEvent(event)  # type: ignore[arg-type]
        # Do not block top-level controller navigation on a synchronous Core Audio
        # snapshot. The queued refresh runs after the navigation command returns,
        # while the periodic timer keeps the visible page current afterward.
        QTimer.singleShot(0, self.refresh)
        self._refresh_timer.start()

    def hideEvent(self, event: object) -> None:
        self._refresh_timer.stop()
        self.cancel_interaction()
        super().hideEvent(event)  # type: ignore[arg-type]

    def refresh(self) -> None:
        self._latest_requested_generation = self._runtime.request_refresh()

    def _on_snapshot_ready(self, generation: int, snapshot: object) -> None:
        if not isinstance(snapshot, AudioSnapshot):
            return
        if generation < self._latest_applied_generation:
            return
        self._latest_applied_generation = generation
        self._apply_snapshot(snapshot)

    def activate_item(self, item_id: str) -> bool:
        if not self._snapshot.available:
            return False
        if (
            item_id in self._OUTPUT_IDS
            and not self._snapshot.output_endpoint_available
        ):
            return False
        if item_id in self._INPUT_IDS and not self._snapshot.input_endpoint_available:
            return False
        if item_id == "output_mute":
            self._runtime.submit("set_output_muted", not self._snapshot.output_muted)
        elif item_id == "input_mute":
            self._runtime.submit("set_input_muted", not self._snapshot.input_muted)
        elif item_id in {"output_device", "input_device"}:
            return self.toggle_selector(item_id)
        elif item_id in self._session_backend_ids:
            session = self._session_for_item(item_id)
            if session is None:
                return False
            self._runtime.submit("set_session_muted", session.session_id, not session.muted)
        else:
            return False
        return True

    def adjust_item(self, item_id: str, delta: int) -> bool:
        if not self._snapshot.available or delta == 0:
            return False
        if (
            item_id in self._OUTPUT_IDS
            and not self._snapshot.output_endpoint_available
        ):
            return False
        if item_id in self._INPUT_IDS and not self._snapshot.input_endpoint_available:
            return False
        amount = _VOLUME_STEP if delta > 0 else -_VOLUME_STEP
        if item_id == "output_volume":
            self._runtime.submit("set_output_volume", self._snapshot.output_volume_percent + amount)
        elif item_id == "input_volume":
            self._runtime.submit("set_input_volume", self._snapshot.input_volume_percent + amount)
        elif item_id in self._session_backend_ids:
            session = self._session_for_item(item_id)
            if session is None:
                return False
            self._runtime.submit(
                "set_session_volume",
                session.session_id,
                session.volume_percent + amount,
            )
        else:
            return False
        return True

    def _set_direct_volume(self, item_id: str, percent: int) -> None:
        if not self._snapshot.available:
            return
        if (
            item_id in self._OUTPUT_IDS
            and not self._snapshot.output_endpoint_available
        ):
            return
        if item_id in self._INPUT_IDS and not self._snapshot.input_endpoint_available:
            return
        if item_id == "output_volume":
            self._runtime.submit("set_output_volume", percent)
        elif item_id == "input_volume":
            self._runtime.submit("set_input_volume", percent)
        elif item_id in self._session_backend_ids:
            session = self._session_for_item(item_id)
            if session is None:
                return
            self._runtime.submit("set_session_volume", session.session_id, percent)

    def move_interaction(self, delta: int) -> bool:
        popup = self._choice_popup
        return popup.move_selection(delta) if popup is not None else False

    def activate_interaction(self) -> bool:
        popup = self._choice_popup
        return popup.activate_selection() if popup is not None else False

    def cancel_interaction(self) -> bool:
        if self._choice_popup is None:
            return False
        self._close_choice_popup()
        return True

    def _build_ui(self, definition: WidgetDefinition) -> None:
        items = {item.item_id: item for item in definition.items}
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 22)
        root.setSpacing(10)

        title = QLabel(definition.label, self)
        title.setObjectName("audioTitle")
        root.addWidget(title)

        toggles = QHBoxLayout()
        toggles.setSpacing(10)
        output_mute = AudioToggleButton(items["output_mute"], self)
        input_mute = AudioToggleButton(items["input_mute"], self)
        toggles.addWidget(output_mute, 1)
        toggles.addWidget(input_mute, 1)
        root.addLayout(toggles)
        self._register_fixed_button("output_mute", output_mute)
        self._register_fixed_button("input_mute", input_mute)

        output_volume = AudioVolumeButton(items["output_volume"], self)
        input_volume = AudioVolumeButton(items["input_volume"], self)
        output_volume.volume_changed.connect(
            lambda value: self._set_direct_volume("output_volume", value)
        )
        input_volume.volume_changed.connect(
            lambda value: self._set_direct_volume("input_volume", value)
        )
        root.addWidget(output_volume)
        root.addWidget(input_volume)
        self._register_fixed_button("output_volume", output_volume)
        self._register_fixed_button("input_volume", input_volume)

        root.addWidget(self._section_label("Default devices"))
        output_device = AudioSelectorButton(items["output_device"], self)
        input_device = AudioSelectorButton(items["input_device"], self)
        root.addWidget(output_device)
        root.addWidget(input_device)
        self._register_fixed_button("output_device", output_device)
        self._register_fixed_button("input_device", input_device)

        root.addWidget(self._section_label("Volume mixer"))
        self._mixer_container = QWidget(self)
        self._mixer_layout = QVBoxLayout(self._mixer_container)
        self._mixer_layout.setContentsMargins(0, 0, 0, 0)
        self._mixer_layout.setSpacing(6)
        root.addWidget(self._mixer_container)

        self._error_label = QLabel(self)
        self._error_label.setObjectName("audioErrorLabel")
        self._error_label.setWordWrap(True)
        self._error_label.hide()
        root.addWidget(self._error_label)
        root.addStretch(1)

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text, self)
        label.setObjectName("audioSectionLabel")
        return label

    def _register_fixed_button(self, item_id: str, button: QPushButton) -> None:
        self._buttons.append(button)
        self._buttons_by_item[item_id] = button

    def _apply_snapshot(self, snapshot: AudioSnapshot) -> None:
        self._snapshot = snapshot
        if not snapshot.available:
            self._show_error(snapshot.detail)
            for button in self._buttons:
                button.setEnabled(False)
            return
        self._error_label.hide()
        self._last_logged_error = None
        for button in self._buttons:
            button.setEnabled(True)
        for item_id in self._OUTPUT_IDS:
            self._buttons_by_item[item_id].setEnabled(
                snapshot.output_endpoint_available
            )
        for item_id in self._INPUT_IDS:
            self._buttons_by_item[item_id].setEnabled(
                snapshot.input_endpoint_available
            )
        if (
            self._choice_kind == "output"
            and not snapshot.output_endpoint_available
        ) or (
            self._choice_kind == "input" and not snapshot.input_endpoint_available
        ):
            self._close_choice_popup()

        output_mute = self._buttons_by_item["output_mute"]
        input_mute = self._buttons_by_item["input_mute"]
        output_volume = self._buttons_by_item["output_volume"]
        input_volume = self._buttons_by_item["input_volume"]
        if isinstance(output_mute, AudioToggleButton):
            output_mute.set_state(snapshot.output_muted)
        if isinstance(input_mute, AudioToggleButton):
            input_mute.set_state(snapshot.input_muted)
        if isinstance(output_volume, AudioVolumeButton):
            output_volume.set_volume_state(snapshot.output_volume_percent, snapshot.output_muted)
        if isinstance(input_volume, AudioVolumeButton):
            input_volume.set_volume_state(snapshot.input_volume_percent, snapshot.input_muted)

        output_selector = self._buttons_by_item["output_device"]
        input_selector = self._buttons_by_item["input_device"]
        output_selector.setText(
            _device_name(snapshot.output_devices, snapshot.default_output_device_id)
            or "No output device"
        )
        input_selector.setText(
            _device_name(snapshot.input_devices, snapshot.default_input_device_id)
            or "No input device"
        )
        self._sync_sessions(snapshot.sessions)

    def _sync_sessions(self, sessions: tuple[AudioSessionInfo, ...]) -> None:
        next_ids = tuple(_session_item_id(session.session_id) for session in sessions)
        current_ids = tuple(self._session_rows)
        if next_ids != current_ids:
            self._rebuild_session_rows(sessions)
        for session in sessions:
            item_id = _session_item_id(session.session_id)
            row = self._session_rows.get(item_id)
            if row is not None:
                row.set_volume_state(session.volume_percent, session.muted)

    def _rebuild_session_rows(self, sessions: tuple[AudioSessionInfo, ...]) -> None:
        while self._mixer_layout.count():
            item = self._mixer_layout.takeAt(0)
            if item is None:
                break
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._session_backend_ids.clear()
        self._session_rows.clear()
        fixed_buttons = [self._buttons_by_item[item_id] for item_id in self._FIXED_IDS]
        self._buttons = list(fixed_buttons)
        dynamic_items: list[WidgetItemDefinition] = []
        icon_provider = QFileIconProvider()
        for session in sessions:
            item_id = _session_item_id(session.session_id)
            dynamic = WidgetItemDefinition(
                item_id=item_id,
                label=session.name,
                description="Adjust this app's audio volume with Left/Right; press A to mute.",
                icon_key="audio",
                icon_path=session.process_path,
            )
            icon: QIcon | None = None
            if session.process_path:
                icon = icon_provider.icon(QFileInfo(session.process_path))
            row = AudioVolumeButton(dynamic, self._mixer_container, icon=icon)
            row.volume_changed.connect(
                lambda value, target_item_id=item_id: self._set_direct_volume(target_item_id, value)
            )
            self._mixer_layout.addWidget(row)
            self._session_backend_ids[item_id] = session.session_id
            self._session_rows[item_id] = row
            self._buttons.append(row)
            dynamic_items.append(dynamic)

        if not sessions:
            empty = QLabel("No active app audio sessions.", self._mixer_container)
            empty.setObjectName("audioEmptyMixer")
            self._mixer_layout.addWidget(empty)

        self._item_definitions = (*self._base_definition.items, *dynamic_items)
        self.items_changed.emit(self._item_definitions, self.item_buttons)

    def _session_for_item(self, item_id: str) -> AudioSessionInfo | None:
        backend_id = self._session_backend_ids.get(item_id)
        if backend_id is None:
            return None
        for session in self._snapshot.sessions:
            if session.session_id == backend_id:
                return session
        return None

    def toggle_selector(self, item_id: str) -> bool:
        """Open, close, or switch one default-device selector."""

        kind_by_item = {"output_device": "output", "input_device": "input"}
        kind = kind_by_item.get(item_id)
        if kind is None:
            return False
        open_item_id = (
            "output_device"
            if self._choice_kind == "output"
            else "input_device"
            if self._choice_kind == "input"
            else None
        )
        action = selector_toggle_action(open_item_id, item_id)
        if action is SelectorToggleAction.CLOSE:
            self._close_choice_popup()
            return True
        if action is SelectorToggleAction.SWITCH:
            # Close the previous popup before checking the new target. This prevents
            # a stale popup when the requested selector currently has no devices.
            self._close_choice_popup()

        devices = (
            self._snapshot.output_devices if kind == "output" else self._snapshot.input_devices
        )
        default_id = (
            self._snapshot.default_output_device_id
            if kind == "output"
            else self._snapshot.default_input_device_id
        )
        if not devices:
            self._show_error("No active audio devices are available.")
            return True
        self._choice_kind = kind
        self._choice_devices = devices
        selected_index = next(
            (index for index, device in enumerate(devices) if device.device_id == default_id),
            0,
        )
        item_id = "output_device" if kind == "output" else "input_device"
        anchor = self._buttons_by_item[item_id]
        popup = SelectorPopup(
            self,
            anchor=anchor,
            option_labels=tuple(device.name for device in devices),
            selected_index=selected_index,
            object_prefix="audio",
            option_selected=self._select_device,
        )
        self._choice_popup = popup
        self._sync_selector_open_state()
        popup.show_anchored()
        return True

    def _select_device(self, index: int) -> None:
        if not 0 <= index < len(self._choice_devices):
            return
        device = self._choice_devices[index]
        kind = self._choice_kind
        self._close_choice_popup()
        if kind == "output":
            self._runtime.submit("set_default_output_device", device.device_id)
        elif kind == "input":
            self._runtime.submit("set_default_input_device", device.device_id)

    def _close_choice_popup(self) -> None:
        popup = self._choice_popup
        self._choice_popup = None
        self._choice_devices = ()
        self._choice_kind = None
        self._sync_selector_open_state()
        if popup is not None:
            popup.dispose()

    def _sync_selector_open_state(self) -> None:
        for item_id, kind in (("output_device", "output"), ("input_device", "input")):
            button = self._buttons_by_item.get(item_id)
            if isinstance(button, VigilSelectorButton):
                button.set_selector_open(
                    self._choice_popup is not None and self._choice_kind == kind
                )

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
        if message != self._last_logged_error:
            _LOGGER.warning("Audio widget unavailable: %s", message)
            self._last_logged_error = message


def _device_name(devices: tuple[AudioDeviceInfo, ...], device_id: str | None) -> str | None:
    if device_id is None:
        return None
    return next((device.name for device in devices if device.device_id == device_id), None)


def _session_item_id(session_id: str) -> str:
    digest = hashlib.sha256(session_id.encode("utf-8", errors="replace")).hexdigest()[:20]
    return f"audio_session_{digest}"
