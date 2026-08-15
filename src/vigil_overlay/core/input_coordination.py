"""Foreground ownership and deterministic visible-overlay input coordination."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from vigil_overlay.core.input_routing import (
    InputControlDiagnostics,
    OverlayInputMode,
    OverlayInputPolicy,
    foreground_pending_input_policy,
    resolve_overlay_input_policy,
)

_LOGGER = logging.getLogger("vigil_overlay")
_DEFAULT_FOREGROUND_LOSS_CONFIRM_SECONDS = 0.20


class InputCoordinationWindow(Protocol):
    """Minimal host-window surface required by the coordinator."""

    def isVisible(self) -> bool: ...

    def winId(self) -> int: ...

    def apply_input_policy(self, policy: OverlayInputPolicy) -> None: ...


class ForegroundOwnership(Protocol):
    @property
    def required(self) -> bool: ...

    @property
    def supported(self) -> bool: ...

    @property
    def detail(self) -> str: ...

    def request(self, window_handle: int) -> bool: ...

    def verify(self) -> bool: ...

    def release(self) -> None: ...


class ForegroundMonitor(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


class ControllerCommandGate(Protocol):
    def require_neutral_before_commands(self) -> None: ...


class GuideOwnership(Protocol):
    def set_controller_ownership_active(self, active: bool) -> None: ...


class ControllerInputOwnership(Protocol):
    @property
    def active(self) -> bool: ...

    def activate(self, *, background_guide_enabled: bool) -> None: ...

    def deactivate(self) -> None: ...


class InputContainment(Protocol):
    @property
    def supported(self) -> bool: ...

    @property
    def active(self) -> bool: ...

    def apply_policy(self, policy: OverlayInputPolicy) -> bool: ...


class InputOwnershipCoordinator:
    """Own foreground leasing, input policy, and native route transitions."""

    def __init__(
        self,
        *,
        window: InputCoordinationWindow,
        foreground_ownership: ForegroundOwnership,
        foreground_monitor: ForegroundMonitor,
        controller_commands: ControllerCommandGate,
        guide_ownership: GuideOwnership,
        controller_ownership: ControllerInputOwnership,
        input_containment: InputContainment,
        allow_mouse_navigation: Callable[[], bool],
        background_guide_enabled: Callable[[], bool],
        clock: Callable[[], float],
        claim_timeout_seconds: float,
        loss_confirm_seconds: float = _DEFAULT_FOREGROUND_LOSS_CONFIRM_SECONDS,
    ) -> None:
        if claim_timeout_seconds <= 0:
            raise ValueError("claim_timeout_seconds must be positive")
        if loss_confirm_seconds <= 0:
            raise ValueError("loss_confirm_seconds must be positive")
        self._window = window
        self._foreground_ownership = foreground_ownership
        self._foreground_monitor = foreground_monitor
        self._controller_commands = controller_commands
        self._guide_ownership = guide_ownership
        self._controller_ownership = controller_ownership
        self._input_containment = input_containment
        self._allow_mouse_navigation = allow_mouse_navigation
        self._background_guide_enabled = background_guide_enabled
        self._clock = clock
        self._claim_timeout_seconds = claim_timeout_seconds
        self._loss_confirm_seconds = loss_confirm_seconds
        self._claim_deadline = 0.0
        self._claim_pending = False
        self._loss_confirming = False
        self._foreground_verified = False
        self._controller_connected = False
        self._controller_commands_ready = True
        self._policy = resolve_overlay_input_policy(
            overlay_visible=False,
            controller_connected=False,
            allow_mouse_navigation_while_controller_connected=False,
        )

    @property
    def policy(self) -> OverlayInputPolicy:
        return self._policy

    @property
    def controller_commands_allowed(self) -> bool:
        return self._policy.route_native_controller_commands and self._controller_commands_ready

    @property
    def diagnostics(self) -> InputControlDiagnostics:
        return InputControlDiagnostics(
            mode=self._policy.mode,
            foreground_verification_required=self._foreground_ownership.required,
            foreground_verification_supported=self._foreground_ownership.supported,
            foreground_verified=self._foreground_verified,
            gameinput_exclusivity_active=self._controller_ownership.active,
            mouse_keyboard_containment_supported=self._input_containment.supported,
            mouse_keyboard_containment_active=self._input_containment.active,
            xinput_isolation_available=False,
        )

    def set_controller_connected(self, connected: bool) -> OverlayInputPolicy:
        self._controller_connected = bool(connected)
        return self.sync_policy()

    def mark_controller_commands_rearmed(self) -> None:
        """Open the current native route after the controller becomes neutral."""

        self._controller_commands_ready = True

    def sync_policy(self, *, overlay_visible: bool | None = None) -> OverlayInputPolicy:
        """Resolve and apply exactly one route for the current host state."""

        visible = self._window.isVisible() if overlay_visible is None else overlay_visible
        if visible and self._foreground_ownership.required and not self._foreground_verified:
            policy = foreground_pending_input_policy()
        else:
            policy = resolve_overlay_input_policy(
                overlay_visible=visible,
                controller_connected=self._controller_connected,
                allow_mouse_navigation_while_controller_connected=(self._allow_mouse_navigation()),
            )
        previous = self._policy
        if policy == previous:
            self._apply_runtime_policy(policy)
            return policy

        entering_native_controller_route = (
            policy.route_native_controller_commands
            and not previous.route_native_controller_commands
        )
        if entering_native_controller_route:
            self._controller_commands_ready = False
            self._controller_commands.require_neutral_before_commands()
        elif not policy.route_native_controller_commands:
            self._controller_commands_ready = True

        self._policy = policy
        self._apply_runtime_policy(policy)
        _LOGGER.debug("Overlay input mode changed: %s -> %s", previous.mode, policy.mode)
        return policy

    def begin_foreground_lease(self) -> None:
        """Suspend exclusive input until native foreground ownership is proven."""

        service = self._foreground_ownership
        service.release()
        self._foreground_verified = False
        self._loss_confirming = False
        if not service.required:
            self._claim_pending = False
            self.sync_policy(overlay_visible=True)
            _LOGGER.debug(
                "Foreground verification unavailable; using portable input routing: %s",
                service.detail,
            )
            return

        self._claim_pending = True
        self._claim_deadline = self._clock() + self._claim_timeout_seconds
        self._controller_commands_ready = False
        self.sync_policy(overlay_visible=True)
        service.request(int(self._window.winId()))
        self._foreground_monitor.start()
        self.reconcile_foreground_ownership()

    def reconcile_foreground_ownership(self) -> None:
        """Acquire or monitor foreground without hiding the visible overlay."""

        service = self._foreground_ownership
        if not service.required:
            return
        if not (self._claim_pending or self._foreground_verified):
            return
        if not self._window.isVisible():
            self.release_visible_control("overlay no longer visible")
            return
        if service.verify():
            if self._foreground_verified:
                return
            restored_after_transition = self._loss_confirming
            self._foreground_verified = True
            self._claim_pending = False
            self._loss_confirming = False
            self.sync_policy(overlay_visible=True)
            reason = (
                "foreground lease restored after transient transition"
                if restored_after_transition
                else "foreground lease acquired"
            )
            self._log_status(reason)
            return

        if self._foreground_verified:
            _LOGGER.warning(
                "Vigil no longer owns foreground; suspending input control while the "
                "foreground transition is confirmed: %s",
                service.detail,
            )
            self._suspend_for_foreground_recheck()
            return

        if self._claim_pending and self._clock() >= self._claim_deadline:
            confirmed_loss = self._loss_confirming
            _LOGGER.warning(
                "%s; input remains suspended and the visible overlay will retry activation: %s",
                (
                    "Vigil foreground loss was confirmed"
                    if confirmed_loss
                    else "Vigil could not verify foreground ownership"
                ),
                service.detail,
            )
            self._claim_deadline = self._clock() + self._claim_timeout_seconds
            service.request(int(self._window.winId()))

    def release_visible_control(self, reason: str) -> None:
        """Drop foreground, controller, and containment state before hiding."""

        had_lease_state = self._claim_pending or self._foreground_verified
        self._foreground_monitor.stop()
        self._foreground_ownership.release()
        self._claim_pending = False
        self._loss_confirming = False
        self._foreground_verified = False
        self.sync_policy(overlay_visible=False)
        if had_lease_state:
            self._log_status(reason)

    def _suspend_for_foreground_recheck(self) -> None:
        self._foreground_ownership.release()
        self._foreground_verified = False
        self._claim_pending = True
        self._loss_confirming = True
        self._claim_deadline = self._clock() + self._loss_confirm_seconds
        self.sync_policy(overlay_visible=True)
        self._log_status("foreground transition suspended")

    def _apply_runtime_policy(self, policy: OverlayInputPolicy) -> None:
        if policy.mode is not OverlayInputMode.CONTROLLER_PRIMARY:
            self._input_containment.apply_policy(policy)

        self._window.apply_input_policy(policy)
        if policy.hold_gameinput_ownership:
            self._guide_ownership.set_controller_ownership_active(True)
            self._controller_ownership.activate(
                background_guide_enabled=self._background_guide_enabled()
            )
        else:
            self._controller_ownership.deactivate()
            self._guide_ownership.set_controller_ownership_active(False)

        if policy.mode is OverlayInputMode.CONTROLLER_PRIMARY:
            self._input_containment.apply_policy(policy)

    def _log_status(self, reason: str) -> None:
        status = self.diagnostics
        _LOGGER.info(
            "Input control status (%s): mode=%s foreground_required=%s "
            "foreground_supported=%s foreground_verified=%s gameinput_exclusive=%s "
            "low_level_supported=%s low_level_containment=%s "
            "raw_input_isolation=%s xinput_isolation=%s",
            reason,
            status.mode.value,
            status.foreground_verification_required,
            status.foreground_verification_supported,
            status.foreground_verified,
            status.gameinput_exclusivity_active,
            status.mouse_keyboard_containment_supported,
            status.mouse_keyboard_containment_active,
            status.raw_input_isolation_available,
            status.xinput_isolation_available,
        )
        if (
            status.mode is OverlayInputMode.CONTROLLER_PRIMARY
            and not status.gameinput_exclusivity_active
        ):
            _LOGGER.warning(
                "Controller-primary is using the XInput compatibility route; GameInput "
                "exclusivity is unavailable and background controller consumers may react"
            )


__all__ = ["InputOwnershipCoordinator"]
