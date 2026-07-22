"""Policy-driven native input containment for the visible Vigil overlay.

Routing decides which input source may navigate Vigil. This module independently
contains ordinary Windows mouse and keyboard input without changing that route.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from vigil_overlay.core.input_routing import OverlayInputMode, OverlayInputPolicy

_LOGGER = logging.getLogger("vigil_overlay")

_MOUSE_INJECTED: Final[int] = 0x00000001
_MOUSE_LOWER_IL_INJECTED: Final[int] = 0x00000002
_KEYBOARD_LOWER_IL_INJECTED: Final[int] = 0x00000002
_KEYBOARD_INJECTED: Final[int] = 0x00000010


class InputInjectionClass(StrEnum):
    """Windows low-level-hook provenance classification when flags expose it."""

    PHYSICAL = "physical"
    INJECTED = "injected"
    LOWER_INTEGRITY_INJECTED = "lower_integrity_injected"


@dataclass(frozen=True, slots=True)
class InputContainmentPlan:
    """Native hook decisions derived from an already-resolved overlay route."""

    mode: OverlayInputMode
    install_hooks: bool
    swallow_mouse: bool
    swallow_injected_keyboard: bool


@dataclass(frozen=True, slots=True)
class HookDiagnosticRecord:
    """Small sanitized record safe to enqueue from a low-level hook callback."""

    source: str
    classification: InputInjectionClass
    swallowed: bool


class InputContainmentBackend(Protocol):
    """Native hook backend contract kept independent from Qt/application objects."""

    @property
    def supported(self) -> bool: ...

    @property
    def detail(self) -> str: ...

    @property
    def healthy(self) -> bool: ...

    def start(self, plan: InputContainmentPlan) -> bool: ...

    def update_plan(self, plan: InputContainmentPlan) -> None: ...

    def stop(self) -> None: ...

    def drain_diagnostics(self) -> tuple[HookDiagnosticRecord, ...]: ...


class UnsupportedInputContainmentBackend:
    """Fail-open backend used when native Windows low-level hooks are unavailable."""

    @property
    def supported(self) -> bool:
        return False

    @property
    def detail(self) -> str:
        return "Native Windows input containment is unavailable on this platform"

    @property
    def healthy(self) -> bool:
        return False

    def start(self, plan: InputContainmentPlan) -> bool:
        del plan
        return False

    def update_plan(self, plan: InputContainmentPlan) -> None:
        del plan

    def stop(self) -> None:
        return

    def drain_diagnostics(self) -> tuple[HookDiagnosticRecord, ...]:
        return ()


def classify_mouse_hook_flags(flags: int) -> InputInjectionClass:
    """Classify MSLLHOOKSTRUCT flags without treating provenance as identity proof."""

    if flags & _MOUSE_LOWER_IL_INJECTED:
        return InputInjectionClass.LOWER_INTEGRITY_INJECTED
    if flags & _MOUSE_INJECTED:
        return InputInjectionClass.INJECTED
    return InputInjectionClass.PHYSICAL


def classify_keyboard_hook_flags(flags: int) -> InputInjectionClass:
    """Classify KBDLLHOOKSTRUCT flags without assuming every mapper sets them."""

    if flags & _KEYBOARD_LOWER_IL_INJECTED:
        return InputInjectionClass.LOWER_INTEGRITY_INJECTED
    if flags & _KEYBOARD_INJECTED:
        return InputInjectionClass.INJECTED
    return InputInjectionClass.PHYSICAL


def resolve_input_containment_plan(policy: OverlayInputPolicy) -> InputContainmentPlan:
    """Map deterministic routing to the minimum native containment needed.

    Controller-primary is the only mode that requires global low-level hooks:
    mouse is not a valid Vigil route there, and injected keyboard events are treated
    as likely controller-mapper duplicates. Physical keyboard remains available as
    the intentional recovery/fallback route.

    Mouse-primary and mouse/keyboard modes deliberately do not swallow the input
    Vigil itself needs. Their ordinary Win32 interaction is contained by the visible
    fullscreen overlay surfaces; Raw Input/XInput leakage remains a physical-probe
    question rather than a claim made here.
    """

    controller_primary = policy.mode is OverlayInputMode.CONTROLLER_PRIMARY
    return InputContainmentPlan(
        mode=policy.mode,
        install_hooks=controller_primary,
        swallow_mouse=controller_primary,
        swallow_injected_keyboard=controller_primary,
    )


def should_swallow_mouse(
    plan: InputContainmentPlan,
    classification: InputInjectionClass,
) -> bool:
    """Return whether a mouse event is blocked before Windows dispatches it."""

    del classification
    return plan.install_hooks and plan.swallow_mouse


def should_swallow_keyboard(
    plan: InputContainmentPlan,
    classification: InputInjectionClass,
) -> bool:
    """Block flagged mapper-style keyboard injection while preserving physical keys."""

    return (
        plan.install_hooks
        and plan.swallow_injected_keyboard
        and classification is not InputInjectionClass.PHYSICAL
    )


class InputContainmentService:
    """Synchronize a fail-open native backend with the current overlay input policy."""

    def __init__(
        self,
        backend: InputContainmentBackend,
        *,
        retry_delay_seconds: float = 1.0,
        max_retry_attempts: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if retry_delay_seconds <= 0:
            raise ValueError("retry_delay_seconds must be positive")
        if max_retry_attempts < 0:
            raise ValueError("max_retry_attempts cannot be negative")
        self._backend = backend
        self._active = False
        self._last_plan: InputContainmentPlan | None = None
        self._retry_delay_seconds = retry_delay_seconds
        self._max_retry_attempts = max_retry_attempts
        self._clock = clock
        self._retry_attempts = 0
        self._next_retry_at = 0.0

    @property
    def active(self) -> bool:
        self._refresh_backend_health()
        return self._active

    @property
    def supported(self) -> bool:
        return self._backend.supported

    @property
    def detail(self) -> str:
        return self._backend.detail

    def apply_policy(self, policy: OverlayInputPolicy) -> bool:
        """Apply containment for one resolved route; failures always leave input open."""

        plan = resolve_input_containment_plan(policy)
        if not plan.install_hooks:
            self._stop_backend()
            self._last_plan = plan
            self._reset_retry_state()
            return False

        if self._refresh_backend_health():
            try:
                self._backend.update_plan(plan)
            except Exception:  # Native teardown/reconfiguration must never trap input.
                _LOGGER.exception(
                    "Native input containment update failed; releasing hooks fail-open"
                )
                self._stop_backend()
            self._last_plan = plan
            return self._active

        if self._last_plan == plan:
            return self.maintain()

        self._last_plan = plan
        self._reset_retry_state()
        return self._start_backend(plan, retry=False)

    def maintain(self, *, now: float | None = None) -> bool:
        """Observe asynchronous backend death and perform bounded fail-open recovery."""

        if self._refresh_backend_health():
            return True
        plan = self._last_plan
        if plan is None or not plan.install_hooks:
            return False
        observed_at = self._clock() if now is None else now
        if observed_at < self._next_retry_at:
            return False
        if self._retry_attempts >= self._max_retry_attempts:
            return False
        return self._start_backend(plan, retry=True)

    def _start_backend(self, plan: InputContainmentPlan, *, retry: bool) -> bool:
        if retry:
            self._retry_attempts += 1
        try:
            started = self._backend.start(plan)
        except Exception:  # Native hook installation is an optional containment layer.
            _LOGGER.exception(
                "Native input containment hook installation failed; continuing fail-open"
            )
            self._active = False
            self._cleanup_failed_start()
            self._schedule_retry()
            return False

        self._active = bool(started)
        if not self._active:
            self._cleanup_failed_start()
            self._schedule_retry()
        if self._active:
            self._reset_retry_state()
            _LOGGER.info("Native input containment active: %s", self._backend.detail)
        else:
            _LOGGER.warning(
                "Native input containment unavailable; continuing fail-open: %s",
                self._backend.detail,
            )
        return self._active

    def stop(self) -> None:
        """Release native containment. Safe and idempotent during shutdown."""

        self._stop_backend()
        self._last_plan = None
        self._reset_retry_state()

    def _refresh_backend_health(self) -> bool:
        if not self._active:
            return False
        try:
            healthy = self._backend.healthy
        except Exception:
            healthy = False
            _LOGGER.exception("Could not read native input-containment health")
        if healthy:
            return True
        _LOGGER.error(
            "Native input containment stopped unexpectedly; remaining fail-open and scheduling "
            "bounded recovery"
        )
        self._active = False
        self._cleanup_failed_start()
        self._log_diagnostic_summary()
        self._retry_attempts = 0
        self._schedule_retry()
        return False

    def _schedule_retry(self) -> None:
        exponent = max(self._retry_attempts - 1, 0)
        delay = min(self._retry_delay_seconds * (2**exponent), 30.0)
        self._next_retry_at = self._clock() + delay

    def _reset_retry_state(self) -> None:
        self._retry_attempts = 0
        self._next_retry_at = 0.0

    def _cleanup_failed_start(self) -> None:
        """Best-effort unwind for a backend that failed after a partial hook install."""

        try:
            self._backend.stop()
        except Exception:
            _LOGGER.exception(
                "Native input containment failed-start cleanup reported an error; "
                "continuing fail-open"
            )

    def _stop_backend(self) -> None:
        if not self._active:
            return
        try:
            self._backend.stop()
        except Exception:
            # Backends are required to attempt unhook before surfacing teardown errors.
            # Never propagate a native cleanup exception into the Qt shutdown path.
            _LOGGER.exception(
                "Native input containment teardown reported an error; continuing shutdown"
            )
        finally:
            self._active = False
            self._log_diagnostic_summary()

    def _log_diagnostic_summary(self) -> None:
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        try:
            records = self._backend.drain_diagnostics()
        except Exception:
            _LOGGER.exception("Could not drain native input-containment diagnostics")
            return
        if not records:
            return
        counts: dict[tuple[str, InputInjectionClass, bool], int] = {}
        for record in records:
            key = (record.source, record.classification, record.swallowed)
            counts[key] = counts.get(key, 0) + 1
        summary = ", ".join(
            f"{source}/{classification.value}/swallowed={swallowed}:{count}"
            for (source, classification, swallowed), count in sorted(
                counts.items(),
                key=lambda item: (item[0][0], item[0][1].value, item[0][2]),
            )
        )
        _LOGGER.debug("Native input containment diagnostic summary: %s", summary)


def create_platform_input_containment_service() -> InputContainmentService:
    """Create the Windows low-level-hook service or a safe no-op fallback."""

    if sys.platform != "win32":
        return InputContainmentService(UnsupportedInputContainmentBackend())

    try:
        from vigil_overlay.services.windows_input_containment import (
            WindowsLowLevelHookContainmentBackend,
        )

        return InputContainmentService(WindowsLowLevelHookContainmentBackend())
    except OSError as exc:
        _LOGGER.warning(
            "Native Windows input containment could not initialize: %s", exc
        )
        return InputContainmentService(UnsupportedInputContainmentBackend())
