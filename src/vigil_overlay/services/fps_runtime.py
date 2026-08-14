"""PresentMon-backed persistent FPS session lifecycle and aggregation."""

from __future__ import annotations

import csv
import logging
import math
import os
import queue
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Final, Protocol, TextIO

from PySide6.QtCore import QObject, Signal

from vigil_overlay.contracts.games import GameRecord
from vigil_overlay.core.paths import ApplicationPaths
from vigil_overlay.services.fps import FpsMetricUpdate, FpsTarget, PresentMonFrame
from vigil_overlay.services.fps_learning import LearnedFpsGameCache
from vigil_overlay.services.fps_targeting import FpsCandidateSelector
from vigil_overlay.services.presentmon_runtime import (
    PresentMonRuntimeError,
    PresentMonRuntimeManager,
)
from vigil_overlay.services.telemetry import PerformanceMetric, TelemetryMetricSnapshot

_LOGGER = logging.getLogger("vigil_overlay")
_CURRENT_WINDOW_MS: Final[float] = 500.0
_AVERAGE_WINDOW_MS: Final[float] = 60_000.0
_PUBLISH_INTERVAL_SECONDS: Final[float] = 0.25
_STALE_AFTER_SECONDS: Final[float] = 1.5
_FPS_HISTORY_LIMIT: Final[int] = 240
_DEFAULT_NO_FRAME_TIMEOUT_SECONDS: Final[float] = 4.0
_DEFAULT_FRAME_STALL_TIMEOUT_SECONDS: Final[float] = 5.0
_DEFAULT_TARGET_RETRY_DELAY_SECONDS: Final[float] = 5.0
_DEFAULT_REJECTED_RESCAN_DELAY_SECONDS: Final[float] = 5.0
_REJECTED_RESCAN_DELAY_MULTIPLIERS: Final[tuple[float, ...]] = (1.0, 6.0, 24.0)
_DEFAULT_GPU_PROBE_INTERVAL_SECONDS: Final[float] = 10.0
_DEFAULT_GPU_ACTIVITY_THRESHOLD_PERCENT: Final[float] = 5.0
_DEFAULT_GPU_SAFETY_RETRY_SECONDS: Final[float] = 120.0
_GPU_ACTIVITY_SAMPLE_COUNT: Final[int] = 2
_DEFAULT_MAX_STALL_RESTARTS: Final[int] = 3
_DEFAULT_UNEXPECTED_FAILURE_RETRY_DELAYS_SECONDS: Final[tuple[float, ...]] = (
    0.5,
    1.0,
    2.0,
)
_MAX_DISCOVERED_TARGETS: Final[int] = 8
_RAW_PROVIDER_CANDIDATE_LIMIT: Final[int] = 32
_VERIFICATION_FRAME_COUNT: Final[int] = 3
_TARGET_LIVENESS_POLL_SECONDS: Final[float] = 1.0
_STDERR_TAIL_LINES: Final[int] = 32
_STDERR_LINE_LIMIT: Final[int] = 1_000


class ProcessGpuUsageProbe(Protocol):
    """Low-duty process-scoped GPU activity source used only to wake parked targets."""

    def sample(self, process_ids: frozenset[int]) -> dict[int, float]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _CandidateSnapshot:
    candidates: tuple[FpsTarget, ...]
    visible_identities: frozenset[tuple[int, int | str]]


@dataclass(frozen=True, slots=True)
class PresentMonCaptureDiagnostics:
    """Bounded evidence explaining why a PresentMon target did not yield FPS."""

    target: FpsTarget
    lines_seen: int
    header_columns: tuple[str, ...]
    data_rows_seen: int
    accepted_frames: int
    rejection_counts: tuple[tuple[str, int], ...]
    exit_code: int | None
    stderr_tail: tuple[str, ...]

    @property
    def permission_required(self) -> bool:
        return _presentmon_permission_required(self.stderr_tail)

    @property
    def no_frame_status(self) -> str:
        if self.permission_required:
            return "FPS PERMISSION REQUIRED"
        rejection_counts = dict(self.rejection_counts)
        if not self.lines_seen:
            return "NO FPS DATA RECEIVED"
        if not self.header_columns and self.lines_seen:
            return "PRESENTMON OUTPUT UNSUPPORTED"
        if not self.data_rows_seen:
            return "NO FPS FRAME DATA RECEIVED"
        if rejection_counts.get("other_process", 0):
            return "FPS ROWS BELONG TO ANOTHER PROCESS"
        rejected_rows = sum(
            count
            for reason, count in self.rejection_counts
            if reason not in {"blank", "before_header"}
        )
        if self.data_rows_seen and rejected_rows:
            return "PRESENTMON ROWS REJECTED"
        return "NO USABLE FPS FRAMES RECEIVED"

    @property
    def summary(self) -> str:
        columns = ",".join(self.header_columns) if self.header_columns else "<none>"
        rejections = ",".join(f"{reason}:{count}" for reason, count in self.rejection_counts)
        stderr = " | ".join(self.stderr_tail) if self.stderr_tail else "<empty>"
        return (
            f"lines={self.lines_seen} header={columns} rows={self.data_rows_seen} "
            f"accepted={self.accepted_frames} rejected={rejections or '<none>'} "
            f"exit={self.exit_code!r} stderr={stderr}"
        )


class PresentMonCsvParser:
    """Incrementally parse PresentMon v2 CSV stdout without trusting fixed column order."""

    REQUIRED_COLUMNS = frozenset({"Application", "ProcessID", "DisplayedTime"})

    def __init__(self, *, target_pid: int) -> None:
        if target_pid <= 0:
            raise ValueError("target_pid must be positive")
        self._target_pid = target_pid
        self._header: tuple[str, ...] = ()
        self._lines_seen = 0
        self._data_rows_seen = 0
        self._accepted_frames = 0
        self._rejections: Counter[str] = Counter()

    def parse_line(self, line: str) -> PresentMonFrame | None:
        stripped = line.strip().lstrip("\ufeff")
        if not stripped:
            self._rejections["blank"] += 1
            return None
        self._lines_seen += 1
        try:
            fields = next(csv.reader([stripped]))
        except csv.Error:
            self._rejections["invalid_csv"] += 1
            return None
        if not self._header:
            if self.REQUIRED_COLUMNS.issubset(fields):
                self._header = tuple(fields)
            else:
                self._rejections["before_header"] += 1
            return None
        self._data_rows_seen += 1
        if len(fields) != len(self._header):
            self._rejections["column_count"] += 1
            return None
        row = dict(zip(self._header, fields, strict=True))
        try:
            process_id = int(row["ProcessID"])
        except (KeyError, ValueError):
            self._rejections["process_id"] += 1
            return None
        if process_id != self._target_pid:
            self._rejections["other_process"] += 1
            return None
        application = row.get("Application", "").strip()
        displayed_raw = row.get("DisplayedTime", "").strip()
        if not application:
            self._rejections["application"] += 1
            return None
        if not displayed_raw or displayed_raw.casefold() in {"na", "n/a"}:
            self._rejections["displayed_time_missing"] += 1
            return None
        try:
            displayed_time_ms = float(displayed_raw)
        except ValueError:
            self._rejections["displayed_time_invalid"] += 1
            return None
        if not math.isfinite(displayed_time_ms) or not 0.01 <= displayed_time_ms <= 10_000.0:
            self._rejections["displayed_time_range"] += 1
            return None
        frame = PresentMonFrame(
            process_id=process_id,
            application=application,
            displayed_time_ms=displayed_time_ms,
            swap_chain=row.get("SwapChainAddress", "").strip() or "default",
            frame_type=row.get("FrameType", "").strip(),
        )
        self._accepted_frames += 1
        return frame

    def diagnostics(
        self,
        target: FpsTarget,
        *,
        exit_code: int | None,
        stderr_tail: tuple[str, ...],
    ) -> PresentMonCaptureDiagnostics:
        return PresentMonCaptureDiagnostics(
            target=target,
            lines_seen=self._lines_seen,
            header_columns=self._header,
            data_rows_seen=self._data_rows_seen,
            accepted_frames=self._accepted_frames,
            rejection_counts=tuple(sorted(self._rejections.items())),
            exit_code=exit_code,
            stderr_tail=stderr_tail,
        )


