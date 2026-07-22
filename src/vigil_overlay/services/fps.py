"""Typed contracts for event-driven game FPS telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vigil_overlay.services.telemetry import PerformanceMetric, TelemetryMetricSnapshot


@dataclass(frozen=True, slots=True)
class FpsTarget:
    """One process selected for presentation telemetry."""

    process_id: int
    executable_name: str
    process_started_at_100ns: int | None = None

    def __post_init__(self) -> None:
        if self.process_id <= 0:
            raise ValueError("process_id must be positive")
        if not self.executable_name.strip():
            raise ValueError("executable_name must not be empty")
        if (
            self.process_started_at_100ns is not None
            and self.process_started_at_100ns <= 0
        ):
            raise ValueError("process_started_at_100ns must be positive when provided")

    @property
    def identity_key(self) -> tuple[int, int | str]:
        """Stable-enough process identity for one Vigil runtime session.

        Windows process IDs are reusable. Prefer the Win32 process-creation timestamp when
        available so a newly launched process that inherits an old PID is not mistaken for a
        previously rejected FPS target. The executable-name fallback keeps protected processes
        usable when Windows denies process-time queries.
        """

        identity: int | str = (
            self.process_started_at_100ns
            if self.process_started_at_100ns is not None
            else self.executable_name.casefold()
        )
        return (self.process_id, identity)


@dataclass(frozen=True, slots=True)
class PresentMonFrame:
    """Minimal frame record consumed from PresentMon CSV stdout."""

    process_id: int
    application: str
    displayed_time_ms: float
    swap_chain: str = "default"
    frame_type: str = ""

    def __post_init__(self) -> None:
        if self.process_id <= 0:
            raise ValueError("process_id must be positive")
        if not self.application.strip():
            raise ValueError("application must not be empty")
        if self.displayed_time_ms <= 0:
            raise ValueError("displayed_time_ms must be positive")
        if not self.swap_chain.strip():
            raise ValueError("swap_chain must not be empty")


@dataclass(frozen=True, slots=True)
class PresentMonRuntime:
    """Verified PresentMon executable selected for the broker."""

    executable: Path
    version: str
    sha256: str


@dataclass(frozen=True, slots=True)
class FpsMetricUpdate:
    """One published gamer-facing FPS metric."""

    metric: TelemetryMetricSnapshot
    target: FpsTarget | None = None

    def __post_init__(self) -> None:
        if self.metric.metric is not PerformanceMetric.FPS:
            raise ValueError("FpsMetricUpdate requires an FPS metric")
