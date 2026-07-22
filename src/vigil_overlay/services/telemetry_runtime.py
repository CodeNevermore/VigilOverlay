"""Background telemetry polling and in-memory 60-second history."""

from __future__ import annotations

import logging
import sys
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Final

from PySide6.QtCore import QObject, Signal

from vigil_overlay.services.fps import FpsMetricUpdate
from vigil_overlay.services.telemetry import (
    PerformanceMetric,
    RawTelemetrySample,
    RawTelemetrySampler,
    TelemetryMetricSnapshot,
    TelemetrySnapshot,
)

_LOGGER = logging.getLogger("vigil_overlay")
_GIB: Final[float] = 1024.0**3


class TelemetryHistoryAccumulator:
    """Convert raw platform samples into immutable UI snapshots."""

    def __init__(self, *, history_limit: int = 60) -> None:
        if history_limit < 1 or history_limit > 60:
            raise ValueError("history_limit must be between 1 and 60")
        self._history: dict[PerformanceMetric, deque[float | None]] = {
            metric: deque(maxlen=history_limit) for metric in PerformanceMetric
        }

    def update(self, sample: RawTelemetrySample) -> TelemetrySnapshot:
        current = {
            PerformanceMetric.CPU: sample.cpu_percent,
            PerformanceMetric.GPU: sample.gpu_percent,
            PerformanceMetric.VRAM: sample.vram_percent,
            PerformanceMetric.RAM: sample.ram_percent,
            PerformanceMetric.FPS: None,
        }
        for metric, value in current.items():
            self._history[metric].append(value)

        return TelemetrySnapshot(
            captured_at_utc=sample.captured_at_utc,
            metrics=(
                _percent_metric(
                    PerformanceMetric.CPU,
                    sample.cpu_percent,
                    _format_frequency(sample.cpu_frequency_ghz),
                    tuple(self._history[PerformanceMetric.CPU]),
                ),
                _percent_metric(
                    PerformanceMetric.GPU,
                    sample.gpu_percent,
                    sample.gpu_name,
                    tuple(self._history[PerformanceMetric.GPU]),
                ),
                _percent_metric(
                    PerformanceMetric.VRAM,
                    sample.vram_percent,
                    _format_memory_pair(
                        sample.vram_used_bytes, sample.vram_total_bytes
                    ),
                    tuple(self._history[PerformanceMetric.VRAM]),
                ),
                _percent_metric(
                    PerformanceMetric.RAM,
                    sample.ram_percent,
                    _format_memory_pair(sample.ram_used_bytes, sample.ram_total_bytes),
                    tuple(self._history[PerformanceMetric.RAM]),
                ),
                TelemetryMetricSnapshot(
                    metric=PerformanceMetric.FPS,
                    display_value="--",
                    scale_min=0.0,
                    scale_max=240.0,
                    history=tuple(self._history[PerformanceMetric.FPS]),
                ),
            ),
        )


class TelemetryPollingService(QObject):
    """Poll a native sampler on a daemon thread and publish queued Qt snapshots."""

    snapshot_ready = Signal(object)

    def __init__(
        self,
        sampler: RawTelemetrySampler,
        *,
        interval_seconds: float = 1.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._sampler = sampler
        self._interval_seconds = interval_seconds
        self._accumulator = TelemetryHistoryAccumulator()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._latest = TelemetrySnapshot.unavailable()
        self._latest_fps_metric = self._latest.metric(PerformanceMetric.FPS)
        self._closed = False

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def snapshot(self) -> TelemetrySnapshot:
        with self._state_lock:
            return self._latest

    def apply_fps_update(self, update: FpsMetricUpdate) -> None:
        """Merge an event-driven FPS metric with the latest 1 Hz hardware snapshot."""

        with self._state_lock:
            self._latest_fps_metric = update.metric
            self._latest = self._latest.with_metric(update.metric)
            snapshot = self._latest
        self.snapshot_ready.emit(snapshot)

    def start(self) -> None:
        if self._closed or self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="VigilTelemetry",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._closed:
            return
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(2.0, self._interval_seconds * 2.0))
        self._thread = None
        try:
            self._sampler.close()
        except Exception:
            _LOGGER.exception("Telemetry sampler cleanup failed")
        self._closed = True

    def _run(self) -> None:
        # Native CPU and PDH rate counters need a baseline interval before the first value.
        if self._stop_event.wait(self._interval_seconds):
            return
        while not self._stop_event.is_set():
            try:
                raw = self._sampler.sample()
                snapshot = self._accumulator.update(raw)
                with self._state_lock:
                    snapshot = snapshot.with_metric(self._latest_fps_metric)
            except Exception:
                _LOGGER.exception(
                    "Telemetry sample failed; retaining last good snapshot"
                )
            else:
                with self._state_lock:
                    self._latest = snapshot
                self.snapshot_ready.emit(snapshot)
            if self._stop_event.wait(self._interval_seconds):
                break


def create_platform_telemetry_service() -> TelemetryPollingService:
    """Create the native Windows sampler or a non-fabricating fallback sampler."""

    if sys.platform == "win32":
        from vigil_overlay.services.windows_telemetry import WindowsTelemetrySampler

        try:
            return TelemetryPollingService(WindowsTelemetrySampler())
        except OSError:
            _LOGGER.exception("Native Windows telemetry initialization failed")
    return TelemetryPollingService(_UnavailableRawSampler())


class _UnavailableRawSampler:
    def sample(self) -> RawTelemetrySample:
        return RawTelemetrySample(captured_at_utc=datetime.now(UTC))

    def close(self) -> None:
        return


def _percent_metric(
    metric: PerformanceMetric,
    value: float | None,
    secondary_text: str,
    history: tuple[float | None, ...],
) -> TelemetryMetricSnapshot:
    display = "--" if value is None else f"{round(value):d}%"
    return TelemetryMetricSnapshot(
        metric=metric,
        display_value=display,
        numeric_value=value,
        secondary_text=secondary_text,
        scale_min=0.0,
        scale_max=100.0,
        history=history,
    )


def _format_frequency(value_ghz: float | None) -> str:
    if value_ghz is None:
        return ""
    return f"{value_ghz:.2f} GHz"


def _format_memory_pair(used_bytes: int | None, total_bytes: int | None) -> str:
    if used_bytes is None or total_bytes is None or total_bytes <= 0:
        return ""
    return f"{used_bytes / _GIB:.1f} / {total_bytes / _GIB:.1f} GB"
