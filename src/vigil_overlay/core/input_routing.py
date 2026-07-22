"""Deterministic overlay input-routing policy.

The policy selects exactly one navigation route while Vigil is visible. Native
controller commands and controller-mapped mouse input must never drive the UI at
the same time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OverlayInputMode(StrEnum):
    """High-level input ownership state for the visible overlay."""

    HIDDEN = "hidden"
    FOREGROUND_PENDING = "foreground_pending"
    MOUSE_KEYBOARD = "mouse_keyboard"
    MOUSE_PRIMARY = "mouse_primary"
    CONTROLLER_PRIMARY = "controller_primary"


@dataclass(frozen=True, slots=True)
class OverlayInputPolicy:
    """Resolved routing decisions consumed by application and window layers."""

    mode: OverlayInputMode
    route_native_controller_commands: bool
    allow_mouse_events_in_vigil: bool
    use_controller_correlated_mouse_guard: bool
    hold_gameinput_ownership: bool

    @property
    def controller_primary(self) -> bool:
        return self.mode is OverlayInputMode.CONTROLLER_PRIMARY


@dataclass(frozen=True, slots=True)
class InputControlDiagnostics:
    """Honest runtime status for the containment layers Vigil can actually prove."""

    mode: OverlayInputMode
    foreground_verification_required: bool
    foreground_verification_supported: bool
    foreground_verified: bool
    gameinput_exclusivity_active: bool
    mouse_keyboard_containment_supported: bool
    mouse_keyboard_containment_active: bool
    raw_input_isolation_available: bool = False
    xinput_isolation_available: bool = False


def foreground_pending_input_policy() -> OverlayInputPolicy:
    """Suspend visible-overlay navigation until Windows grants foreground ownership."""

    return OverlayInputPolicy(
        mode=OverlayInputMode.FOREGROUND_PENDING,
        route_native_controller_commands=False,
        allow_mouse_events_in_vigil=False,
        use_controller_correlated_mouse_guard=False,
        hold_gameinput_ownership=False,
    )


def resolve_overlay_input_policy(
    *,
    overlay_visible: bool,
    controller_connected: bool,
    allow_mouse_navigation_while_controller_connected: bool,
) -> OverlayInputPolicy:
    """Resolve one mutually exclusive overlay navigation route."""

    if not overlay_visible:
        return OverlayInputPolicy(
            mode=OverlayInputMode.HIDDEN,
            route_native_controller_commands=False,
            allow_mouse_events_in_vigil=True,
            use_controller_correlated_mouse_guard=False,
            hold_gameinput_ownership=False,
        )

    if not controller_connected:
        return OverlayInputPolicy(
            mode=OverlayInputMode.MOUSE_KEYBOARD,
            route_native_controller_commands=False,
            allow_mouse_events_in_vigil=True,
            use_controller_correlated_mouse_guard=False,
            hold_gameinput_ownership=False,
        )

    if allow_mouse_navigation_while_controller_connected:
        return OverlayInputPolicy(
            mode=OverlayInputMode.MOUSE_PRIMARY,
            route_native_controller_commands=False,
            allow_mouse_events_in_vigil=True,
            use_controller_correlated_mouse_guard=False,
            hold_gameinput_ownership=False,
        )

    return OverlayInputPolicy(
        mode=OverlayInputMode.CONTROLLER_PRIMARY,
        route_native_controller_commands=True,
        allow_mouse_events_in_vigil=False,
        use_controller_correlated_mouse_guard=True,
        hold_gameinput_ownership=True,
    )
