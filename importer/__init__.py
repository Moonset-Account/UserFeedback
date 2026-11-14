"""Local, whitelist-based importer for versioned task records.

Only ``(version, task_type)`` pairs present in the static registry can produce
data objects.  Task types are never used as module names, attribute paths, or
class names for dynamic loading.
"""

from .core import REPORT_TOOL, exit_code, import_file
from .limits import Limits, LimitsConfigError, load_limits

__all__ = [
    "Limits",
    "LimitsConfigError",
    "REPORT_TOOL",
    "exit_code",
    "import_file",
    "load_limits",
]
