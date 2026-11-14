"""错误响应映射层：公开错误映射与内部诊断字段脱敏。"""

from .registry import ErrorRegistry
from .mapper import map_error
from .redact import sanitize, collect_secret_paths, REDACTED, MAX_DEPTH

__all__ = [
    "ErrorRegistry",
    "map_error",
    "sanitize",
    "collect_secret_paths",
    "REDACTED",
    "MAX_DEPTH",
]
