"""Widget registration primitives for built-in and host-managed widgets."""

from vigil_overlay.widgets.builtins import built_in_widget_definitions
from vigil_overlay.widgets.registry import (
                                            WidgetDefinition,
                                            WidgetItemDefinition,
                                            WidgetRegistry,
                                            WidgetViewKind,
)

__all__ = [
    "WidgetDefinition",
    "WidgetItemDefinition",
    "WidgetRegistry",
    "WidgetViewKind",
    "built_in_widget_definitions",
]