class FpsWindowAccumulator:
    """Calculate displayed FPS from frame display durations with bounded 60-second history."""

    def __init__(self) -> None:
        self._frames: deque[tuple[float, float]] = deque()
        self._display_clock_ms = 0.0
        self._session_frame_count = 0
        self._session_displayed_time_ms = 0.0
        self._last_event_wall: float | None = None
        self._history: deque[float | None] = deque(maxlen=_FPS_HISTORY_LIMIT)

    @property
    def history(self) -> tuple[float | None, ...]:
        return tuple(self._history)

    def reset(self) -> None:
        self._frames.clear()
        self._display_clock_ms = 0.0
        self._session_frame_count = 0
        self._session_displayed_time_ms = 0.0
        self._last_event_wall = None
        self._history.clear()

    def ingest(self, frame: PresentMonFrame, *, observed_at: float | None = None) -> None:
        wall = time.monotonic() if observed_at is None else observed_at
        self._display_clock_ms += frame.displayed_time_ms
        self._session_frame_count += 1
        self._session_displayed_time_ms += frame.displayed_time_ms
        self._frames.append((self._display_clock_ms, frame.displayed_time_ms))
        self._last_event_wall = wall
        cutoff = self._display_clock_ms - _AVERAGE_WINDOW_MS
        while self._frames and self._frames[0][0] < cutoff:
            self._frames.popleft()

    def metric(
        self,
        *,
        now: float | None = None,
        append_history: bool = True,
        allow_stale: bool = False,
    ) -> TelemetryMetricSnapshot:
        wall = time.monotonic() if now is None else now
        stale = self._last_event_wall is None or wall - self._last_event_wall > _STALE_AFTER_SECONDS
        if stale and not allow_stale:
            if append_history:
                self._history.append(None)
            return _unavailable_fps_metric(self.history)

        current = self._calculate_window(_CURRENT_WINDOW_MS)
        average = self._calculate_session_average()
        if current is None:
            if append_history:
                self._history.append(None)
            return _unavailable_fps_metric(self.history)
        if append_history:
            self._history.append(current)
        history = self.history
        scale_max = _fps_scale_max(current, history)
        average_text = "" if average is None else f"AVG FPS {round(average):d}"
        secondary = (
            ("LAST FPS" if not average_text else f"LAST FPS · {average_text}")
            if stale
            else average_text
        )
        return TelemetryMetricSnapshot(
            metric=PerformanceMetric.FPS,
            display_value=f"{round(current):d}",
            numeric_value=current,
            secondary_text=secondary,
            scale_min=0.0,
            scale_max=scale_max,
            history=history,
        )

    def is_fresh(self, *, now: float | None = None) -> bool:
        wall = time.monotonic() if now is None else now
        return (
            self._last_event_wall is not None
            and wall - self._last_event_wall <= _STALE_AFTER_SECONDS
        )

    def recent_frame_count(self, window_ms: float = 1000.0) -> int:
        if not self._frames:
            return 0
        cutoff = self._display_clock_ms - window_ms
        return sum(1 for end_time, _duration in self._frames if end_time > cutoff)

    def _calculate_window(self, window_ms: float) -> float | None:
        if not self._frames:
            return None
        cutoff = self._display_clock_ms - window_ms
        durations = [duration for end_time, duration in self._frames if end_time > cutoff]
        total = sum(durations)
        if not durations or total <= 0:
            return None
        return 1000.0 * len(durations) / total

    def _calculate_session_average(self) -> float | None:
        if self._session_frame_count <= 0 or self._session_displayed_time_ms <= 0:
            return None
        return 1000.0 * self._session_frame_count / self._session_displayed_time_ms


class FpsStreamSelector:
    """Keep independent swap-chain histories and select the dominant presentation stream."""

    def __init__(self) -> None:
        self._streams: dict[str, FpsWindowAccumulator] = {}
        self._selected_stream: str | None = None

    def ingest(self, frame: PresentMonFrame, *, observed_at: float | None = None) -> None:
        accumulator = self._streams.setdefault(frame.swap_chain, FpsWindowAccumulator())
        accumulator.ingest(frame, observed_at=observed_at)
        if self._selected_stream is None:
            self._selected_stream = frame.swap_chain

    def metric(self, *, now: float | None = None) -> TelemetryMetricSnapshot:
        wall = time.monotonic() if now is None else now
        active_streams = [
            (stream_id, stream)
            for stream_id, stream in self._streams.items()
            if stream.is_fresh(now=wall)
        ]
        if not active_streams:
            if self._selected_stream is None:
                return _unavailable_fps_metric(())
            selected = self._streams.get(self._selected_stream)
            if selected is None:
                return _unavailable_fps_metric(())
            return selected.metric(
                now=wall,
                append_history=False,
                allow_stale=True,
            )
        selected_id, selected = max(
            active_streams,
            key=lambda item: item[1].recent_frame_count(1000.0),
        )
        self._selected_stream = selected_id
        return selected.metric(now=wall)


class _CaptureOutcome(StrEnum):
    CANCELLED = "cancelled"
    NO_FRAMES = "no_frames"
    COMPLETED_WITH_FRAMES = "completed_with_frames"
    COLLECTOR_FAILED = "collector_failed"
    PERMISSION_REQUIRED = "permission_required"
    STALLED = "stalled"


