"""Capacity audit toolkit for load-test arrival, throttle, completion,
withdrawal, retry/reissue, and capacity-loss reconciliation.

The public entry point is :func:`capacity_audit.analyze`.
"""

from .analyzer import CapacityAnalyzer, analyze
from .clock import ClockSpec, ManualClock, MetricRange, TimeBounds
from .ingest import (
    DuplicateReport,
    IngestionResult,
    SequenceGap,
    ingest_records,
)
from .model import Event, EventValidationError, Issue
from .report import (
    AnalysisResult,
    AttributionEntry,
    CohortSummary,
    Incident,
    QueueInterval,
    ReissueAttribution,
    WindowReport,
)
from .windows import MetricBounds, WindowAssignment, WindowGrid, Window
from .reporting import format_report, format_reissue_journal, format_window_table

__version__ = "1.0.0"

__all__ = [
    "analyze",
    "CapacityAnalyzer",
    "ClockSpec",
    "ManualClock",
    "TimeBounds",
    "MetricRange",
    "ingest_records",
    "IngestionResult",
    "DuplicateReport",
    "SequenceGap",
    "Event",
    "EventValidationError",
    "Issue",
    "AnalysisResult",
    "WindowReport",
    "QueueInterval",
    "Incident",
    "ReissueAttribution",
    "AttributionEntry",
    "CohortSummary",
    "MetricBounds",
    "WindowAssignment",
    "WindowGrid",
    "Window",
    "format_report",
    "format_window_table",
    "format_reissue_journal",
    "__version__",
]
