"""Parser regression summary package (Python 3 standard library only)."""

from .models import Report, TestRecord
from .loader import LoadError, load_report, parse_report
from .compare import compare_reports
from .report import render_json, render_markdown

__all__ = [
    "LoadError",
    "Report",
    "TestRecord",
    "compare_reports",
    "load_report",
    "parse_report",
    "render_json",
    "render_markdown",
]
