"""面向本地公开 SVG 实验图的数值采集器（仅依据可见图形）。"""

from .engine import collect, collect_file, PROTOCOL_SUBSET
from .axis import CalibrationError
from .transforms import UnsupportedFeature

__all__ = [
    "collect",
    "collect_file",
    "CalibrationError",
    "UnsupportedFeature",
    "PROTOCOL_SUBSET",
]
