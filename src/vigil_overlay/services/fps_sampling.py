"""PresentMon CSV parsing and per-stream FPS sampling."""

from __future__ import annotations

import csv
import math
import time
from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from vigil_overlay.services.fps import FpsTarget, PresentMonFrame
from vigil_overlay.services.telemetry import PerformanceMetric, TelemetryMetricSnapshot

_CURRENT_WINDOW_MS: Final[float] = 500.0
_AVERAGE_WINDOW_MS: Final[float] = 60_000.0
_STALE_AFTER_SECONDS: Final[float] = 1.5
_FPS_HISTORY_LIMIT: Final[int] = 240


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
        return presentmon_permission_required(self.stderr_tail)

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
    """Calculate displayed FPS from frame display durations with bounded history."""

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
            return unavailable_fps_metric(self.history)

        current = self._calculate_window(_CURRENT_WINDOW_MS)
        average = self._calculate_session_average()
        if current is None:
            if append_history:
                self._history.append(None)
            return unavailable_fps_metric(self.history)
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
    """Keep independent swap-chain histories and select the dominant stream."""

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
                return unavailable_fps_metric(())
            selected = self._streams.get(self._selected_stream)
            if selected is None:
                return unavailable_fps_metric(())
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


def presentmon_permission_required(stderr_lines: Sequence[str]) -> bool:
    """Recognize PresentMon's system-wide ETW privilege failure."""

    folded = "\n".join(stderr_lines).casefold()
    return bool(
        "performance log users" in folded
        or "requires elevated privilege" in folded
        or "failed to start trace session: access denied" in folded
        or "failed to start trace session: access is denied" in folded
        or ("trace session" in folded and "access denied" in folded)
    )


def unavailable_fps_metric(
    history: tuple[float | None, ...],
    secondary_text: str = "",
) -> TelemetryMetricSnapshot:
    """Build the shared unavailable FPS metric without capture-lifecycle policy."""

    return TelemetryMetricSnapshot(
        metric=PerformanceMetric.FPS,
        display_value="--",
        secondary_text=secondary_text,
        scale_min=0.0,
        scale_max=_fps_scale_max(60.0, history),
        history=history,
    )


def _fps_scale_max(current: float, history: tuple[float | None, ...]) -> float:
    maximum = max((value for value in history if value is not None), default=current)
    maximum = max(maximum, current, 60.0)
    return min(max(math.ceil(maximum / 30.0) * 30.0, 60.0), 1000.0)
