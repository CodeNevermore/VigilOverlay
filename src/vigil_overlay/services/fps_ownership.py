"""Lock-owned state for one PresentMon capture target."""

from __future__ import annotations

from dataclasses import dataclass, field

from vigil_overlay.services.fps import FpsTarget
from vigil_overlay.services.fps_sampling import (
    FpsStreamSelector,
    PresentMonCaptureDiagnostics,
)

FpsTargetIdentity = tuple[int, int | str]


@dataclass(slots=True)
class FpsCaptureOwnership:
    """Keep target verification, sampling, and diagnostics in one state boundary.

    The owning service must hold its lifecycle lock while reading or mutating this
    object. Discovery preferences intentionally live outside this boundary.
    """

    target: FpsTarget | None = None
    last_capture_diagnostics: PresentMonCaptureDiagnostics | None = None
    stream_identity: FpsTargetIdentity | None = None
    verified_target_identity: FpsTargetIdentity | None = None
    stream_selector: FpsStreamSelector = field(default_factory=FpsStreamSelector)

    def replace_target(self, target: FpsTarget | None) -> None:
        """Start a replacement generation, retaining streams only for the same target."""

        previous_identity = self.target.identity_key if self.target is not None else None
        target_identity = target.identity_key if target is not None else None
        self.target = target
        self.last_capture_diagnostics = None
        self.verified_target_identity = None
        if target is None or previous_identity is None or previous_identity != target_identity:
            self.stream_identity = target_identity
            self.stream_selector = FpsStreamSelector()

    def activate_candidate(self, target: FpsTarget) -> None:
        """Make a provisional candidate current without changing discovery policy."""

        self.target = target
        self.last_capture_diagnostics = None
        self.verified_target_identity = None

    def selector_for(self, target: FpsTarget) -> FpsStreamSelector:
        """Return sampling state owned exclusively by the supplied target identity."""

        if self.stream_identity != target.identity_key:
            self.stream_identity = target.identity_key
            self.stream_selector = FpsStreamSelector()
        return self.stream_selector

    def verify_if_current(self, target: FpsTarget) -> bool:
        """Grant persistent ownership only when the verified candidate is still current."""

        if not self.is_current(target):
            return False
        self.verified_target_identity = target.identity_key
        return True

    def retains_verified(self, target: FpsTarget) -> bool:
        return self.verified_target_identity == target.identity_key

    def record_diagnostics_if_current(
        self,
        target: FpsTarget,
        diagnostics: PresentMonCaptureDiagnostics,
    ) -> bool:
        if not self.is_current(target):
            return False
        self.last_capture_diagnostics = diagnostics
        return True

    def clear_if_current(self, target: FpsTarget) -> bool:
        """Release a finished current target while preserving its final diagnostics."""

        if not self.is_current(target):
            return False
        self.clear()
        return True

    def clear(self) -> None:
        """Release target and sampling ownership while preserving final diagnostics."""

        self.target = None
        self.stream_identity = None
        self.verified_target_identity = None
        self.stream_selector = FpsStreamSelector()

    def is_current(self, target: FpsTarget) -> bool:
        return self.target is not None and self.target.identity_key == target.identity_key
