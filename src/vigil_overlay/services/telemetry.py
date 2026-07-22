"""Typed telemetry contracts consumed by the Compact Mode Performance widget."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol


class PerformanceMetric(StrEnum):
    """Stable performance metric identities rendered by the UI."""

    CPU = "cpu"
    GPU = "gpu"
    VRAM = "vram"
    RAM = "ram"
    FPS = "fps"


@dataclass(frozen=True, slots=True)
class TelemetryMetricSnapshot:
    """One current metric plus bounded history (60 hardware or 240 FPS samples)."""

    metric: PerformanceMetric
    display_value: str
    numeric_value: float | None = None
    secondary_text: str = ""
    scale_min: float = 0.0
    scale_max: float = 100.0
    history: tuple[float | None, ...] = ()

    def __post_init__(self) -> None:
        if not self.display_value.strip():
            raise ValueError("display_value must not be empty")
        if self.scale_max <= self.scale_min:
            raise ValueError("scale_max must be greater than scale_min")
        history_limit = 240 if self.metric is PerformanceMetric.FPS else 60
        if len(self.history) > history_limit:
            raise ValueError(f"telemetry history cannot exceed {history_limit} samples")
        for sample in self.history:
            if sample is None:
                continue
            if sample < self.scale_min or sample > self.scale_max:
                raise ValueError(
                    "telemetry history sample is outside the declared scale"
                )


@dataclass(frozen=True, slots=True)
class TelemetrySnapshot:
    """Immutable normalized performance snapshot."""

    captured_at_utc: datetime
    metrics: tuple[TelemetryMetricSnapshot, ...]

    def __post_init__(self) -> None:
        if self.captured_at_utc.tzinfo is None:
            raise ValueError("captured_at_utc must be timezone-aware")
        metric_ids = tuple(metric.metric for metric in self.metrics)
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("telemetry snapshot contains duplicate metrics")

    def metric(self, metric: PerformanceMetric) -> TelemetryMetricSnapshot:
        for snapshot in self.metrics:
            if snapshot.metric is metric:
                return snapshot
        return unavailable_metric(metric)

    def with_metric(self, replacement: TelemetryMetricSnapshot) -> TelemetrySnapshot:
        """Return a snapshot with one metric replaced while preserving stable metric order."""

        metrics = tuple(
            replacement if metric.metric is replacement.metric else metric
            for metric in self.metrics
        )
        if all(metric.metric is not replacement.metric for metric in self.metrics):
            metrics = (*metrics, replacement)
        return TelemetrySnapshot(captured_at_utc=self.captured_at_utc, metrics=metrics)

    @classmethod
    def unavailable(cls) -> TelemetrySnapshot:
        return cls(
            captured_at_utc=datetime.now(UTC),
            metrics=tuple(unavailable_metric(metric) for metric in PerformanceMetric),
        )


@dataclass(frozen=True, slots=True)
class RawTelemetrySample:
    """One platform sample before display formatting and history accumulation."""

    captured_at_utc: datetime
    cpu_percent: float | None = None
    cpu_frequency_ghz: float | None = None
    gpu_percent: float | None = None
    gpu_name: str = ""
    vram_percent: float | None = None
    vram_used_bytes: int | None = None
    vram_total_bytes: int | None = None
    ram_percent: float | None = None
    ram_used_bytes: int | None = None
    ram_total_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.captured_at_utc.tzinfo is None:
            raise ValueError("captured_at_utc must be timezone-aware")
        for value in (
            self.cpu_percent,
            self.gpu_percent,
            self.vram_percent,
            self.ram_percent,
        ):
            if value is not None and not 0.0 <= value <= 100.0:
                raise ValueError(
                    "percentage telemetry values must be between 0 and 100"
                )
        if self.cpu_frequency_ghz is not None and self.cpu_frequency_ghz < 0:
            raise ValueError("cpu_frequency_ghz must not be negative")
        for value in (
            self.vram_used_bytes,
            self.vram_total_bytes,
            self.ram_used_bytes,
            self.ram_total_bytes,
        ):
            if value is not None and value < 0:
                raise ValueError("telemetry byte values must not be negative")


class RawTelemetrySampler(Protocol):
    """Platform sampler called only by the telemetry worker thread."""

    def sample(self) -> RawTelemetrySample:
        """Collect one platform sample."""

    def close(self) -> None:
        """Release native resources. Implementations must be idempotent."""


def unavailable_metric(metric: PerformanceMetric) -> TelemetryMetricSnapshot:
    """Return the normalized unavailable state for one metric."""

    maximum = 60.0 if metric is PerformanceMetric.FPS else 100.0
    return TelemetryMetricSnapshot(
        metric=metric,
        display_value="--",
        scale_min=0.0,
        scale_max=maximum,
    )
