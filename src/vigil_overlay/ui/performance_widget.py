"""Xbox Compact Mode-inspired Performance widget view."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from vigil_overlay.services.telemetry import (
    PerformanceMetric,
    TelemetryMetricSnapshot,
    TelemetrySnapshot,
)
from vigil_overlay.widgets.registry import WidgetDefinition


class TelemetryHistoryGraph(QWidget):
    """Small host-rendered 60-second line graph for one selected metric."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("telemetryHistoryGraph")
        self.setMinimumHeight(105)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._metric = TelemetrySnapshot.unavailable().metric(PerformanceMetric.CPU)

    def set_metric(self, metric: TelemetryMetricSnapshot) -> None:
        self._metric = metric
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(330, 120)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        area = QRectF(self.rect()).adjusted(2.0, 2.0, -2.0, -2.0)
        baseline = QColor(self.palette().color(self.foregroundRole()))
        baseline.setAlpha(70)
        painter.setPen(QPen(baseline, 1.0))
        painter.drawLine(area.bottomLeft(), area.bottomRight())

        samples = self._metric.history
        usable = [
            (index, value) for index, value in enumerate(samples) if value is not None
        ]
        if len(usable) < 2:
            painter.end()
            return

        value_range = self._metric.scale_max - self._metric.scale_min
        path = QPainterPath()
        started = False
        denominator = max(len(samples) - 1, 1)
        for index, value in enumerate(samples):
            if value is None:
                started = False
                continue
            x = area.left() + area.width() * index / denominator
            normalized = (value - self._metric.scale_min) / value_range
            y = area.bottom() - area.height() * min(max(normalized, 0.0), 1.0)
            point = QPointF(x, y)
            if not started:
                path.moveTo(point)
                started = True
            else:
                path.lineTo(point)

        accent = QColor(self.palette().color(QPalette.ColorRole.Highlight))
        painter.setPen(QPen(accent, 2.0))
        painter.drawPath(path)
        painter.end()


class PerformanceWidgetView(QWidget):
    """Metric selector and selected-metric detail panel."""

    def __init__(
        self,
        definition: WidgetDefinition,
        snapshot: TelemetrySnapshot,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("performanceWidgetView")
        self.setProperty("compactPage", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._definition = definition
        self._snapshot = snapshot
        self._selected_index = 0
        self._metric_buttons: list[QPushButton] = []
        self._metric_by_item = {
            "cpu": PerformanceMetric.CPU,
            "gpu": PerformanceMetric.GPU,
            "vram": PerformanceMetric.VRAM,
            "ram": PerformanceMetric.RAM,
            "fps": PerformanceMetric.FPS,
        }
        self._build_ui()
        self.apply_snapshot(snapshot)
        self.set_selected_metric(0)

    @property
    def metric_buttons(self) -> tuple[QPushButton, ...]:
        return tuple(self._metric_buttons)

    @property
    def selected_metric(self) -> PerformanceMetric:
        item = self._definition.items[self._selected_index]
        return self._metric_by_item[item.item_id]

    @property
    def value_text(self) -> str:
        return self._large_value.text()

    @property
    def secondary_text(self) -> str:
        return self._secondary_value.text()

    def apply_snapshot(self, snapshot: TelemetrySnapshot) -> None:
        self._snapshot = snapshot
        for index, item in enumerate(self._definition.items):
            metric = self._metric_by_item[item.item_id]
            reading = snapshot.metric(metric)
            self._metric_buttons[index].setText(
                f"{metric.value.upper():<5}  {reading.display_value}"
            )
        self._refresh_detail()

    def set_selected_metric(self, index: int) -> None:
        if not self._definition.items:
            return
        self._selected_index = min(max(index, 0), len(self._definition.items) - 1)
        self._refresh_detail()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        title = QLabel("Performance", self)
        title.setObjectName("performanceTitle")
        root.addWidget(title)

        body = QHBoxLayout()
        body.setSpacing(18)

        selector = QFrame(self)
        selector.setObjectName("performanceMetricSelector")
        selector.setFixedWidth(132)
        selector_layout = QVBoxLayout(selector)
        selector_layout.setContentsMargins(0, 0, 0, 0)
        selector_layout.setSpacing(1)

        for item in self._definition.items:
            button = QPushButton(selector)
            button.setObjectName("performanceMetricButton")
            button.setProperty("itemId", item.item_id)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setAccessibleName(item.label)
            button.setToolTip(item.description)
            button.setEnabled(item.enabled)
            self._metric_buttons.append(button)
            selector_layout.addWidget(button)
        selector_layout.addStretch(1)
        body.addWidget(selector)

        detail = QFrame(self)
        detail.setObjectName("performanceMetricDetail")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(3)

        self._large_value = QLabel("--", detail)
        self._large_value.setObjectName("performanceLargeValue")
        self._secondary_value = QLabel("", detail)
        self._secondary_value.setObjectName("performanceSecondaryValue")
        detail_layout.addWidget(self._large_value)
        detail_layout.addWidget(self._secondary_value)
        detail_layout.addStretch(1)

        graph_grid = QGridLayout()
        graph_grid.setContentsMargins(0, 0, 0, 0)
        graph_grid.setHorizontalSpacing(5)
        graph_grid.setVerticalSpacing(2)
        self._scale_max = QLabel("100", detail)
        self._scale_max.setObjectName("performanceScaleLabel")
        self._scale_max.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._graph = TelemetryHistoryGraph(detail)
        history_label = QLabel("60 SECONDS", detail)
        history_label.setObjectName("performanceHistoryLabel")
        self._scale_min = QLabel("0", detail)
        self._scale_min.setObjectName("performanceScaleLabel")
        self._scale_min.setAlignment(Qt.AlignmentFlag.AlignRight)
        graph_grid.addWidget(self._scale_max, 0, 1)
        graph_grid.addWidget(self._graph, 1, 0, 1, 2)
        graph_grid.addWidget(history_label, 2, 0)
        graph_grid.addWidget(self._scale_min, 2, 1)
        detail_layout.addLayout(graph_grid)
        body.addWidget(detail, 1)
        root.addLayout(body, 1)

    def _refresh_detail(self) -> None:
        reading = self._snapshot.metric(self.selected_metric)
        self._large_value.setText(reading.display_value)
        self._secondary_value.setText(reading.secondary_text)
        self._secondary_value.setVisible(bool(reading.secondary_text))
        self._scale_max.setText(_format_scale(reading.scale_max))
        self._scale_min.setText(_format_scale(reading.scale_min))
        self._graph.set_metric(reading)


def _format_scale(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"