class PresentMonFpsService(QObject):
    """Own one frame-verified game's persistent PresentMon capture."""

    metric_ready = Signal(object)
    failure_ready = Signal(str)

    def __init__(
        self,
        runtime_manager: PresentMonRuntimeManager,
        *,
        publish_interval_seconds: float = _PUBLISH_INTERVAL_SECONDS,
        no_frame_timeout_seconds: float = _DEFAULT_NO_FRAME_TIMEOUT_SECONDS,
        frame_stall_timeout_seconds: float = _DEFAULT_FRAME_STALL_TIMEOUT_SECONDS,
        target_retry_delay_seconds: float = _DEFAULT_TARGET_RETRY_DELAY_SECONDS,
        rejected_rescan_delay_seconds: float = _DEFAULT_REJECTED_RESCAN_DELAY_SECONDS,
        max_stall_restarts: int = _DEFAULT_MAX_STALL_RESTARTS,
        unexpected_failure_retry_delays_seconds: tuple[float, ...] = (
            _DEFAULT_UNEXPECTED_FAILURE_RETRY_DELAYS_SECONDS
        ),
        candidate_provider: Callable[[], Sequence[FpsTarget]] | None = None,
        candidate_selector: FpsCandidateSelector | None = None,
        target_liveness_probe: Callable[[FpsTarget], bool] | None = None,
        gpu_usage_sampler: ProcessGpuUsageProbe | None = None,
        gpu_probe_interval_seconds: float = _DEFAULT_GPU_PROBE_INTERVAL_SECONDS,
        gpu_activity_threshold_percent: float = (_DEFAULT_GPU_ACTIVITY_THRESHOLD_PERCENT),
        gpu_safety_retry_seconds: float = _DEFAULT_GPU_SAFETY_RETRY_SECONDS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if publish_interval_seconds <= 0:
            raise ValueError("publish_interval_seconds must be positive")
        if no_frame_timeout_seconds <= 0:
            raise ValueError("no_frame_timeout_seconds must be positive")
        if frame_stall_timeout_seconds <= 0:
            raise ValueError("frame_stall_timeout_seconds must be positive")
        if target_retry_delay_seconds <= 0:
            raise ValueError("target_retry_delay_seconds must be positive")
        if rejected_rescan_delay_seconds <= 0:
            raise ValueError("rejected_rescan_delay_seconds must be positive")
        if gpu_probe_interval_seconds <= 0:
            raise ValueError("gpu_probe_interval_seconds must be positive")
        if not math.isfinite(gpu_activity_threshold_percent) or gpu_activity_threshold_percent < 0:
            raise ValueError("gpu_activity_threshold_percent must be finite and non-negative")
        if gpu_safety_retry_seconds <= 0:
            raise ValueError("gpu_safety_retry_seconds must be positive")
        if max_stall_restarts < 0:
            raise ValueError("max_stall_restarts cannot be negative")
        if len(unexpected_failure_retry_delays_seconds) != 3 or any(
            not math.isfinite(delay) or delay <= 0
            for delay in unexpected_failure_retry_delays_seconds
        ):
            raise ValueError(
                "unexpected_failure_retry_delays_seconds must contain three positive finite delays"
            )
        self._runtime_manager = runtime_manager
        self._publish_interval_seconds = publish_interval_seconds
        self._no_frame_timeout_seconds = no_frame_timeout_seconds
        self._frame_stall_timeout_seconds = frame_stall_timeout_seconds
        self._target_retry_delay_seconds = target_retry_delay_seconds
        self._rejected_rescan_delay_seconds = rejected_rescan_delay_seconds
        self._max_stall_restarts = max_stall_restarts
        self._unexpected_failure_retry_delays_seconds = unexpected_failure_retry_delays_seconds
        self._candidate_provider = candidate_provider
        self._candidate_selector = candidate_selector
        self._target_liveness_probe = target_liveness_probe
        self._gpu_usage_sampler = gpu_usage_sampler
        self._gpu_probe_interval_seconds = gpu_probe_interval_seconds
        self._gpu_activity_threshold_percent = gpu_activity_threshold_percent
        self._gpu_safety_retry_seconds = gpu_safety_retry_seconds
        self._lock = threading.Lock()
        self._target: FpsTarget | None = None
        self._generation = 0
        self._capture_thread: threading.Thread | None = None
        self._capture_stop: threading.Event | None = None
        self._process: subprocess.Popen[str] | None = None
        self._last_capture_diagnostics: PresentMonCaptureDiagnostics | None = None
        self._stream_identity: tuple[int, int | str] | None = None
        self._verified_target_identity: tuple[int, int | str] | None = None
        self._preferred_target: FpsTarget | None = None
        self._pending_foreground_hint: tuple[int, FpsTarget] | None = None
        self._foreground_hint_revision = 0
        self._provider_evidence_loaded = False
        self._provider_evidence_revision = 0
        self._stream_selector = FpsStreamSelector()
        self._overlay_visible = False
        self._discovery_active = threading.Event()
        self._discovery_active.set()
        self._started = False
        self._closed = False

    @property
    def running(self) -> bool:
        with self._lock:
            thread = self._capture_thread
            return thread is not None and thread.is_alive()

    @property
    def target(self) -> FpsTarget | None:
        with self._lock:
            return self._target

    @property
    def last_capture_diagnostics(self) -> PresentMonCaptureDiagnostics | None:
        with self._lock:
            return self._last_capture_diagnostics

    def start(self) -> None:
        if self._closed or self._started:
            return
        self._started = True
        with self._lock:
            target = self._target
        if target is not None:
            self._replace_capture(target)
        elif self._candidate_provider is not None:
            self._replace_capture(None, discover=True)
        else:
            self.metric_ready.emit(
                FpsMetricUpdate(
                    _unavailable_fps_metric((), "OPEN VIGIL OVER A GAME"),
                    None,
                )
            )

    def stop(self) -> None:
        if self._closed:
            return
        self._started = False
        self._discovery_active.set()
        self._replace_capture(None)
        gpu_usage_sampler = self._gpu_usage_sampler
        if gpu_usage_sampler is not None:
            try:
                gpu_usage_sampler.close()
            except OSError:
                _LOGGER.exception("Could not close the FPS GPU activity sampler")
        self._closed = True

    def set_target(self, target: FpsTarget | None) -> None:
        if self._closed:
            return
        with self._lock:
            thread = self._capture_thread
            current = self._target
            same_identity = (
                target is not None
                and current is not None
                and target.identity_key == current.identity_key
            )
            if same_identity and thread is not None and thread.is_alive():
                return
        self._replace_capture(target if self._started else None)
        if not self._started:
            with self._lock:
                self._target = target

    def set_known_games(self, games: tuple[GameRecord, ...]) -> None:
        """Refresh provider evidence used by subsequent FPS candidate scans."""

        selector = self._candidate_selector
        if selector is None or self._closed:
            return
        provider_evidence_changed = selector.update_known_games(games)
        with self._lock:
            self._provider_evidence_loaded = True
            pending_foreground_hint = self._pending_foreground_hint
            self._pending_foreground_hint = None
        pending_target = pending_foreground_hint[1] if pending_foreground_hint is not None else None
        pending_match = pending_target is not None and selector.match(pending_target) is not None
        pending_is_fallback = pending_target is not None and _eligible_local_fallback(
            pending_target
        )
        promoted_pending_hint = False
        with self._lock:
            if (
                pending_foreground_hint is not None
                and self._foreground_hint_revision == pending_foreground_hint[0]
                and (pending_match or pending_is_fallback)
                and pending_target is not None
                and (
                    self._target is None or not self._retains_verified_capture_locked(self._target)
                )
            ):
                self._preferred_target = pending_target
                promoted_pending_hint = True
            if provider_evidence_changed:
                self._provider_evidence_revision += 1
            target = self._target
            should_start_discovery = self._started and (
                target is None
                or (promoted_pending_hint and self._process is None)
                or (
                    provider_evidence_changed
                    and not self._retains_verified_capture_locked(target)
                    and self._process is None
                )
            )
        if promoted_pending_hint:
            _LOGGER.info("Deferred FPS foreground hint is ready for learned/provider targeting")
        elif pending_foreground_hint is not None:
            _LOGGER.debug("Discarded ineligible deferred FPS foreground hint")
        if should_start_discovery:
            self._replace_capture(
                pending_target if promoted_pending_hint else None,
                discover=True,
            )

    def request_discovery(self, preferred_target: FpsTarget | None = None) -> None:
        """Wake provider discovery and prioritize a pre-overlay foreground hint."""

        if self._closed or self._candidate_provider is None:
            return
        selector = self._candidate_selector
        preferred_is_match = (
            preferred_target is not None
            and selector is not None
            and selector.match(preferred_target) is not None
        )
        preferred_is_fallback = preferred_target is not None and _eligible_local_fallback(
            preferred_target
        )
        replace_provisional = False
        deferred_preferred = False
        with self._lock:
            thread = self._capture_thread
            if not self._started:
                return
            if thread is not None and thread.is_alive():
                target = self._target
                if target is not None and self._retains_verified_capture_locked(target):
                    return
            else:
                target = self._target
            activate_preferred = False
            if preferred_target is not None:
                self._foreground_hint_revision += 1
                self._pending_foreground_hint = None
                if (
                    selector is not None
                    and not self._provider_evidence_loaded
                    and not preferred_is_match
                ):
                    self._pending_foreground_hint = (
                        self._foreground_hint_revision,
                        preferred_target,
                    )
                    deferred_preferred = True
                elif preferred_is_match or preferred_is_fallback:
                    self._preferred_target = preferred_target
                    activate_preferred = True
            if thread is not None and thread.is_alive():
                replace_provisional = (
                    activate_preferred
                    and preferred_target is not None
                    and (
                        self._process is None
                        or (
                            preferred_is_match
                            and (
                                target is None
                                or target.identity_key != preferred_target.identity_key
                            )
                        )
                    )
                )
                if not replace_provisional:
                    self._discovery_active.set()
                    return
        if deferred_preferred:
            _LOGGER.debug("Deferred FPS foreground hint until provider evidence loads")
        elif preferred_target is not None and not (preferred_is_match or preferred_is_fallback):
            _LOGGER.debug("Ignored FPS foreground hint without an eligible executable path")
        if replace_provisional:
            _LOGGER.info(
                "FPS foreground hint woke parked or provisional discovery; restarting scan"
            )
            self._replace_capture(preferred_target, discover=True)
            return
        self._replace_capture(preferred_target if activate_preferred else None, discover=True)

    def set_overlay_visible(self, visible: bool) -> None:
        """Record display policy without disturbing the owned capture."""

        if self._closed:
            return
        with self._lock:
            self._overlay_visible = visible
            if visible and not self._verified_target_identity:
                self._discovery_active.set()

    def _replace_capture(
        self,
        target: FpsTarget | None,
        *,
        discover: bool = False,
    ) -> None:
        with self._lock:
            previous_target = self._target
            self._generation += 1
            generation = self._generation
            previous_stop = self._capture_stop
            previous_thread = self._capture_thread
            process = self._process
            self._capture_stop = None
            self._capture_thread = None
            self._process = None
            self._target = target
            self._last_capture_diagnostics = None
            self._verified_target_identity = None
            if not discover:
                self._preferred_target = None
                self._pending_foreground_hint = None
            if (
                target is None
                or previous_target is None
                or previous_target.identity_key != target.identity_key
            ):
                self._stream_identity = target.identity_key if target is not None else None
                self._stream_selector = FpsStreamSelector()
        if previous_stop is not None:
            previous_stop.set()
        if process is not None:
            _terminate_process(process)
        if (
            previous_thread is not None
            and previous_thread.is_alive()
            and previous_thread is not threading.current_thread()
        ):
            previous_thread.join(timeout=2.0)
        if target is None and (not discover or self._candidate_provider is None):
            self.metric_ready.emit(
                FpsMetricUpdate(_unavailable_fps_metric((), "OPEN VIGIL OVER A GAME"), None)
            )
            return
        if not self._started:
            return
        initial_status = (
            "FINDING GAME" if self._candidate_provider is not None else "ATTACHING TO GAME"
        )
        self.metric_ready.emit(FpsMetricUpdate(_unavailable_fps_metric((), initial_status), target))
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._supervise_capture,
            args=(target, generation, stop_event),
            name="VigilFpsBroker",
            daemon=True,
        )
        with self._lock:
            if generation != self._generation:
                return
            self._capture_stop = stop_event
            self._capture_thread = thread
        thread.start()

    def _supervise_capture(
        self,
        seed_target: FpsTarget | None,
        generation: int,
        stop_event: threading.Event,
    ) -> None:
        """Contain unexpected worker failures and retry one acquisition generation."""

        for failure_number in range(len(self._unexpected_failure_retry_delays_seconds) + 1):
            try:
                self._capture_loop(seed_target, generation, stop_event)
                return
            except Exception:
                if not self._cleanup_unexpected_capture_failure(
                    generation,
                    stop_event,
                ):
                    return
                retry_available = failure_number < len(
                    self._unexpected_failure_retry_delays_seconds
                )
                _LOGGER.exception(
                    "FPS watchdog caught an unexpected capture failure%s",
                    (
                        f"; retrying {failure_number + 1}/"
                        f"{len(self._unexpected_failure_retry_delays_seconds)}"
                        if retry_available
                        else "; retries exhausted"
                    ),
                )
                if not retry_available:
                    current = self.target or seed_target
                    if self._complete_capture(
                        generation,
                        current,
                        secondary_text="FPS WATCHDOG UNAVAILABLE",
                    ):
                        self.failure_ready.emit(
                            "The FPS watchdog stopped after repeated unexpected errors. "
                            "Close and reopen Vigil over the game to retry. If this continues, "
                            "check the Vigil log for the recorded failure."
                        )
                    return
                self._publish_status(
                    generation,
                    self.target or seed_target,
                    "FPS WATCHDOG RECOVERING",
                )
                delay = self._unexpected_failure_retry_delays_seconds[failure_number]
                if stop_event.wait(delay):
                    return

    def _cleanup_unexpected_capture_failure(
        self,
        generation: int,
        stop_event: threading.Event,
    ) -> bool:
        """Detach and terminate only the process owned by the failing generation."""

        with self._lock:
            if generation != self._generation or stop_event.is_set():
                return False
            process = self._process
            self._process = None
        if process is not None:
            _terminate_process(process)
        return True

    def _capture_loop(
        self,
        seed_target: FpsTarget | None,
        generation: int,
        stop_event: threading.Event,
    ) -> None:
        try:
            runtime = self._runtime_manager.ensure()
        except PresentMonRuntimeError:
            _LOGGER.exception("PresentMon FPS runtime is unavailable")
            self._complete_capture(
                generation,
                seed_target,
                secondary_text="FPS COLLECTOR UNAVAILABLE",
            )
            return

        # Provider and durable learned matches lead discovery. An otherwise unknown
        # foreground executable may enter only through targeted sustained-GPU activity or
        # its bounded safety probe. Every candidate remains provisional until the current
        # process produces usable PresentMon rows; only then does one collector own the FPS
        # session through later no-frame/stale periods until target or collector exit.
        rejected_until: dict[tuple[int, int | str], float] = {}
        rejection_attempts: dict[tuple[int, int | str], int] = {}
        gpu_high_samples: dict[tuple[int, int | str], int] = {}
        fallback_wait_started: dict[tuple[int, int | str], float] = {}
        next_gpu_probe_at = 0.0
        previous_visible_identities: set[tuple[int, int | str]] = set()
        completed_candidate_scan = False
        first_cycle = True
        stall_restarts: dict[tuple[int, int | str], int] = {}
        with self._lock:
            observed_provider_evidence_revision = self._provider_evidence_revision
        while not stop_event.is_set():
            if not self._wait_for_discovery(stop_event):
                return
            discovered = self._candidate_cycle(seed_target if first_cycle else None)
            first_cycle = False
            current_visible_identities = {candidate.identity_key for candidate in discovered}

            with self._lock:
                provider_evidence_revision = self._provider_evidence_revision
            if provider_evidence_revision != observed_provider_evidence_revision:
                rejected_until.clear()
                rejection_attempts.clear()
                stall_restarts.clear()
                gpu_high_samples.clear()
                fallback_wait_started.clear()
                next_gpu_probe_at = 0.0
                previous_visible_identities.clear()
                completed_candidate_scan = False
                observed_provider_evidence_revision = provider_evidence_revision
                _LOGGER.info("FPS provider evidence changed; clearing candidate backoff")

            if completed_candidate_scan:
                reappeared = current_visible_identities - previous_visible_identities
                for identity in reappeared:
                    was_backed_off = (
                        identity in rejected_until
                        or identity in rejection_attempts
                        or identity in stall_restarts
                    )
                    rejected_until.pop(identity, None)
                    rejection_attempts.pop(identity, None)
                    stall_restarts.pop(identity, None)
                    gpu_high_samples.pop(identity, None)
                    fallback_wait_started.pop(identity, None)
                    if was_backed_off:
                        _LOGGER.info(
                            "FPS candidate became visible again; clearing no-frame rejection: %r",
                            identity,
                        )
            now = time.monotonic()
            expired = tuple(
                identity for identity, retry_at in rejected_until.items() if retry_at <= now
            )
            for identity in expired:
                del rejected_until[identity]
                _LOGGER.info("FPS candidate cooldown expired; retrying: %r", identity)
            completed_candidate_scan = True
            previous_visible_identities = current_visible_identities

            selector = self._candidate_selector
            gpu_parked = tuple(
                candidate
                for candidate in discovered
                if candidate.identity_key in rejected_until
                or (selector is not None and selector.match(candidate) is None)
            )
            gpu_wakes: frozenset[tuple[int, int | str]] = frozenset()
            if gpu_parked and now >= next_gpu_probe_at:
                gpu_wakes = self._sample_gpu_wakes(gpu_parked, gpu_high_samples)
                next_gpu_probe_at = now + self._gpu_probe_interval_seconds
                for identity in gpu_wakes:
                    if identity in rejected_until:
                        rejected_until.pop(identity, None)
                        _LOGGER.info(
                            "Sustained GPU activity woke parked FPS candidate early: %r",
                            identity,
                        )

            ready_candidates: list[FpsTarget] = []
            for candidate in discovered:
                identity = candidate.identity_key
                if identity in rejected_until:
                    continue
                is_local_fallback = selector is not None and selector.match(candidate) is None
                if is_local_fallback:
                    wait_started = fallback_wait_started.setdefault(identity, now)
                    safety_ready = now - wait_started >= self._gpu_safety_retry_seconds
                    if identity not in gpu_wakes and not safety_ready:
                        continue
                    if safety_ready and identity not in gpu_wakes:
                        _LOGGER.info(
                            "FPS foreground candidate reached the safety probe interval: %r",
                            identity,
                        )
                ready_candidates.append(candidate)
            candidates = tuple(ready_candidates)
            if not candidates:
                if self._candidate_provider is None:
                    self._complete_capture(
                        generation,
                        seed_target,
                        secondary_text="FPS TARGET NOT FOUND",
                    )
                    return
                if discovered:
                    # PresentMon is stopped while the target is parked. Low-duty GPU
                    # sampling can wake it early; the bounded timer also supports
                    # CPU-bound games and systems without process GPU counters.
                    self._publish_status(
                        generation,
                        self.target,
                        "GAME FOUND - WAITING FOR ACTIVITY",
                    )
                    retry_delays = [self._target_retry_delay_seconds]
                    retry_delays.extend(
                        max(0.0, rejected_until[candidate.identity_key] - now)
                        for candidate in discovered
                        if candidate.identity_key in rejected_until
                    )
                    retry_delays.extend(
                        max(
                            0.0,
                            fallback_wait_started[candidate.identity_key]
                            + self._gpu_safety_retry_seconds
                            - now,
                        )
                        for candidate in discovered
                        if candidate.identity_key in fallback_wait_started
                    )
                    if self._gpu_usage_sampler is not None:
                        retry_delays.append(max(0.0, next_gpu_probe_at - now))
                    retry_delay = max(min(retry_delays), 0.01)
                    if stop_event.wait(retry_delay):
                        return
                else:
                    self._publish_status(
                        generation,
                        self.target,
                        "RETRYING GAME DETECTION",
                    )
                    has_parked_state = bool(
                        rejection_attempts or stall_restarts or fallback_wait_started
                    )
                    if not self._overlay_is_visible() and not has_parked_state:
                        self._discovery_active.clear()
                    else:
                        if stop_event.wait(self._target_retry_delay_seconds):
                            return
                continue

            retry_stalled_candidate: FpsTarget | None = None
            for candidate in candidates:
                if not self._wait_for_discovery(stop_event):
                    return
                if not self._activate_candidate(generation, candidate):
                    return
                command = build_presentmon_command(runtime.executable, candidate)
                try:
                    process = _start_hidden_process(command)
                except OSError:
                    _LOGGER.exception(
                        "PresentMon FPS collector could not be started for %s (pid=%d)",
                        candidate.executable_name,
                        candidate.process_id,
                    )
                    self._complete_capture(
                        generation,
                        candidate,
                        secondary_text="FPS COLLECTOR FAILED",
                    )
                    return
                with self._lock:
                    if generation != self._generation:
                        _terminate_process(process)
                        return
                    self._process = process
                try:
                    outcome = self._consume_process(process, candidate, stop_event)
                finally:
                    _terminate_process(process)
                    with self._lock:
                        if generation == self._generation and self._process is process:
                            self._process = None
                    self._discovery_active.set()
                if outcome is _CaptureOutcome.CANCELLED:
                    return
                if outcome is _CaptureOutcome.PERMISSION_REQUIRED:
                    diagnostics = self.last_capture_diagnostics
                    detail = (
                        "PresentMon could not start its Windows trace session. "
                        "Run Vigil Overlay as administrator and try again."
                    )
                    if diagnostics is not None:
                        _LOGGER.error(
                            "PresentMon requires elevated FPS access for %s (pid=%d): %s",
                            candidate.executable_name,
                            candidate.process_id,
                            diagnostics.summary,
                        )
                    completed = self._complete_capture(
                        generation,
                        candidate,
                        secondary_text="FPS PERMISSION REQUIRED",
                    )
                    if completed:
                        self.failure_ready.emit(detail)
                    return
                if outcome is _CaptureOutcome.COLLECTOR_FAILED:
                    diagnostics = self.last_capture_diagnostics
                    if diagnostics is not None:
                        _LOGGER.error(
                            "PresentMon collector failed for %s (pid=%d): %s",
                            candidate.executable_name,
                            candidate.process_id,
                            diagnostics.summary,
                        )
                    self._complete_capture(
                        generation,
                        candidate,
                        secondary_text="FPS COLLECTOR FAILED",
                    )
                    return
                if outcome is _CaptureOutcome.COMPLETED_WITH_FRAMES:
                    self._clear_finished_target(generation, candidate)
                    _LOGGER.info(
                        "FPS target stopped after producing frames; rediscovering: %s (pid=%d)",
                        candidate.executable_name,
                        candidate.process_id,
                    )
                    break
                if outcome is _CaptureOutcome.STALLED:
                    attempts = stall_restarts.get(candidate.identity_key, 0) + 1
                    stall_restarts[candidate.identity_key] = attempts
                    if attempts <= self._max_stall_restarts:
                        retry_stalled_candidate = candidate
                        self._publish_status(
                            generation,
                            candidate,
                            "FPS STREAM STALLED - RESTARTING",
                        )
                        _LOGGER.warning(
                            "FPS stream stalled; restarting collector %d/%d for %s (pid=%d)",
                            attempts,
                            self._max_stall_restarts,
                            candidate.executable_name,
                            candidate.process_id,
                        )
                        break
                    _LOGGER.error(
                        "FPS stream remained stalled after %d collector restarts for %s (pid=%d)",
                        self._max_stall_restarts,
                        candidate.executable_name,
                        candidate.process_id,
                    )
                    rejection_attempt, retry_delay = self._reject_candidate(
                        candidate,
                        rejection_attempts,
                        rejected_until,
                    )
                    _LOGGER.info(
                        "FPS unstable candidate backoff attempt %d is %.1f seconds: %r",
                        rejection_attempt,
                        retry_delay,
                        candidate.identity_key,
                    )
                    self._publish_status(
                        generation,
                        candidate,
                        "FPS STREAM UNSTABLE - TRYING NEXT GAME",
                    )
                    continue

                rejection_attempt, retry_delay = self._reject_candidate(
                    candidate,
                    rejection_attempts,
                    rejected_until,
                )
                diagnostics = self.last_capture_diagnostics
                status = (
                    diagnostics.no_frame_status
                    if diagnostics is not None
                    else "NO FPS DATA RECEIVED"
                )
                _LOGGER.info(
                    "FPS candidate parked with PresentMon stopped before retry attempt %d "
                    "for %.1f seconds: "
                    "%s (pid=%d, identity=%r, reason=%s, diagnostics=%s)",
                    rejection_attempt,
                    retry_delay,
                    candidate.executable_name,
                    candidate.process_id,
                    candidate.identity_key,
                    status,
                    diagnostics.summary if diagnostics is not None else "<unavailable>",
                )
                self._publish_status(
                    generation,
                    candidate,
                    "GAME FOUND - WAITING FOR ACTIVITY",
                )
            else:
                self._publish_status(
                    generation,
                    self.target,
                    "GAME FOUND - WAITING FOR ACTIVITY",
                )

            if retry_stalled_candidate is not None:
                seed_target = retry_stalled_candidate
                first_cycle = True
                if stop_event.wait(self._target_retry_delay_seconds):
                    return
                continue

            if self._candidate_provider is None:
                current = self.target or seed_target
                self._complete_capture(
                    generation,
                    current,
                    secondary_text="FPS TARGET NOT FOUND",
                )
                return
            if stop_event.wait(self._target_retry_delay_seconds):
                return

    def _sample_gpu_wakes(
        self,
        candidates: tuple[FpsTarget, ...],
        high_samples: dict[tuple[int, int | str], int],
    ) -> frozenset[tuple[int, int | str]]:
        sampler = self._gpu_usage_sampler
        if sampler is None or not candidates:
            return frozenset()
        try:
            usage_by_pid = sampler.sample(
                frozenset(candidate.process_id for candidate in candidates)
            )
        except (OSError, RuntimeError):
            _LOGGER.exception(
                "Process GPU activity is unavailable; parked FPS targets will use "
                "their safety retry"
            )
            with suppress(OSError, RuntimeError):
                sampler.close()
            self._gpu_usage_sampler = None
            for candidate in candidates:
                high_samples.pop(candidate.identity_key, None)
            return frozenset()

        woke: set[tuple[int, int | str]] = set()
        for candidate in candidates:
            identity = candidate.identity_key
            usage = usage_by_pid.get(candidate.process_id)
            if usage is None or usage < self._gpu_activity_threshold_percent:
                high_samples[identity] = 0
                continue
            count = high_samples.get(identity, 0) + 1
            high_samples[identity] = count
            if count >= _GPU_ACTIVITY_SAMPLE_COUNT:
                woke.add(identity)
                high_samples[identity] = 0
        return frozenset(woke)

    def _reject_candidate(
        self,
        candidate: FpsTarget,
        rejection_attempts: dict[tuple[int, int | str], int],
        rejected_until: dict[tuple[int, int | str], float],
    ) -> tuple[int, float]:
        identity = candidate.identity_key
        attempt = rejection_attempts.get(identity, 0) + 1
        rejection_attempts[identity] = attempt
        multiplier = _REJECTED_RESCAN_DELAY_MULTIPLIERS[
            min(attempt - 1, len(_REJECTED_RESCAN_DELAY_MULTIPLIERS) - 1)
        ]
        retry_delay = self._rejected_rescan_delay_seconds * multiplier
        rejected_until[identity] = time.monotonic() + retry_delay
        return attempt, retry_delay

    def _wait_for_discovery(self, stop_event: threading.Event) -> bool:
        while not stop_event.is_set():
            if self._discovery_active.wait(timeout=0.1):
                return True
        return False

    def _selector_for_target(self, target: FpsTarget) -> FpsStreamSelector:
        with self._lock:
            if self._stream_identity != target.identity_key:
                self._stream_identity = target.identity_key
                self._stream_selector = FpsStreamSelector()
            return self._stream_selector

    def _clear_finished_target(self, generation: int, target: FpsTarget) -> None:
        """Reset cached metrics when the selected process ends without stopping discovery."""

        with self._lock:
            if generation != self._generation:
                return
            if self._target is None or self._target.identity_key != target.identity_key:
                return
            self._target = None
            self._stream_identity = None
            self._verified_target_identity = None
            self._preferred_target = None
            self._pending_foreground_hint = None
            self._stream_selector = FpsStreamSelector()
            self._discovery_active.set()
        self.metric_ready.emit(
            FpsMetricUpdate(
                _unavailable_fps_metric((), "GAME CLOSED - FINDING GAME"),
                None,
            )
        )

    def _candidate_cycle(self, seed_target: FpsTarget | None) -> tuple[FpsTarget, ...]:
        discovered: Sequence[FpsTarget] = ()
        with self._lock:
            preferred_target = self._preferred_target
        selector = self._candidate_selector
        should_scan = self._candidate_provider is not None and (
            selector is None
            or selector.has_targeting_evidence
            or seed_target is not None
            or preferred_target is not None
        )
        if should_scan and self._candidate_provider is not None:
            try:
                discovered = self._candidate_provider()
            except Exception:
                _LOGGER.exception("FPS candidate discovery failed; using foreground seed target")
        ordered: list[FpsTarget] = []
        seen_pids: set[int] = set()
        for candidate in (
            *discovered,
            *((seed_target,) if seed_target is not None else ()),
            *((preferred_target,) if preferred_target is not None else ()),
        ):
            if candidate.process_id in seen_pids:
                continue
            seen_pids.add(candidate.process_id)
            ordered.append(candidate)
            if selector is None and len(ordered) >= _MAX_DISCOVERED_TARGETS:
                break
        if selector is not None:
            selected = list(
                selector.select(
                    tuple(ordered),
                    preferred_target=preferred_target,
                )
            )
            selected_identities = {candidate.identity_key for candidate in selected}
            discovered_identities = {candidate.identity_key for candidate in discovered}
            for fallback in (seed_target, preferred_target):
                if (
                    fallback is None
                    or fallback.identity_key in selected_identities
                    or selector.match(fallback) is not None
                    or not _eligible_local_fallback(fallback)
                    or (
                        fallback is not seed_target
                        and fallback.identity_key not in discovered_identities
                    )
                ):
                    continue
                selected.append(fallback)
                selected_identities.add(fallback.identity_key)
                break
            return tuple(selected[:_MAX_DISCOVERED_TARGETS])
        return tuple(ordered)

    def _activate_candidate(self, generation: int, target: FpsTarget) -> bool:
        selector = self._candidate_selector
        provider_match = selector.match(target) if selector is not None else None
        with self._lock:
            if generation != self._generation:
                return False
            self._target = target
            self._last_capture_diagnostics = None
            self._verified_target_identity = None
            self._pending_foreground_hint = None
            self._discovery_active.clear()
        self.metric_ready.emit(
            FpsMetricUpdate(_unavailable_fps_metric((), "ATTACHING TO GAME"), target)
        )
        if provider_match is not None:
            _LOGGER.info(
                "FPS provider candidate attached provisionally: %s (pid=%d, match=%s)",
                target.executable_name,
                target.process_id,
                provider_match.reason,
            )
        else:
            _LOGGER.info(
                "FPS candidate selected: %s (pid=%d)",
                target.executable_name,
                target.process_id,
            )
        return True

    def _publish_status(
        self,
        generation: int,
        target: FpsTarget | None,
        secondary_text: str,
    ) -> None:
        with self._lock:
            if generation != self._generation:
                return
        self.metric_ready.emit(FpsMetricUpdate(_unavailable_fps_metric((), secondary_text), target))

    def _complete_capture(
        self,
        generation: int,
        target: FpsTarget | None,
        *,
        secondary_text: str = "",
    ) -> bool:
        """Clear a session only when the currently owned capture finishes on its own."""

        with self._lock:
            if generation != self._generation:
                return False
            self._generation += 1
            self._target = None
            self._capture_stop = None
            self._capture_thread = None
            self._process = None
            self._stream_identity = None
            self._verified_target_identity = None
            self._preferred_target = None
            self._pending_foreground_hint = None
            self._stream_selector = FpsStreamSelector()
        self.metric_ready.emit(
            FpsMetricUpdate(
                _unavailable_fps_metric((), secondary_text),
                None,
            )
        )
        if target is None:
            _LOGGER.info("FPS discovery ended before a game target was selected")
        else:
            _LOGGER.info(
                "FPS session ended after capture stopped: %s (pid=%d)",
                target.executable_name,
                target.process_id,
            )
        return True

    def _consume_process(
        self,
        process: subprocess.Popen[str],
        target: FpsTarget,
        stop_event: threading.Event,
    ) -> _CaptureOutcome:
        stdout = process.stdout
        if stdout is None:
            return _CaptureOutcome.COLLECTOR_FAILED
        lines: queue.Queue[str | None] = queue.Queue(maxsize=4096)
        reader = threading.Thread(
            target=_read_lines,
            args=(stdout, lines, stop_event),
            name="VigilFpsBrokerReader",
            daemon=True,
        )
        reader.start()
        parser = PresentMonCsvParser(target_pid=target.process_id)
        selector = self._selector_for_target(target)
        started_at = time.monotonic()
        next_liveness_probe = started_at
        no_frame_probe_started_at: float | None = None
        last_frame_at: float | None = None
        resume_probe_started_at: float | None = None
        next_publish = started_at + self._publish_interval_seconds
        saw_usable_frame = False
        accepted_frame_count = 0
        retain_verified_capture = self._retains_verified_capture(target)
        outcome = _CaptureOutcome.NO_FRAMES
        stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        stderr_reader: threading.Thread | None = None
        stderr = getattr(process, "stderr", None)
        if stderr is not None:
            stderr_reader = threading.Thread(
                target=_read_stderr_lines,
                args=(stderr, stderr_tail, stop_event),
                name="VigilFpsBrokerStderr",
                daemon=True,
            )
            stderr_reader.start()
        while True:
            if stop_event.is_set():
                outcome = _CaptureOutcome.CANCELLED
                break
            timeout = max(min(next_publish - time.monotonic(), 0.1), 0.01)
            try:
                line = lines.get(timeout=timeout)
            except queue.Empty:
                line = ""
            if line is None:
                exit_code = process.poll()
                if saw_usable_frame:
                    outcome = _CaptureOutcome.COMPLETED_WITH_FRAMES
                elif _presentmon_permission_required(stderr_tail):
                    outcome = _CaptureOutcome.PERMISSION_REQUIRED
                elif exit_code not in {None, 0}:
                    outcome = _CaptureOutcome.COLLECTOR_FAILED
                else:
                    outcome = _CaptureOutcome.NO_FRAMES
                break
            if line:
                frame = parser.parse_line(line)
                if frame is not None:
                    saw_usable_frame = True
                    accepted_frame_count += 1
                    last_frame_at = time.monotonic()
                    resume_probe_started_at = None
                    selector.ingest(frame, observed_at=last_frame_at)
                    if (
                        not retain_verified_capture
                        and accepted_frame_count >= _VERIFICATION_FRAME_COUNT
                    ):
                        retain_verified_capture = self._verify_candidate(target)
            now = time.monotonic()
            if self._target_liveness_probe is not None and now >= next_liveness_probe:
                try:
                    target_alive = self._target_liveness_probe(target)
                except OSError:
                    target_alive = True
                    _LOGGER.exception(
                        "FPS target liveness check failed open for %s (pid=%d)",
                        target.executable_name,
                        target.process_id,
                    )
                next_liveness_probe = now + _TARGET_LIVENESS_POLL_SECONDS
                if not target_alive:
                    _LOGGER.info(
                        "FPS watchdog observed target exit: %s (pid=%d)",
                        target.executable_name,
                        target.process_id,
                    )
                    outcome = (
                        _CaptureOutcome.COMPLETED_WITH_FRAMES
                        if saw_usable_frame
                        else _CaptureOutcome.NO_FRAMES
                    )
                    break
            if not saw_usable_frame and _presentmon_permission_required(stderr_tail):
                outcome = _CaptureOutcome.PERMISSION_REQUIRED
                break
            if not saw_usable_frame:
                if retain_verified_capture:
                    no_frame_probe_started_at = None
                elif no_frame_probe_started_at is None:
                    no_frame_probe_started_at = now
                elif now - no_frame_probe_started_at >= self._no_frame_timeout_seconds:
                    outcome = _CaptureOutcome.NO_FRAMES
                    break
            if (
                saw_usable_frame
                and last_frame_at is not None
                and now - last_frame_at >= self._frame_stall_timeout_seconds
            ):
                if retain_verified_capture:
                    # Only a frame-verified game owns a persistent collector across
                    # stale periods. Provisional targets stop and park after timeout.
                    resume_probe_started_at = None
                elif resume_probe_started_at is None:
                    # Give a game one full stall interval to resume after Vigil hides.
                    resume_probe_started_at = now
                elif now - resume_probe_started_at >= self._frame_stall_timeout_seconds:
                    outcome = _CaptureOutcome.STALLED
                    break
            else:
                resume_probe_started_at = None
            if now >= next_publish:
                metric = selector.metric(now=now)
                if metric.numeric_value is None:
                    metric = _unavailable_fps_metric(
                        metric.history,
                        (
                            "WAITING FOR GAME FRAMES"
                            if retain_verified_capture
                            else "PROBING GAME FRAMES"
                        ),
                    )
                self.metric_ready.emit(FpsMetricUpdate(metric, target))
                next_publish = now + self._publish_interval_seconds
            if process.poll() is not None and lines.empty():
                exit_code = process.poll()
                if saw_usable_frame:
                    outcome = _CaptureOutcome.COMPLETED_WITH_FRAMES
                elif _presentmon_permission_required(stderr_tail):
                    outcome = _CaptureOutcome.PERMISSION_REQUIRED
                elif exit_code not in {None, 0}:
                    outcome = _CaptureOutcome.COLLECTOR_FAILED
                else:
                    outcome = _CaptureOutcome.NO_FRAMES
                break
        exit_code_before_stop = process.poll()
        if outcome is _CaptureOutcome.CANCELLED:
            stop_event.set()
        elif outcome in {
            _CaptureOutcome.NO_FRAMES,
            _CaptureOutcome.PERMISSION_REQUIRED,
            _CaptureOutcome.STALLED,
        }:
            _terminate_process(process)
        reader.join(timeout=1.0)
        if stderr_reader is not None:
            stderr_reader.join(timeout=1.0)
        diagnostics = parser.diagnostics(
            target,
            exit_code=exit_code_before_stop,
            stderr_tail=tuple(stderr_tail),
        )
        if (
            outcome in {_CaptureOutcome.NO_FRAMES, _CaptureOutcome.COLLECTOR_FAILED}
            and diagnostics.permission_required
        ):
            outcome = _CaptureOutcome.PERMISSION_REQUIRED
        with self._lock:
            if self._target is not None and self._target.identity_key == target.identity_key:
                self._last_capture_diagnostics = diagnostics
        return outcome

    def _overlay_is_visible(self) -> bool:
        with self._lock:
            return self._overlay_visible

    def _verify_candidate(self, target: FpsTarget) -> bool:
        selector = self._candidate_selector
        if selector is None:
            return False
        verified_match = selector.record_verified(target)
        if verified_match is None:
            return False
        with self._lock:
            if self._target is None or self._target.identity_key != target.identity_key:
                return False
            self._verified_target_identity = target.identity_key
            self._preferred_target = None
            self._pending_foreground_hint = None
            self._discovery_active.clear()
        identity = verified_match.identity
        if identity is None:
            _LOGGER.info(
                "FPS local target verified by frames; ownership retained until exit: %s (pid=%d)",
                target.executable_name,
                target.process_id,
            )
        else:
            _LOGGER.info(
                "FPS provider target verified by frames; ownership retained until exit: "
                "%s (pid=%d, provider=%s, game=%s)",
                target.executable_name,
                target.process_id,
                identity.provider_id,
                identity.provider_game_id,
            )
        return True

    def _retains_verified_capture_locked(self, target: FpsTarget) -> bool:
        return self._verified_target_identity == target.identity_key

    def _retains_verified_capture(self, target: FpsTarget) -> bool:
        with self._lock:
            return self._retains_verified_capture_locked(target)


