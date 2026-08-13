"""Controller-ready widget-strip navigation model and host-owned panel UI."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

from PySide6.QtCore import QEvent, QFileInfo, QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QIcon,
    QKeyEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QFileIconProvider,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from vigil_overlay.core.controller_shortcuts import ControllerShortcutBinding
from vigil_overlay.services.telemetry import TelemetrySnapshot
from vigil_overlay.ui.audio_widget import AudioWidgetView
from vigil_overlay.ui.display_widget import DisplayWidgetView
from vigil_overlay.ui.integrations_widget import IntegrationsWidgetView
from vigil_overlay.ui.performance_widget import PerformanceWidgetView
from vigil_overlay.ui.scrollbars import (
    VigilVerticalScrollBar,
    ensure_controller_target_visible,
)
from vigil_overlay.ui.settings_widget import SettingsWidgetView
from vigil_overlay.ui.wifi_widget import WifiWidgetView
from vigil_overlay.widgets.registry import (
    WidgetDefinition,
    WidgetItemDefinition,
    WidgetViewKind,
)

_WIDGET_STRIP_VISIBLE_SLOTS = 6
_WIDGET_STRIP_BUTTON_SIZE = 62
_WIDGET_STRIP_SPACING = 10
_WIDGET_STRIP_SLOT_WIDTH = _WIDGET_STRIP_BUTTON_SIZE + _WIDGET_STRIP_SPACING
_BRAND_ICON_SIZE = 32
_BRAND_HEADER_HEIGHT = 40
_WIDGET_STRIP_HEIGHT = 72
_WIDGET_STRIP_OVERFLOW_SLOT_WIDTH = 46
_WIDGET_STRIP_OVERFLOW_INDICATOR_WIDTH = 40


def _widget_strip_viewport_width(visible_count: int) -> int:
    slots = min(max(visible_count, 1), _WIDGET_STRIP_VISIBLE_SLOTS)
    return (slots * _WIDGET_STRIP_BUTTON_SIZE) + ((slots - 1) * _WIDGET_STRIP_SPACING)


class WidgetStripScrollArea(QScrollArea):
    """Hidden-scrollbar strip viewport that pages by complete widget slots."""

    def scroll_widget_slots(self, slots: int) -> None:
        if slots == 0:
            return
        bar = self.horizontalScrollBar()
        current_slot = round(bar.value() / _WIDGET_STRIP_SLOT_WIDTH)
        bar.setValue((current_slot + slots) * _WIDGET_STRIP_SLOT_WIDTH)

    def wheelEvent(self, event: QWheelEvent) -> None:
        pixel_delta = event.pixelDelta()
        angle_delta = event.angleDelta()
        horizontal = pixel_delta.x() or angle_delta.x()
        vertical = pixel_delta.y() or angle_delta.y()
        dominant = horizontal if abs(horizontal) >= abs(vertical) else vertical
        if dominant == 0:
            super().wheelEvent(event)
            return
        self.scroll_widget_slots(-1 if dominant > 0 else 1)
        event.accept()


class NavigationCommand(StrEnum):
    """Input-independent commands understood by the compact navigation host."""

    MOVE_LEFT = "move_left"
    MOVE_RIGHT = "move_right"
    MOVE_UP = "move_up"
    MOVE_DOWN = "move_down"
    PREVIOUS_WIDGET = "previous_widget"
    NEXT_WIDGET = "next_widget"
    ACTIVATE = "activate"
    BACK = "back"
    OPEN_OPTIONS = "open_options"
    TOGGLE_OVERLAY = "toggle_overlay"


_SPATIAL_NAVIGATION_COMMANDS = frozenset(
    {
        NavigationCommand.MOVE_LEFT,
        NavigationCommand.MOVE_RIGHT,
        NavigationCommand.MOVE_UP,
        NavigationCommand.MOVE_DOWN,
    }
)


def spatial_navigation_target(
    rectangles: Sequence[QRectF],
    current_index: int,
    command: NavigationCommand,
) -> int | None:
    """Return the nearest rendered control in one cardinal direction.

    Controls that overlap the source control's horizontal or vertical navigation
    beam win over diagonal candidates. This keeps rows and columns intuitive while
    still allowing uneven host-rendered layouts to remain controller navigable.
    """

    if command not in _SPATIAL_NAVIGATION_COMMANDS:
        return None
    if not 0 <= current_index < len(rectangles):
        return None

    source = rectangles[current_index]
    source_center = source.center()
    best: tuple[tuple[float, float, float, float, int], int] | None = None
    for index, candidate in enumerate(rectangles):
        if index == current_index:
            continue
        candidate_center = candidate.center()
        if command is NavigationCommand.MOVE_LEFT:
            if min(source.right(), candidate.right()) - max(source.left(), candidate.left()) > 0.5:
                continue
            primary_distance = source_center.x() - candidate_center.x()
            primary_gap = max(source.left() - candidate.right(), 0.0)
            cross_distance = abs(source_center.y() - candidate_center.y())
            aligned = min(source.bottom(), candidate.bottom()) >= max(source.top(), candidate.top())
        elif command is NavigationCommand.MOVE_RIGHT:
            if min(source.right(), candidate.right()) - max(source.left(), candidate.left()) > 0.5:
                continue
            primary_distance = candidate_center.x() - source_center.x()
            primary_gap = max(candidate.left() - source.right(), 0.0)
            cross_distance = abs(source_center.y() - candidate_center.y())
            aligned = min(source.bottom(), candidate.bottom()) >= max(source.top(), candidate.top())
        elif command is NavigationCommand.MOVE_UP:
            if min(source.bottom(), candidate.bottom()) - max(source.top(), candidate.top()) > 0.5:
                continue
            primary_distance = source_center.y() - candidate_center.y()
            primary_gap = max(source.top() - candidate.bottom(), 0.0)
            cross_distance = abs(source_center.x() - candidate_center.x())
            aligned = min(source.right(), candidate.right()) >= max(source.left(), candidate.left())
        else:
            if min(source.bottom(), candidate.bottom()) - max(source.top(), candidate.top()) > 0.5:
                continue
            primary_distance = candidate_center.y() - source_center.y()
            primary_gap = max(candidate.top() - source.bottom(), 0.0)
            cross_distance = abs(source_center.x() - candidate_center.x())
            aligned = min(source.right(), candidate.right()) >= max(source.left(), candidate.left())

        if primary_distance <= 0.5:
            continue
        score = (
            0.0 if aligned else 1.0,
            primary_gap,
            cross_distance,
            primary_distance,
            index,
        )
        if best is None or score < best[0]:
            best = (score, index)
    return None if best is None else best[1]


class FocusZone(StrEnum):
    """The current controller/keyboard focus region."""

    WIDGET_STRIP = "widget_strip"
    CONTENT = "content"


class NavigationOutcome(StrEnum):
    """Result of applying one navigation command."""

    NO_CHANGE = "no_change"
    FOCUS_CHANGED = "focus_changed"
    WIDGET_CHANGED = "widget_changed"
    ITEM_CHANGED = "item_changed"
    ITEM_ACTIVATED = "item_activated"
    HIDE_REQUESTED = "hide_requested"
    TOGGLE_REQUESTED = "toggle_requested"


WidgetChangedCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class NavigationResult:
    """Immutable result returned to keyboard and controller callers."""

    outcome: NavigationOutcome
    selected_widget_id: str
    focus_zone: FocusZone
    selected_item_index: int | None = None
    selected_item_id: str | None = None


@dataclass(slots=True)
class CompactFocusState:
    """Pure state machine shared by keyboard and controller backends."""

    widget_ids: tuple[str, ...]
    item_counts: Mapping[str, int]
    selected_widget_id: str
    focus_zone: FocusZone = FocusZone.WIDGET_STRIP
    selected_items: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.widget_ids:
            raise ValueError("compact navigation requires at least one widget")
        if self.selected_widget_id not in self.widget_ids:
            self.selected_widget_id = self.widget_ids[0]
        normalized_counts: dict[str, int] = {}
        for widget_id in self.widget_ids:
            count = self.item_counts.get(widget_id, 0)
            if count < 0:
                raise ValueError(f"item count cannot be negative for widget {widget_id}")
            normalized_counts[widget_id] = count
            self.selected_items[widget_id] = self._clamp_item_index(
                widget_id,
                self.selected_items.get(widget_id, 0),
                count_override=count,
            )
        self.item_counts = normalized_counts
        if self.current_item_count == 0:
            self.focus_zone = FocusZone.WIDGET_STRIP

    @property
    def selected_widget_index(self) -> int:
        return self.widget_ids.index(self.selected_widget_id)

    @property
    def current_item_count(self) -> int:
        return self.item_counts.get(self.selected_widget_id, 0)

    @property
    def selected_item_index(self) -> int | None:
        if self.current_item_count == 0:
            return None
        return self.selected_items[self.selected_widget_id]

    def set_selected_widget(
        self,
        widget_id: str,
        *,
        focus_zone: FocusZone | None = None,
    ) -> bool:
        if widget_id not in self.widget_ids:
            raise ValueError(f"unknown widget: {widget_id}")
        changed = self.selected_widget_id != widget_id
        self.selected_widget_id = widget_id
        if focus_zone is not None:
            self.focus_zone = focus_zone
        if self.current_item_count == 0:
            self.focus_zone = FocusZone.WIDGET_STRIP
        return changed

    def set_selected_item(self, item_index: int) -> bool:
        count = self.current_item_count
        if count == 0:
            self.focus_zone = FocusZone.WIDGET_STRIP
            return False
        clamped = self._clamp_item_index(self.selected_widget_id, item_index)
        changed = self.selected_items[self.selected_widget_id] != clamped
        self.selected_items[self.selected_widget_id] = clamped
        self.focus_zone = FocusZone.CONTENT
        return changed

    def set_item_count(self, widget_id: str, item_count: int) -> None:
        if widget_id not in self.widget_ids:
            raise ValueError(f"unknown widget: {widget_id}")
        if item_count < 0:
            raise ValueError("item count cannot be negative")
        counts = dict(self.item_counts)
        counts[widget_id] = item_count
        self.item_counts = counts
        self.selected_items[widget_id] = self._clamp_item_index(
            widget_id,
            self.selected_items.get(widget_id, 0),
            count_override=item_count,
        )
        if widget_id == self.selected_widget_id and item_count == 0:
            self.focus_zone = FocusZone.WIDGET_STRIP

    def dispatch(self, command: NavigationCommand) -> NavigationOutcome:
        if command is NavigationCommand.MOVE_LEFT:
            return self._move_widget(-1)
        if command is NavigationCommand.MOVE_RIGHT:
            return self._move_widget(1)
        if command is NavigationCommand.MOVE_UP:
            return self._move_vertical(-1)
        if command is NavigationCommand.MOVE_DOWN:
            return self._move_vertical(1)
        if command is NavigationCommand.PREVIOUS_WIDGET:
            return self._move_widget(-1)
        if command is NavigationCommand.NEXT_WIDGET:
            return self._move_widget(1)
        if command is NavigationCommand.ACTIVATE:
            return self._activate()
        if command is NavigationCommand.BACK:
            return self._back()
        if command is NavigationCommand.OPEN_OPTIONS:
            return NavigationOutcome.NO_CHANGE
        if command is NavigationCommand.TOGGLE_OVERLAY:
            return NavigationOutcome.TOGGLE_REQUESTED
        return NavigationOutcome.NO_CHANGE

    def _move_widget(self, delta: int) -> NavigationOutcome:
        target_index = (self.selected_widget_index + delta) % len(self.widget_ids)
        target = self.widget_ids[target_index]
        changed = self.set_selected_widget(target, focus_zone=FocusZone.WIDGET_STRIP)
        return NavigationOutcome.WIDGET_CHANGED if changed else NavigationOutcome.FOCUS_CHANGED

    def _move_vertical(self, delta: int) -> NavigationOutcome:
        if self.current_item_count == 0:
            self.focus_zone = FocusZone.WIDGET_STRIP
            return NavigationOutcome.NO_CHANGE

        if self.focus_zone is FocusZone.WIDGET_STRIP:
            if delta > 0:
                self.focus_zone = FocusZone.CONTENT
                return NavigationOutcome.FOCUS_CHANGED
            return NavigationOutcome.NO_CHANGE

        current = self.selected_items[self.selected_widget_id]
        if delta < 0 and current == 0:
            self.focus_zone = FocusZone.WIDGET_STRIP
            return NavigationOutcome.FOCUS_CHANGED

        target = min(max(current + delta, 0), self.current_item_count - 1)
        if target == current:
            return NavigationOutcome.NO_CHANGE
        self.selected_items[self.selected_widget_id] = target
        return NavigationOutcome.ITEM_CHANGED

    def _activate(self) -> NavigationOutcome:
        if self.focus_zone is FocusZone.WIDGET_STRIP:
            if self.current_item_count == 0:
                return NavigationOutcome.NO_CHANGE
            self.focus_zone = FocusZone.CONTENT
            return NavigationOutcome.FOCUS_CHANGED
        if self.selected_item_index is None:
            return NavigationOutcome.NO_CHANGE
        return NavigationOutcome.ITEM_ACTIVATED

    def _back(self) -> NavigationOutcome:
        if self.focus_zone is FocusZone.CONTENT:
            self.focus_zone = FocusZone.WIDGET_STRIP
            return NavigationOutcome.FOCUS_CHANGED
        return NavigationOutcome.HIDE_REQUESTED

    def _clamp_item_index(
        self,
        widget_id: str,
        item_index: int,
        *,
        count_override: int | None = None,
    ) -> int:
        count = self.item_counts.get(widget_id, 0) if count_override is None else count_override
        if count <= 0:
            return 0
        return min(max(item_index, 0), count - 1)


def navigation_command_for_key(event: QKeyEvent) -> NavigationCommand | None:
    """Translate keyboard input into the commands used by controller navigation."""

    key = event.key()
    modifiers = event.modifiers()
    control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
    shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
    other_modifiers = modifiers & ~(
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    )

    if control and not other_modifiers and key in {Qt.Key.Key_Tab, Qt.Key.Key_Backtab}:
        if shift or key == Qt.Key.Key_Backtab:
            return NavigationCommand.PREVIOUS_WIDGET
        return NavigationCommand.NEXT_WIDGET

    if modifiers != Qt.KeyboardModifier.NoModifier:
        return None

    key_map: dict[int, NavigationCommand] = {
        int(Qt.Key.Key_Left): NavigationCommand.MOVE_LEFT,
        int(Qt.Key.Key_Right): NavigationCommand.MOVE_RIGHT,
        int(Qt.Key.Key_Up): NavigationCommand.MOVE_UP,
        int(Qt.Key.Key_Down): NavigationCommand.MOVE_DOWN,
        int(Qt.Key.Key_Q): NavigationCommand.PREVIOUS_WIDGET,
        int(Qt.Key.Key_E): NavigationCommand.NEXT_WIDGET,
        int(Qt.Key.Key_Return): NavigationCommand.ACTIVATE,
        int(Qt.Key.Key_Enter): NavigationCommand.ACTIVATE,
        int(Qt.Key.Key_Space): NavigationCommand.ACTIVATE,
        int(Qt.Key.Key_Escape): NavigationCommand.BACK,
        int(Qt.Key.Key_Menu): NavigationCommand.OPEN_OPTIONS,
    }
    return key_map.get(key)


_ICON_MAP: dict[str, QStyle.StandardPixmap] = {
    "home": QStyle.StandardPixmap.SP_DirHomeIcon,
    "performance": QStyle.StandardPixmap.SP_ComputerIcon,
    "display": QStyle.StandardPixmap.SP_DesktopIcon,
    "settings": QStyle.StandardPixmap.SP_FileDialogContentsView,
    "computer": QStyle.StandardPixmap.SP_ComputerIcon,
    "information": QStyle.StandardPixmap.SP_MessageBoxInformation,
    "network": QStyle.StandardPixmap.SP_DriveNetIcon,
    "resolution": QStyle.StandardPixmap.SP_TitleBarMaxButton,
    "refresh": QStyle.StandardPixmap.SP_BrowserReload,
    "overlay": QStyle.StandardPixmap.SP_TitleBarMenuButton,
    "controls": QStyle.StandardPixmap.SP_ArrowRight,
    "widgets": QStyle.StandardPixmap.SP_FileDialogDetailedView,
    "appearance": QStyle.StandardPixmap.SP_DialogResetButton,
    "audio": QStyle.StandardPixmap.SP_MediaVolume,
}


class NavigationShell(QWidget):
    """Registry-driven widget strip with one spatially navigable active view."""

    widget_changed = Signal(str)
    item_activated = Signal(str, str)
    item_secondary_activated = Signal(str, str, str)
    panel_size_hint_changed = Signal()

    def __init__(
        self,
        widget_definitions: tuple[WidgetDefinition, ...],
        selected_widget_id: str,
        on_widget_changed: WidgetChangedCallback,
        parent: QWidget | None = None,
        *,
        telemetry_snapshot: TelemetrySnapshot | None = None,
        visible_widget_ids: tuple[str, ...] | None = None,
        guide_button_enabled: bool = True,
        controller_shortcut_binding: ControllerShortcutBinding | None = None,
        allow_mouse_navigation_while_controller_connected: bool = False,
        hotkey_combination: str = "Ctrl+Alt+Shift+G",
        start_with_windows_enabled: bool = True,
        start_with_windows_available: bool = True,
        run_in_background_enabled: bool = True,
        run_in_background_available: bool = True,
        safe_mode_active: bool = False,
        application_icon: QIcon | None = None,
    ) -> None:
        super().__init__(parent)
        if not widget_definitions:
            raise ValueError("navigation shell requires at least one widget")
        self.setObjectName("navigationShell")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._on_widget_changed = on_widget_changed
        self._definitions = {definition.widget_id: definition for definition in widget_definitions}
        if len(self._definitions) != len(widget_definitions):
            raise ValueError("navigation shell received duplicate widget definitions")
        all_widget_ids = tuple(definition.widget_id for definition in widget_definitions)
        widget_ids = visible_widget_ids or all_widget_ids
        if not widget_ids:
            raise ValueError("navigation shell requires at least one visible widget")
        unknown_visible = [
            widget_id for widget_id in widget_ids if widget_id not in self._definitions
        ]
        if unknown_visible:
            raise ValueError(
                f"navigation shell received unknown visible widget: {unknown_visible[0]}"
            )
        initial_widget = selected_widget_id if selected_widget_id in widget_ids else widget_ids[0]
        self._buttons: dict[str, QPushButton] = {}
        self._item_buttons: dict[str, list[QPushButton]] = {}
        self._secondary_action_buttons: dict[str, dict[int, QPushButton]] = {}
        self._secondary_action_focus: tuple[str, int] | None = None
        self._page_indexes: dict[str, int] = {}
        self._page_scrollers: dict[str, QScrollArea] = {}
        self._panel_size_hint_refresh_pending = False
        self._last_dynamic_natural_height: dict[str, int] = {}
        self._stack = QStackedWidget(self)
        self._performance_view: PerformanceWidgetView | None = None
        self._display_view: DisplayWidgetView | None = None
        self._audio_view: AudioWidgetView | None = None
        self._wifi_view: WifiWidgetView | None = None
        self._settings_view: SettingsWidgetView | None = None
        self._integrations_view: IntegrationsWidgetView | None = None
        self._telemetry_snapshot = telemetry_snapshot or TelemetrySnapshot.unavailable()
        self._guide_button_enabled = guide_button_enabled
        self._controller_shortcut_binding = (
            controller_shortcut_binding or ControllerShortcutBinding()
        )
        self._allow_mouse_navigation_while_controller_connected = (
            allow_mouse_navigation_while_controller_connected
        )
        self._hotkey_combination = hotkey_combination
        self._start_with_windows_enabled = start_with_windows_enabled
        self._start_with_windows_available = start_with_windows_available
        self._run_in_background_enabled = run_in_background_enabled
        self._run_in_background_available = run_in_background_available
        self._safe_mode_active = safe_mode_active
        self._application_icon = application_icon or QIcon()
        item_counts = {
            widget_id: len(self._definitions[widget_id].items) for widget_id in widget_ids
        }
        self._state = CompactFocusState(
            widget_ids=widget_ids,
            item_counts=item_counts,
            selected_widget_id=initial_widget,
        )
        self._build_ui(widget_definitions)
        self._set_button_visibility(widget_ids)
        self._apply_state(persist_widget=False)

    @property
    def selected_widget_id(self) -> str:
        return self._state.selected_widget_id

    @property
    def focus_zone(self) -> FocusZone:
        return self._state.focus_zone

    @property
    def selected_item_index(self) -> int | None:
        return self._state.selected_item_index

    @property
    def selected_item_id(self) -> str | None:
        index = self._state.selected_item_index
        if index is None:
            return None
        return self._definitions[self.selected_widget_id].items[index].item_id

    @property
    def widget_ids(self) -> tuple[str, ...]:
        return self._state.widget_ids

    def widget_definition(self, widget_id: str) -> WidgetDefinition:
        try:
            return self._definitions[widget_id]
        except KeyError as exc:
            raise ValueError(f"unknown widget: {widget_id}") from exc

    @property
    def current_panel_width(self) -> int:
        return self._definitions[self.selected_widget_id].preferred_panel_width

    @property
    def current_natural_panel_height(self) -> int:
        """Return the active widget panel's natural height before monitor capping.

        The shared strip remains fixed-height while the page contributes its layout's
        width-aware natural height. The host window caps this value to the active
        monitor and lets the page scroll area handle any overflow.
        """

        widget_id = self.selected_widget_id
        scroller = self._page_scrollers[widget_id]
        page = scroller.widget()
        page_height = 0
        if page is not None:
            page_layout = page.layout()
            if page_layout is not None:
                content_width = max(scroller.viewport().width(), 1)
                height_for_width = (
                    page_layout.totalHeightForWidth(content_width)
                    if page_layout.hasHeightForWidth()
                    else 0
                )
                page_height = max(
                    height_for_width,
                    page_layout.minimumSize().height(),
                    page.minimumSizeHint().height(),
                )
            else:
                page_height = max(page.sizeHint().height(), page.minimumSizeHint().height())

            scrollbar = scroller.verticalScrollBar()
            if (scrollbar.maximum() - scrollbar.minimum()) > 4:
                # A real range means the current viewport is still shorter than the
                # content on this platform/style. Include the actual overflowing page
                # height so the host gets one more chance to grow before scrolling.
                page_height = max(page_height, page.height())

        root_layout = self.layout()
        spacing = root_layout.spacing() if root_layout is not None else 0
        # Keep a tiny fit allowance for platform layout/frame rounding. The host still
        # caps the result to the monitor, so genuine overflow continues to scroll.
        fit_allowance = 6 if page_height > 0 else 0
        return self._header.height() + max(spacing, 0) + max(page_height, 0) + fit_allowance

    @property
    def performance_view(self) -> PerformanceWidgetView | None:
        return self._performance_view

    @property
    def display_view(self) -> DisplayWidgetView | None:
        return self._display_view

    @property
    def audio_view(self) -> AudioWidgetView | None:
        return self._audio_view

    @property
    def wifi_view(self) -> WifiWidgetView | None:
        return self._wifi_view

    @property
    def settings_view(self) -> SettingsWidgetView | None:
        return self._settings_view

    def set_hotkey_combination(self, combination: str) -> None:
        self._hotkey_combination = combination
        if self._settings_view is not None:
            self._settings_view.set_hotkey_combination(combination)

    @property
    def integrations_view(self) -> IntegrationsWidgetView | None:
        return self._integrations_view

    def page_scroll_area(self, widget_id: str) -> QScrollArea:
        """Return the host-owned vertical scroller for one registered widget page."""

        try:
            return self._page_scrollers[widget_id]
        except KeyError as exc:
            raise ValueError(f"unknown widget: {widget_id}") from exc

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Observe runtime page-layout changes that can alter natural panel height.

        Widget content is allowed to change after construction (for example, an
        initially empty status/error label becoming visible). Qt emits a
        ``LayoutRequest`` for the containing page in that case, but the previous
        adaptive-height implementation only recalculated for explicit host actions
        such as widget switches and list replacement. Observe the page itself so
        any built-in or host-wrapped widget using normal Qt layout semantics
        can request a new panel fit before relying on scrolling.
        """

        if isinstance(watched, QWidget):
            widget_id = watched.property("hostAdaptiveWidgetId")
            if (
                isinstance(widget_id, str)
                and widget_id == self.selected_widget_id
                and event.type()
                in (
                    QEvent.Type.LayoutRequest,
                    QEvent.Type.Show,
                    QEvent.Type.Hide,
                )
            ):
                self._schedule_dynamic_panel_size_hint_refresh()
        return super().eventFilter(watched, event)

    def _schedule_dynamic_panel_size_hint_refresh(self) -> None:
        if self._panel_size_hint_refresh_pending:
            return
        self._panel_size_hint_refresh_pending = True
        QTimer.singleShot(0, self._emit_dynamic_panel_size_hint_refresh)

    def _emit_dynamic_panel_size_hint_refresh(self) -> None:
        self._panel_size_hint_refresh_pending = False
        widget_id = self.selected_widget_id
        scroller = self._page_scrollers.get(widget_id)
        if scroller is None:
            return
        page = scroller.widget()
        if page is not None:
            page_layout = page.layout()
            if page_layout is not None:
                page_layout.activate()
        natural_height = self.current_natural_panel_height
        previous_height = self._last_dynamic_natural_height.get(widget_id)
        self._last_dynamic_natural_height[widget_id] = natural_height
        if previous_height != natural_height:
            self.panel_size_hint_changed.emit()

    def set_selected_widget(self, widget_id: str, *, persist: bool = True) -> None:
        changed = self._state.set_selected_widget(
            widget_id,
            focus_zone=FocusZone.WIDGET_STRIP,
        )
        self._apply_state(persist_widget=persist and changed)
        if changed:
            self.panel_size_hint_changed.emit()

    def set_visible_widgets(
        self,
        widget_ids: tuple[str, ...],
        *,
        selected_widget_id: str | None = None,
        persist: bool = False,
    ) -> None:
        """Update strip visibility without rebuilding widget pages or losing runtime content."""

        if not widget_ids:
            raise ValueError("navigation shell requires at least one visible widget")
        unknown = [widget_id for widget_id in widget_ids if widget_id not in self._definitions]
        if unknown:
            raise ValueError(f"unknown widget: {unknown[0]}")

        previous = self._state
        target = selected_widget_id or previous.selected_widget_id
        if target not in widget_ids:
            target = widget_ids[0]
        focus_zone = (
            previous.focus_zone if target == previous.selected_widget_id else FocusZone.WIDGET_STRIP
        )
        item_counts = {
            widget_id: len(self._definitions[widget_id].items) for widget_id in widget_ids
        }
        self._state = CompactFocusState(
            widget_ids=widget_ids,
            item_counts=item_counts,
            selected_widget_id=target,
            focus_zone=focus_zone,
            selected_items=dict(previous.selected_items),
        )
        self._set_button_visibility(widget_ids)
        self._sync_strip_viewport_width(widget_ids)
        self._strip_content.adjustSize()
        self._apply_state(persist_widget=persist)
        QTimer.singleShot(0, self._ensure_selected_widget_visible)
        self.panel_size_hint_changed.emit()

    def set_selected_item(self, item_index: int) -> None:
        self._state.set_selected_item(item_index)
        self._apply_state(persist_widget=False)

    def set_telemetry_snapshot(self, snapshot: TelemetrySnapshot) -> None:
        self._telemetry_snapshot = snapshot
        if self._performance_view is not None:
            self._performance_view.apply_snapshot(snapshot)

    def replace_widget_items(
        self,
        widget_id: str,
        items: tuple[WidgetItemDefinition, ...],
        *,
        empty_message: str | None = None,
    ) -> None:
        """Replace one standard-list widget's runtime items without rebuilding the shell."""

        definition = self._definitions.get(widget_id)
        if definition is None:
            raise ValueError(f"unknown widget: {widget_id}")
        if definition.view_kind is not WidgetViewKind.STANDARD_LIST:
            raise ValueError("runtime item replacement is supported only for standard-list widgets")
        updated = replace(
            definition,
            items=items,
            empty_message=(
                empty_message if empty_message is not None else definition.empty_message
            ),
        )
        page_scroller = self._page_scrollers[widget_id]
        old_page = page_scroller.takeWidget()
        page, buttons = self._build_page(updated)
        page_layout = page.layout()
        if page_layout is not None:
            page_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        page_scroller.setWidget(page)
        if old_page is not None:
            old_page.deleteLater()
        self._definitions[widget_id] = updated
        self._item_buttons[widget_id] = buttons
        self._state.set_item_count(widget_id, len(items))
        self._apply_state(persist_widget=False)
        if widget_id == self.selected_widget_id:
            self.panel_size_hint_changed.emit()

    def _replace_custom_view_items(
        self,
        widget_id: str,
        items: tuple[WidgetItemDefinition, ...],
        buttons: tuple[QPushButton, ...],
    ) -> None:
        definition = self._definitions.get(widget_id)
        if definition is None:
            return
        previous_buttons = set(self._item_buttons.get(widget_id, ()))
        self._definitions[widget_id] = replace(definition, items=items)
        self._item_buttons[widget_id] = list(buttons)
        self._state.set_item_count(widget_id, len(items))
        for index, button in enumerate(buttons):
            if button in previous_buttons:
                continue
            button.clicked.connect(
                lambda checked=False, target_widget=widget_id, item_index=index: self._item_clicked(
                    target_widget, item_index
                )
            )
        self._apply_state(persist_widget=False)
        if widget_id == self.selected_widget_id:
            self.panel_size_hint_changed.emit()

    def restore_focus(self) -> None:
        """Restore the managed focus without rebuilding any widgets."""

        self._apply_state(persist_widget=False)
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def _selected_secondary_action_button(self) -> QPushButton | None:
        if self._state.focus_zone is not FocusZone.CONTENT:
            return None
        selected_index = self._state.selected_item_index
        if selected_index is None:
            return None
        return self._secondary_action_buttons.get(self.selected_widget_id, {}).get(selected_index)

    def _result_for_outcome(self, outcome: NavigationOutcome) -> NavigationResult:
        return NavigationResult(
            outcome=outcome,
            selected_widget_id=self.selected_widget_id,
            focus_zone=self.focus_zone,
            selected_item_index=self.selected_item_index,
            selected_item_id=self.selected_item_id,
        )

    def handle_command(self, command: NavigationCommand) -> NavigationResult:
        if command in {
            NavigationCommand.PREVIOUS_WIDGET,
            NavigationCommand.NEXT_WIDGET,
        }:
            self._secondary_action_focus = None

        if self._secondary_action_focus is not None:
            widget_id, item_index = self._secondary_action_focus
            button = self._secondary_action_buttons.get(widget_id, {}).get(item_index)
            if command is NavigationCommand.ACTIVATE and button is not None:
                action_id = button.property("actionId")
                item_id = button.property("itemId")
                if isinstance(action_id, str) and isinstance(item_id, str):
                    self.item_secondary_activated.emit(widget_id, item_id, action_id)
                    return self._result_for_outcome(NavigationOutcome.ITEM_ACTIVATED)
            if command in {NavigationCommand.MOVE_LEFT, NavigationCommand.BACK}:
                self._secondary_action_focus = None
                self._apply_state(persist_widget=False)
                return self._result_for_outcome(NavigationOutcome.FOCUS_CHANGED)
            if command in {NavigationCommand.MOVE_UP, NavigationCommand.MOVE_DOWN}:
                self._secondary_action_focus = None
            elif command is NavigationCommand.MOVE_RIGHT:
                return self._result_for_outcome(NavigationOutcome.NO_CHANGE)

        if command is NavigationCommand.MOVE_RIGHT:
            secondary_button = self._selected_secondary_action_button()
            selected_index = self._state.selected_item_index
            if secondary_button is not None and selected_index is not None:
                self._secondary_action_focus = (self.selected_widget_id, selected_index)
                self._apply_state(persist_widget=False)
                return self._result_for_outcome(NavigationOutcome.FOCUS_CHANGED)

        spatial_outcome = self._move_content_focus_spatially(command)
        if spatial_outcome is not None:
            self._apply_state(persist_widget=False)
            return self._result_for_outcome(spatial_outcome)

        previous_widget = self._state.selected_widget_id
        outcome = self._state.dispatch(command)
        widget_changed = previous_widget != self._state.selected_widget_id
        self._apply_state(persist_widget=widget_changed)
        if widget_changed:
            self.panel_size_hint_changed.emit()

        selected_item_id = self.selected_item_id
        if outcome is NavigationOutcome.ITEM_ACTIVATED and selected_item_id is not None:
            selected_index = self._state.selected_item_index
            if selected_index is None:
                outcome = NavigationOutcome.NO_CHANGE
            else:
                definition = self._definitions[self.selected_widget_id].items[selected_index]
                if definition.enabled:
                    self.item_activated.emit(self.selected_widget_id, selected_item_id)
                else:
                    outcome = NavigationOutcome.NO_CHANGE

        return NavigationResult(
            outcome=outcome,
            selected_widget_id=self.selected_widget_id,
            focus_zone=self.focus_zone,
            selected_item_index=self.selected_item_index,
            selected_item_id=selected_item_id,
        )

    def _move_content_focus_spatially(
        self,
        command: NavigationCommand,
    ) -> NavigationOutcome | None:
        """Move content focus using host-rendered control geometry when available."""

        if self._state.focus_zone is not FocusZone.CONTENT:
            return None
        if command not in _SPATIAL_NAVIGATION_COMMANDS:
            return None
        if not self.isVisible():
            if command in {NavigationCommand.MOVE_UP, NavigationCommand.MOVE_DOWN}:
                return None
            return NavigationOutcome.NO_CHANGE
        selected_index = self._state.selected_item_index
        if selected_index is None:
            return NavigationOutcome.NO_CHANGE
        buttons = self._item_buttons.get(self.selected_widget_id, [])
        if not 0 <= selected_index < len(buttons):
            return NavigationOutcome.NO_CHANGE

        entries = tuple(
            (index, rectangle)
            for index, button in enumerate(buttons)
            if (rectangle := self._navigation_rectangle(button)) is not None
        )
        selected_position = next(
            (
                position
                for position, (item_index, _rectangle) in enumerate(entries)
                if item_index == selected_index
            ),
            None,
        )
        if selected_position is None:
            if command in {NavigationCommand.MOVE_UP, NavigationCommand.MOVE_DOWN}:
                return None
            return NavigationOutcome.NO_CHANGE
        rectangles = tuple(rectangle for _item_index, rectangle in entries)
        source_center = rectangles[selected_position].center()
        geometry_ready = len(rectangles) <= 1 or any(
            abs(rectangle.center().x() - source_center.x()) > 0.5
            or abs(rectangle.center().y() - source_center.y()) > 0.5
            for index, rectangle in enumerate(rectangles)
            if index != selected_position
        )
        if not geometry_ready:
            if command in {NavigationCommand.MOVE_UP, NavigationCommand.MOVE_DOWN}:
                return None
            return NavigationOutcome.NO_CHANGE

        target_position = spatial_navigation_target(
            rectangles,
            selected_position,
            command,
        )
        if target_position is not None:
            target_index = entries[target_position][0]
            changed = self._state.set_selected_item(target_index)
            return NavigationOutcome.ITEM_CHANGED if changed else NavigationOutcome.NO_CHANGE
        if command is NavigationCommand.MOVE_UP:
            self._state.focus_zone = FocusZone.WIDGET_STRIP
            return NavigationOutcome.FOCUS_CHANGED
        return NavigationOutcome.NO_CHANGE

    def _navigation_rectangle(self, button: QPushButton) -> QRectF | None:
        ancestor: QWidget | None = button
        while ancestor is not None and ancestor is not self._stack:
            if ancestor.width() <= 0 or ancestor.height() <= 0:
                return None
            ancestor = ancestor.parentWidget()
        if ancestor is None:
            return None
        top_left = button.mapTo(self._stack, button.rect().topLeft())
        return QRectF(
            float(top_left.x()),
            float(top_left.y()),
            float(button.width()),
            float(button.height()),
        )

    def _build_ui(self, widget_definitions: tuple[WidgetDefinition, ...]) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        self._header = QFrame(self)
        self._header.setObjectName("overlayHeader")
        self._header.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header_layout = QVBoxLayout(self._header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        self._branding = QFrame(self._header)
        self._branding.setObjectName("overlayBranding")
        self._branding.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._branding.setFixedHeight(_BRAND_HEADER_HEIGHT)
        branding_layout = QHBoxLayout(self._branding)
        branding_layout.setContentsMargins(2, 0, 0, 0)
        branding_layout.setSpacing(10)

        self._branding_icon_label = QLabel(self._branding)
        self._branding_icon_label.setObjectName("overlayBrandIcon")
        self._branding_icon_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._branding_icon_label.setFixedSize(_BRAND_ICON_SIZE, _BRAND_ICON_SIZE)
        if not self._application_icon.isNull():
            self._branding_icon_label.setPixmap(
                self._application_icon.pixmap(_BRAND_ICON_SIZE, _BRAND_ICON_SIZE)
            )
        else:
            self._branding_icon_label.hide()

        self._branding_title_label = QLabel("Vigil Overlay", self._branding)
        self._branding_title_label.setObjectName("overlayBrandTitle")
        self._branding_title_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        branding_layout.addWidget(self._branding_icon_label)
        branding_layout.addWidget(self._branding_title_label)
        branding_layout.addStretch(1)
        header_layout.addWidget(self._branding, 0, Qt.AlignmentFlag.AlignLeft)

        self._widget_strip_row = QFrame(self._header)
        self._widget_strip_row.setObjectName("overlayWidgetStripRow")
        self._widget_strip_row.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        strip_row_layout = QHBoxLayout(self._widget_strip_row)
        strip_row_layout.setContentsMargins(0, 0, 0, 0)
        strip_row_layout.setSpacing(0)

        self._strip_scroller = WidgetStripScrollArea(self._widget_strip_row)
        self._strip_scroller.setObjectName("widgetStripScroller")
        self._strip_scroller.setFrameShape(QFrame.Shape.NoFrame)
        self._strip_scroller.setWidgetResizable(False)
        self._strip_scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._strip_scroller.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._strip_scroller.setFixedHeight(_WIDGET_STRIP_HEIGHT)
        self._sync_strip_viewport_width(self._state.widget_ids)

        self._strip_content = QWidget(self._strip_scroller)
        self._strip_content.setObjectName("widgetStripContent")
        strip_layout = QHBoxLayout(self._strip_content)
        strip_layout.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        strip_layout.setContentsMargins(0, 0, 0, 0)
        strip_layout.setSpacing(_WIDGET_STRIP_SPACING)

        for definition in widget_definitions:
            button = QPushButton(self._strip_content)
            button.setObjectName("compactWidgetButton")
            button.setProperty("widgetId", definition.widget_id)
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setIcon(self._outline_widget_icon(button, definition.widget_id))
            button.setProperty("iconStyle", f"outline-{definition.widget_id}")
            button.setIconSize(QSize(28, 28))
            button.setFixedSize(_WIDGET_STRIP_BUTTON_SIZE, _WIDGET_STRIP_BUTTON_SIZE)
            button.setToolTip(definition.label)
            button.setAccessibleName(f"Open {definition.label} widget")
            button.clicked.connect(
                lambda checked=False, widget_id=definition.widget_id: self._widget_clicked(
                    widget_id
                )
            )
            self._buttons[definition.widget_id] = button
            strip_layout.addWidget(button)

            page, item_buttons = self._build_page(definition)
            page_scroller = self._wrap_page(definition.widget_id, page)
            self._item_buttons[definition.widget_id] = item_buttons
            self._page_scrollers[definition.widget_id] = page_scroller
            self._page_indexes[definition.widget_id] = self._stack.addWidget(page_scroller)

        self._strip_scroller.setWidget(self._strip_content)

        self._left_overflow_slot = QWidget(self._widget_strip_row)
        self._left_overflow_slot.setObjectName("widgetStripLeftOverflowSlot")
        self._left_overflow_slot.setFixedWidth(_WIDGET_STRIP_OVERFLOW_SLOT_WIDTH)
        left_overflow_layout = QHBoxLayout(self._left_overflow_slot)
        left_overflow_layout.setContentsMargins(0, 0, 6, 0)
        left_overflow_layout.setSpacing(0)
        self._left_overflow_indicator = self._build_strip_overflow_indicator(
            "widgetStripLeftOverflowIndicator",
            "\u276e",
            "More widget tabs to the left",
            -1,
            self._left_overflow_slot,
        )
        left_overflow_layout.addWidget(
            self._left_overflow_indicator,
            0,
            Qt.AlignmentFlag.AlignCenter,
        )

        self._right_overflow_slot = QWidget(self._widget_strip_row)
        self._right_overflow_slot.setObjectName("widgetStripRightOverflowSlot")
        self._right_overflow_slot.setFixedWidth(_WIDGET_STRIP_OVERFLOW_SLOT_WIDTH)
        right_overflow_layout = QHBoxLayout(self._right_overflow_slot)
        right_overflow_layout.setContentsMargins(6, 0, 0, 0)
        right_overflow_layout.setSpacing(0)
        self._right_overflow_indicator = self._build_strip_overflow_indicator(
            "widgetStripRightOverflowIndicator",
            "\u276f",
            "More widget tabs to the right",
            1,
            self._right_overflow_slot,
        )
        right_overflow_layout.addWidget(
            self._right_overflow_indicator,
            0,
            Qt.AlignmentFlag.AlignCenter,
        )

        strip_row_layout.addWidget(self._left_overflow_slot)
        strip_row_layout.addWidget(self._strip_scroller)
        strip_row_layout.addWidget(self._right_overflow_slot)
        strip_row_layout.addStretch(1)

        horizontal_bar = self._strip_scroller.horizontalScrollBar()
        horizontal_bar.rangeChanged.connect(self._update_strip_overflow_indicators)
        horizontal_bar.valueChanged.connect(self._update_strip_overflow_indicators)
        self._update_strip_overflow_indicators()
        header_layout.addWidget(self._widget_strip_row, 0, Qt.AlignmentFlag.AlignLeft)
        root.addWidget(self._header, 0, Qt.AlignmentFlag.AlignLeft)
        root.addWidget(self._stack, 1)

    def _wrap_page(self, widget_id: str, page: QWidget) -> QScrollArea:
        """Wrap a widget page in the shared host-owned vertical scrolling surface."""

        layout = page.layout()
        if layout is not None:
            layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)

        scroller = QScrollArea(self._stack)
        scroller.setObjectName("widgetPageScroller")
        scroller.setProperty("widgetId", widget_id)
        scroller.setFrameShape(QFrame.Shape.NoFrame)
        scroller.setWidgetResizable(True)
        scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroller.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        # Install a host-owned custom-painted scrollbar. The subclass refuses
        # platform/layout requests to show itself when the scroll range is zero,
        # and paints its own rounded track/thumb so Windows cannot flatten the
        # handle back to a square native-looking bar.
        vertical_scrollbar = VigilVerticalScrollBar(
            scroller,
            object_name="widgetPageVerticalScrollBar",
        )
        vertical_scrollbar.rangeChanged.connect(
            lambda minimum, maximum: (
                self._schedule_dynamic_panel_size_hint_refresh() if maximum - minimum > 4 else None
            )
        )
        scroller.setVerticalScrollBar(vertical_scrollbar)
        scroller.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroller.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        scroller.viewport().setObjectName("widgetPageViewport")
        scroller.viewport().setAutoFillBackground(False)
        page.setProperty("hostAdaptiveWidgetId", widget_id)
        page.installEventFilter(self)
        scroller.setWidget(page)
        return scroller

    def _sync_strip_viewport_width(self, widget_ids: tuple[str, ...]) -> None:
        self._strip_scroller.setFixedWidth(_widget_strip_viewport_width(len(widget_ids)))
        QTimer.singleShot(0, self._update_strip_overflow_indicators)

    def _build_strip_overflow_indicator(
        self,
        object_name: str,
        label: str,
        accessible_name: str,
        scroll_slots: int,
        parent: QWidget,
    ) -> QPushButton:
        indicator = QPushButton(label, parent)
        indicator.setObjectName(object_name)
        indicator.setProperty("widgetStripOverflowIndicator", True)
        indicator.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        indicator.setCursor(Qt.CursorShape.PointingHandCursor)
        indicator.setFixedSize(
            _WIDGET_STRIP_OVERFLOW_INDICATOR_WIDTH,
            _WIDGET_STRIP_BUTTON_SIZE,
        )
        indicator.setAccessibleName(accessible_name)
        indicator.setToolTip(accessible_name)
        # Page on press so an overlay activation/topmost reconciliation between
        # press and release cannot discard the mouse action before ``clicked``.
        # These arrows are mouse-only, non-focusable controls, so there is no
        # keyboard or controller activation contract tied to button release.
        indicator.pressed.connect(
            lambda slots=scroll_slots: self._strip_scroller.scroll_widget_slots(slots)
        )
        return indicator

    def _update_strip_overflow_indicators(self, *_args: int) -> None:
        bar = self._strip_scroller.horizontalScrollBar()
        has_overflow = bar.maximum() > bar.minimum()
        self._left_overflow_slot.setVisible(has_overflow)
        self._right_overflow_slot.setVisible(has_overflow)
        self._left_overflow_indicator.setVisible(has_overflow and bar.value() > bar.minimum())
        self._right_overflow_indicator.setVisible(has_overflow and bar.value() < bar.maximum())

    def _ensure_selected_widget_visible(self) -> None:
        button = self._buttons.get(self.selected_widget_id)
        if button is None or not button.isVisible():
            return
        ensure_controller_target_visible(
            self._strip_scroller,
            button,
            x_margin=0,
            y_margin=0,
        )

    def _set_button_visibility(self, widget_ids: tuple[str, ...]) -> None:
        visible = set(widget_ids)
        for widget_id, button in self._buttons.items():
            button.setVisible(widget_id in visible)

    def _build_page(
        self,
        definition: WidgetDefinition,
    ) -> tuple[QWidget, list[QPushButton]]:
        self._secondary_action_buttons[definition.widget_id] = {}
        if definition.view_kind is WidgetViewKind.PERFORMANCE:
            view = PerformanceWidgetView(definition, self._telemetry_snapshot, self._stack)
            self._performance_view = view
            performance_buttons = list(view.metric_buttons)
            for index, button in enumerate(performance_buttons):
                button.clicked.connect(
                    lambda checked=False, widget_id=definition.widget_id, item_index=index: (
                        self._item_clicked(widget_id, item_index)
                    )
                )
            return view, performance_buttons

        if definition.view_kind is WidgetViewKind.AUDIO:
            audio_view = AudioWidgetView(definition, self._stack)
            self._audio_view = audio_view
            audio_view.items_changed.connect(
                lambda items, buttons, widget_id=definition.widget_id: (
                    self._replace_custom_view_items(widget_id, items, buttons)
                )
            )
            audio_buttons = list(audio_view.item_buttons)
            for index, button in enumerate(audio_buttons):
                button.clicked.connect(
                    lambda checked=False, widget_id=definition.widget_id, item_index=index: (
                        self._item_clicked(widget_id, item_index)
                    )
                )
            self._definitions[definition.widget_id] = replace(
                definition, items=audio_view.item_definitions
            )
            # Synchronize the base controls immediately. The background audio runtime
            # emits items_changed later if live mixer-session rows change the count.
            if definition.widget_id in self._state.widget_ids:
                self._state.set_item_count(definition.widget_id, len(audio_view.item_definitions))
            return audio_view, audio_buttons

        if definition.view_kind is WidgetViewKind.WIFI:
            wifi_view = WifiWidgetView(definition, self._stack)
            self._wifi_view = wifi_view
            wifi_view.items_changed.connect(
                lambda items, buttons, widget_id=definition.widget_id: (
                    self._replace_custom_view_items(widget_id, items, buttons)
                )
            )
            wifi_buttons = list(wifi_view.item_buttons)
            for index, button in enumerate(wifi_buttons):
                button.clicked.connect(
                    lambda checked=False, widget_id=definition.widget_id, item_index=index: (
                        self._item_clicked(widget_id, item_index)
                    )
                )
            self._definitions[definition.widget_id] = replace(
                definition, items=wifi_view.item_definitions
            )
            if definition.widget_id in self._state.widget_ids:
                self._state.set_item_count(definition.widget_id, len(wifi_view.item_definitions))
            return wifi_view, wifi_buttons

        if definition.view_kind is WidgetViewKind.DISPLAY:
            display_view = DisplayWidgetView(definition, self._stack)
            self._display_view = display_view
            display_buttons = list(display_view.item_buttons)
            for index, button in enumerate(display_buttons):
                button.clicked.connect(
                    lambda checked=False, widget_id=definition.widget_id, item_index=index: (
                        self._item_clicked(widget_id, item_index)
                    )
                )
            return display_view, display_buttons

        if definition.view_kind is WidgetViewKind.INTEGRATIONS:
            integrations_view = IntegrationsWidgetView(definition, self._stack)
            self._integrations_view = integrations_view
            integration_buttons = list(integrations_view.item_buttons)
            for index, button in enumerate(integration_buttons):
                button.clicked.connect(
                    lambda checked=False, widget_id=definition.widget_id, item_index=index: (
                        self._item_clicked(widget_id, item_index)
                    )
                )
            return integrations_view, integration_buttons

        if definition.view_kind is WidgetViewKind.SETTINGS:
            settings_view = SettingsWidgetView(
                definition,
                self._stack,
                guide_button_enabled=self._guide_button_enabled,
                controller_shortcut_binding=self._controller_shortcut_binding,
                allow_mouse_navigation_while_controller_connected=(
                    self._allow_mouse_navigation_while_controller_connected
                ),
                hotkey_combination=self._hotkey_combination,
                start_with_windows_enabled=self._start_with_windows_enabled,
                start_with_windows_available=self._start_with_windows_available,
                run_in_background_enabled=self._run_in_background_enabled,
                run_in_background_available=self._run_in_background_available,
                safe_mode_active=self._safe_mode_active,
            )
            self._settings_view = settings_view
            settings_buttons = list(settings_view.item_buttons)
            for index, button in enumerate(settings_buttons):
                button.clicked.connect(
                    lambda checked=False, widget_id=definition.widget_id, item_index=index: (
                        self._item_clicked(widget_id, item_index)
                    )
                )
            return settings_view, settings_buttons

        page = QWidget(self._stack)
        page.setObjectName(f"compactWidgetPage_{definition.widget_id}")
        page.setProperty("widgetId", definition.widget_id)
        page.setProperty("compactPage", True)
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(5)

        label = QLabel(definition.label, page)
        label.setObjectName("compactPageTitle")
        description = QLabel(definition.description, page)
        description.setObjectName("compactPageDescription")
        description.setWordWrap(True)
        layout.addWidget(label)
        layout.addWidget(description)

        buttons: list[QPushButton] = []
        for index, item in enumerate(definition.items):
            is_home_power = definition.widget_id == "home" and item.item_id == "power"
            if is_home_power:
                layout.addStretch(1)
            button = QPushButton(item.label, page)
            button.setObjectName("compactPowerButton" if is_home_power else "compactListItem")
            button.setProperty("itemId", item.item_id)
            button.setCheckable(False)
            button.setEnabled(item.enabled)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if is_home_power:
                button.setText("")
                button.setIcon(self._outline_widget_icon(button, "power"))
                button.setProperty("iconStyle", "outline-power")
            elif item.widget_icon_source_id is not None:
                source_button = self._buttons.get(item.widget_icon_source_id)
                if source_button is not None and not source_button.icon().isNull():
                    button.setIcon(source_button.icon())
                    button.setProperty("widgetIconSourceId", item.widget_icon_source_id)
                else:
                    button.setIcon(self.style().standardIcon(self._icon(item.icon_key)))
            elif item.icon_path is not None:
                suffix = QFileInfo(item.icon_path).suffix().casefold()
                if suffix in {"jpg", "jpeg", "png", "webp", "bmp", "gif", "ico"}:
                    icon = QIcon(item.icon_path)
                else:
                    icon = QFileIconProvider().icon(QFileInfo(item.icon_path))
                if icon.isNull():
                    icon = self.style().standardIcon(self._icon(item.icon_key))
                button.setIcon(icon)
            else:
                button.setIcon(self.style().standardIcon(self._icon(item.icon_key)))
            button.setIconSize(QSize(28, 28) if is_home_power else QSize(34, 34))
            if is_home_power:
                button.setFixedSize(_WIDGET_STRIP_BUTTON_SIZE, _WIDGET_STRIP_BUTTON_SIZE)
            button.setToolTip(item.description)
            button.setAccessibleName(f"{item.label}. {item.description}")
            button.clicked.connect(
                lambda checked=False, widget_id=definition.widget_id, item_index=index: (
                    self._item_clicked(widget_id, item_index)
                )
            )
            buttons.append(button)
            if item.secondary_action_id is None:
                if is_home_power:
                    layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
                else:
                    layout.addWidget(button)
            else:
                row = QWidget(page)
                row.setObjectName("compactListItemRow")
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(8)
                button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                row_layout.addWidget(button, 1)
                action_button = QPushButton(item.secondary_action_label or "", row)
                action_button.setObjectName("compactListItemSecondaryAction")
                action_button.setProperty("itemId", item.item_id)
                action_button.setProperty("actionId", item.secondary_action_id)
                action_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                action_button.setToolTip(item.secondary_action_description or "")
                action_button.setAccessibleName(
                    f"{item.secondary_action_description or item.secondary_action_label}"
                )
                self._secondary_action_buttons[definition.widget_id][index] = action_button

                def emit_secondary_action(
                    checked: bool = False,
                    *,
                    widget_id: str = definition.widget_id,
                    item_id: str = item.item_id,
                    action_id: str | None = item.secondary_action_id,
                ) -> None:
                    del checked
                    self.item_secondary_activated.emit(widget_id, item_id, action_id or "")

                action_button.clicked.connect(emit_secondary_action)
                row_layout.addWidget(action_button)
                layout.addWidget(row)

        if not buttons:
            empty = QLabel(
                definition.empty_message or "This widget has no available items.",
                page,
            )
            empty.setObjectName("compactEmptyState")
            empty.setWordWrap(True)
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(empty)

        if not (
            definition.widget_id == "home"
            and any(item.item_id == "power" for item in definition.items)
        ):
            layout.addStretch(1)
        return page, buttons

    def _widget_clicked(self, widget_id: str) -> None:
        self.set_selected_widget(widget_id)
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    def _item_clicked(self, widget_id: str, item_index: int) -> None:
        changed_widget = self._state.set_selected_widget(
            widget_id,
            focus_zone=FocusZone.CONTENT,
        )
        self._state.set_selected_item(item_index)
        self._apply_state(persist_widget=changed_widget)
        result = self.handle_command(NavigationCommand.ACTIVATE)
        if result.outcome is NavigationOutcome.NO_CHANGE:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    def _apply_state(self, *, persist_widget: bool) -> None:
        widget_id = self._state.selected_widget_id
        selected_index = self._state.selected_item_index
        if self._secondary_action_focus != (widget_id, selected_index):
            self._secondary_action_focus = None
        self._stack.setCurrentIndex(self._page_indexes[widget_id])

        for current_widget_id, button in self._buttons.items():
            active = current_widget_id == widget_id
            strip_focus = active and self._state.focus_zone is FocusZone.WIDGET_STRIP
            button.setChecked(active)
            self._set_dynamic_property(button, "activeWidget", active)
            self._set_dynamic_property(button, "navigationFocus", strip_focus)

        for current_widget_id, buttons in self._item_buttons.items():
            for index, button in enumerate(buttons):
                selected = current_widget_id == widget_id and index == selected_index
                item_focus = selected and self._state.focus_zone is FocusZone.CONTENT
                self._set_dynamic_property(button, "selectedItem", selected)
                self._set_dynamic_property(button, "navigationFocus", item_focus)

        for current_widget_id, actions in self._secondary_action_buttons.items():
            for index, action_button in actions.items():
                action_focus = self._secondary_action_focus == (
                    current_widget_id,
                    index,
                )
                self._set_dynamic_property(action_button, "navigationFocus", action_focus)

        if self._performance_view is not None and widget_id == "performance":
            self._performance_view.set_selected_metric(selected_index or 0)

        self._ensure_selected_widget_visible()
        self._ensure_selected_item_visible(widget_id)
        if persist_widget:
            self._on_widget_changed(widget_id)
            self.widget_changed.emit(widget_id)

    def _ensure_selected_item_visible(self, widget_id: str) -> None:
        """Keep controller/keyboard content focus visible inside the active page scroller."""

        if widget_id != self._state.selected_widget_id:
            return
        if self._state.focus_zone is not FocusZone.CONTENT:
            return
        selected_index = self._state.selected_item_index
        if selected_index is None:
            return
        buttons = self._item_buttons.get(widget_id, [])
        if not 0 <= selected_index < len(buttons):
            return
        scroller = self._page_scrollers.get(widget_id)
        if scroller is None:
            return
        if selected_index == 0:
            # The first control is below the page heading and description. Qt's
            # ensureWidgetVisible would reveal only the control and can leave that
            # introductory text above the viewport. At the first item, the semantic
            # controller position is the true beginning of the page.
            scrollbar = scroller.verticalScrollBar()
            scrollbar.setValue(scrollbar.minimum())
            QTimer.singleShot(
                0,
                lambda bar=scrollbar: bar.setValue(bar.minimum()),
            )
            return
        ensure_controller_target_visible(
            scroller,
            buttons[selected_index],
            x_margin=18,
            y_margin=18,
        )

    @staticmethod
    def _outline_widget_icon(button: QPushButton, widget_id: str) -> QIcon:
        """Return a theme-aware outline icon from Vigil's widget-strip icon family."""

        size = 28
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(button.palette().buttonText().color())
        pen.setWidthF(2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        path = QPainterPath()
        if widget_id == "home":
            path.moveTo(4.5, 13.0)
            path.lineTo(14.0, 5.0)
            path.lineTo(23.5, 13.0)
            path.moveTo(7.0, 11.0)
            path.lineTo(7.0, 23.0)
            path.lineTo(21.0, 23.0)
            path.lineTo(21.0, 11.0)
            path.moveTo(11.0, 23.0)
            path.lineTo(11.0, 16.5)
            path.lineTo(17.0, 16.5)
            path.lineTo(17.0, 23.0)
        elif widget_id == "performance":
            path.moveTo(4.0, 22.0)
            path.lineTo(4.0, 6.0)
            path.moveTo(4.0, 22.0)
            path.lineTo(24.0, 22.0)
            path.moveTo(6.5, 18.0)
            path.lineTo(11.0, 13.5)
            path.lineTo(15.0, 16.0)
            path.lineTo(22.0, 8.0)
        elif widget_id == "audio":
            path.moveTo(5.0, 11.0)
            path.lineTo(9.0, 11.0)
            path.lineTo(14.0, 7.0)
            path.lineTo(14.0, 21.0)
            path.lineTo(9.0, 17.0)
            path.lineTo(5.0, 17.0)
            path.closeSubpath()
            path.moveTo(18.0, 10.0)
            path.cubicTo(21.0, 12.0, 21.0, 16.0, 18.0, 18.0)
            path.moveTo(20.5, 7.5)
            path.cubicTo(25.0, 11.0, 25.0, 17.0, 20.5, 20.5)
        elif widget_id == "wifi":
            path.moveTo(5.0, 11.0)
            path.cubicTo(10.0, 6.0, 18.0, 6.0, 23.0, 11.0)
            path.moveTo(8.0, 14.0)
            path.cubicTo(11.5, 10.5, 16.5, 10.5, 20.0, 14.0)
            path.moveTo(11.0, 17.0)
            path.cubicTo(12.8, 15.2, 15.2, 15.2, 17.0, 17.0)
            path.addEllipse(QRectF(13.0, 20.0, 2.0, 2.0))
        elif widget_id == "display":
            path.addRoundedRect(QRectF(4.0, 5.0, 20.0, 14.0), 2.0, 2.0)
            path.moveTo(14.0, 19.0)
            path.lineTo(14.0, 23.0)
            path.moveTo(9.5, 23.0)
            path.lineTo(18.5, 23.0)
        elif widget_id == "integrations":
            path.moveTo(10.0, 5.0)
            path.lineTo(10.0, 10.0)
            path.moveTo(18.0, 5.0)
            path.lineTo(18.0, 10.0)
            path.moveTo(7.0, 10.0)
            path.lineTo(21.0, 10.0)
            path.lineTo(21.0, 13.0)
            path.cubicTo(21.0, 17.0, 18.0, 20.0, 14.0, 20.0)
            path.cubicTo(10.0, 20.0, 7.0, 17.0, 7.0, 13.0)
            path.closeSubpath()
            path.moveTo(14.0, 20.0)
            path.lineTo(14.0, 24.0)
        elif widget_id == "settings":
            path.addEllipse(QRectF(10.0, 10.0, 8.0, 8.0))
            path.addEllipse(QRectF(6.0, 6.0, 16.0, 16.0))
            for x1, y1, x2, y2 in (
                (14.0, 3.5, 14.0, 6.0),
                (14.0, 22.0, 14.0, 24.5),
                (3.5, 14.0, 6.0, 14.0),
                (22.0, 14.0, 24.5, 14.0),
                (6.6, 6.6, 8.3, 8.3),
                (19.7, 19.7, 21.4, 21.4),
                (21.4, 6.6, 19.7, 8.3),
                (8.3, 19.7, 6.6, 21.4),
            ):
                path.moveTo(x1, y1)
                path.lineTo(x2, y2)
        elif widget_id == "widgets":
            for x, y in ((5.0, 5.0), (15.5, 5.0), (5.0, 15.5), (15.5, 15.5)):
                path.addRoundedRect(QRectF(x, y, 7.5, 7.5), 1.4, 1.4)
        elif widget_id == "power":
            path.moveTo(14.0, 3.5)
            path.lineTo(14.0, 13.5)
            path.moveTo(8.5, 7.0)
            path.cubicTo(5.0, 9.1, 3.8, 13.6, 5.6, 17.2)
            path.cubicTo(7.4, 20.8, 11.4, 22.8, 15.4, 22.0)
            path.cubicTo(19.4, 21.2, 22.2, 17.7, 22.2, 13.6)
            path.cubicTo(22.2, 10.8, 20.8, 8.3, 18.5, 6.9)
        else:
            path.addRoundedRect(QRectF(5.0, 5.0, 18.0, 18.0), 3.0, 3.0)

        painter.drawPath(path)
        painter.end()
        return QIcon(pixmap)

    @staticmethod
    def _icon(icon_key: str) -> QStyle.StandardPixmap:
        return _ICON_MAP.get(icon_key, QStyle.StandardPixmap.SP_FileIcon)

    @staticmethod
    def _set_dynamic_property(widget: QWidget, name: str, value: bool) -> None:
        if widget.property(name) == value:
            return
        widget.setProperty(name, value)
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()
