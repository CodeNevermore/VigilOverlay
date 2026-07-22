"""Typed widget registry used to populate the Compact Mode top strip."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

_WIDGET_ID: Final = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
DEFAULT_COMPACT_PANEL_WIDTH: Final = 610


class WidgetViewKind(StrEnum):
    """Host-rendered view layouts supported by the current widget contract."""

    STANDARD_LIST = "standard_list"
    PERFORMANCE = "performance"
    DISPLAY = "display"
    AUDIO = "audio"
    WIFI = "wifi"
    SETTINGS = "settings"
    INTEGRATIONS = "integrations"


@dataclass(frozen=True, slots=True)
class WidgetItemDefinition:
    """One controller-focusable item owned by a widget's vertical view."""

    item_id: str
    label: str
    description: str
    icon_key: str
    enabled: bool = True
    icon_path: str | None = None
    widget_icon_source_id: str | None = None
    secondary_action_id: str | None = None
    secondary_action_label: str | None = None
    secondary_action_description: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.item_id, "item_id")
        _validate_text(self.label, "label")
        _validate_text(self.description, "description")
        _validate_identifier(self.icon_key, "icon_key")
        if self.icon_path is not None:
            _validate_text(self.icon_path, "icon_path")
        if self.widget_icon_source_id is not None:
            _validate_identifier(self.widget_icon_source_id, "widget_icon_source_id")
        secondary_values = (
            self.secondary_action_id,
            self.secondary_action_label,
            self.secondary_action_description,
        )
        if any(value is not None for value in secondary_values):
            if any(value is None for value in secondary_values):
                raise ValueError("secondary action fields must be provided together")
            _validate_identifier(self.secondary_action_id or "", "secondary_action_id")
            _validate_text(self.secondary_action_label or "", "secondary_action_label")
            _validate_text(
                self.secondary_action_description or "",
                "secondary_action_description",
            )


@dataclass(frozen=True, slots=True)
class WidgetDefinition:
    """Host-facing metadata and initial content for one Compact Mode widget."""

    widget_id: str
    label: str
    description: str
    icon_key: str
    items: tuple[WidgetItemDefinition, ...] = ()
    empty_message: str | None = None
    required: bool = False
    built_in: bool = True
    view_kind: WidgetViewKind = WidgetViewKind.STANDARD_LIST
    preferred_panel_width: int = DEFAULT_COMPACT_PANEL_WIDTH

    def __post_init__(self) -> None:
        _validate_identifier(self.widget_id, "widget_id")
        _validate_text(self.label, "label")
        _validate_text(self.description, "description")
        _validate_identifier(self.icon_key, "icon_key")
        if self.empty_message is not None:
            _validate_text(self.empty_message, "empty_message")
        if not 320 <= self.preferred_panel_width <= 900:
            raise ValueError("preferred_panel_width must be between 320 and 900")
        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError(f"widget {self.widget_id} contains duplicate item IDs")


class WidgetRegistry:
    """Deterministic registry that resolves enabled widgets and strip order."""

    def __init__(
        self,
        definitions: tuple[WidgetDefinition, ...],
        *,
        enabled_widget_ids: tuple[str, ...],
        widget_order: tuple[str, ...],
    ) -> None:
        self._definitions: dict[str, WidgetDefinition] = {}
        self._registration_order: list[str] = []
        for definition in definitions:
            self.register(definition)
        self._enabled_widget_ids = _deduplicated_ids(enabled_widget_ids)
        self._widget_order = _deduplicated_ids(widget_order)

    def register(self, definition: WidgetDefinition) -> None:
        """Register one unique widget definition."""

        if definition.widget_id in self._definitions:
            raise ValueError(f"duplicate widget ID: {definition.widget_id}")
        self._definitions[definition.widget_id] = definition
        self._registration_order.append(definition.widget_id)

    @property
    def registered_widget_ids(self) -> tuple[str, ...]:
        return tuple(self._registration_order)

    def registered_widgets(self) -> tuple[WidgetDefinition, ...]:
        """Return every registered widget in stable registration order."""

        return tuple(
            self._definitions[widget_id] for widget_id in self._registration_order
        )

    @property
    def enabled_widget_ids(self) -> tuple[str, ...]:
        return self._enabled_widget_ids

    def set_enabled_widget_ids(self, widget_ids: tuple[str, ...]) -> None:
        """Replace the configured enabled set without altering registration order."""

        self._enabled_widget_ids = _deduplicated_ids(widget_ids)

    def definition(self, widget_id: str) -> WidgetDefinition:
        try:
            return self._definitions[widget_id]
        except KeyError as exc:
            raise ValueError(f"unknown widget ID: {widget_id}") from exc

    def visible_widgets(self) -> tuple[WidgetDefinition, ...]:
        """Return enabled/required widgets in deterministic controller order."""

        enabled = set(self._enabled_widget_ids)
        enabled.update(
            widget_id
            for widget_id, definition in self._definitions.items()
            if definition.required
        )

        resolved_order: list[str] = []
        for widget_id in (*self._widget_order, *self._registration_order):
            if widget_id in resolved_order:
                continue
            if widget_id not in self._definitions:
                continue
            if widget_id not in enabled:
                continue
            resolved_order.append(widget_id)

        if not resolved_order:
            raise ValueError("widget registry must expose at least one visible widget")
        return tuple(self._definitions[widget_id] for widget_id in resolved_order)


def _deduplicated_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    ordered: list[str] = []
    for value in values:
        _validate_identifier(value, "widget ID")
        if value not in ordered:
            ordered.append(value)
    return tuple(ordered)


def _validate_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _WIDGET_ID.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase identifier")


def _validate_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