class UnavailableFpsService(QObject):
    """No-op FPS service for unsupported platforms."""

    metric_ready = Signal(object)
    failure_ready = Signal(str)

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def set_target(self, target: FpsTarget | None) -> None:
        del target

    def set_known_games(self, games: tuple[GameRecord, ...]) -> None:
        del games

    def request_discovery(self, preferred_target: FpsTarget | None = None) -> None:
        del preferred_target
        return

    def set_overlay_visible(self, visible: bool) -> None:
        del visible


def create_platform_fps_service(
    paths: ApplicationPaths | None = None,
) -> PresentMonFpsService | UnavailableFpsService:
    """Create the Windows PresentMon service or an unsupported-platform fallback."""

    if sys.platform != "win32":
        return UnavailableFpsService()
    resolved_paths = paths or ApplicationPaths.discover()
    from vigil_overlay.services.windows_process import (
        capture_foreground_fps_targets,
        is_fps_target_alive,
    )
    from vigil_overlay.services.windows_telemetry import ProcessGpuUsageSampler

    gpu_usage_sampler: ProcessGpuUsageSampler | None
    try:
        gpu_usage_sampler = ProcessGpuUsageSampler()
    except OSError:
        gpu_usage_sampler = None
        _LOGGER.exception(
            "Process GPU activity is unavailable; FPS fallback will use safety retries"
        )

    return PresentMonFpsService(
        PresentMonRuntimeManager(resolved_paths),
        candidate_provider=lambda: capture_foreground_fps_targets(
            max_candidates=_RAW_PROVIDER_CANDIDATE_LIMIT
        ),
        candidate_selector=FpsCandidateSelector(
            learned_cache=LearnedFpsGameCache(
                resolved_paths.user_data_root / "fps" / "learned_games.json"
            )
        ),
        target_liveness_probe=is_fps_target_alive,
        gpu_usage_sampler=gpu_usage_sampler,
    )


