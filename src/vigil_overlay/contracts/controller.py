"""Controller status contracts shared between input services and UI surfaces."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ControllerBatterySnapshot:
    """Battery state for the controller currently driving Vigil navigation."""

    connected: bool = False
    battery_present: bool | None = None
    battery_percent: int | None = None
    approximate_percent: bool = False
    level_label: str | None = None