def build_presentmon_command(executable: Path, target: FpsTarget) -> list[str]:
    """Build the bounded no-shell PresentMon console invocation."""

    return [
        str(executable),
        "--process_id",
        str(target.process_id),
        "--output_stdout",
        "--no_console_stats",
        "--v2_metrics",
        "--exclude_dropped",
        "--session_name",
        f"VigilOverlayFPS-{target.process_id}",
        "--stop_existing_session",
        "--terminate_on_proc_exit",
    ]


def _eligible_local_fallback(target: FpsTarget) -> bool:
    executable_path = target.executable_path
    if executable_path is None:
        return False
    parsed = PureWindowsPath(executable_path.strip())
    if not parsed.is_absolute() or parsed.suffix.casefold() != ".exe":
        return False
    from vigil_overlay.services.windows_process import is_excluded_process_name

    return not is_excluded_process_name(target.executable_name)


def _start_hidden_process(command: Sequence[str]) -> subprocess.Popen[str]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        shell=False,
        creationflags=creationflags,
    )


def _read_lines(
    stdout: TextIO, output: queue.Queue[str | None], stop_event: threading.Event
) -> None:
    try:
        for line in stdout:
            if stop_event.is_set():
                break
            try:
                output.put(line, timeout=0.1)
            except queue.Full:
                _LOGGER.warning("FPS broker line queue overflow; dropping one PresentMon row")
    finally:
        with suppress(queue.Full):
            output.put_nowait(None)


def _read_stderr_lines(
    stderr: TextIO,
    output: deque[str],
    stop_event: threading.Event,
) -> None:
    for line in stderr:
        if stop_event.is_set():
            break
        stripped = line.strip()
        if stripped:
            output.append(stripped[:_STDERR_LINE_LIMIT])


def _presentmon_permission_required(stderr_lines: Sequence[str]) -> bool:
    """Recognize PresentMon's system-wide ETW privilege failure."""

    folded = "\n".join(stderr_lines).casefold()
    return bool(
        "performance log users" in folded
        or "requires elevated privilege" in folded
        or "failed to start trace session: access denied" in folded
        or "failed to start trace session: access is denied" in folded
        or ("trace session" in folded and "access denied" in folded)
    )


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            return


def _fps_scale_max(current: float, history: tuple[float | None, ...]) -> float:
    maximum = max((value for value in history if value is not None), default=current)
    maximum = max(maximum, current, 60.0)
    return min(max(math.ceil(maximum / 30.0) * 30.0, 60.0), 1000.0)


def _unavailable_fps_metric(
    history: tuple[float | None, ...],
    secondary_text: str = "",
) -> TelemetryMetricSnapshot:
    return TelemetryMetricSnapshot(
        metric=PerformanceMetric.FPS,
        display_value="--",
        secondary_text=secondary_text,
        scale_min=0.0,
        scale_max=_fps_scale_max(60.0, history),
        history=history,
    )
