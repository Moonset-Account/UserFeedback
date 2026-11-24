"""Quantization calibration group/scale selection.

This package implements a purely defensive, offline calibration helper:
given per-channel activation frequency tables, the signed integer bit widths
and scales supported by a device, and the contiguous channel spans that may
share a scale, it picks the cheapest (smallest byte footprint) grouping and
per-group quantization scale whose per-channel reconstruction error stays
within a tolerance.
"""

from .calibration import (
    CalibrationResult,
    ChannelError,
    GroupSelection,
    QuantSpec,
    evaluate_channel,
    quantize_dequantize,
    select_calibration,
)

__all__ = [
    "CalibrationResult",
    "ChannelError",
    "GroupSelection",
    "QuantSpec",
    "evaluate_channel",
    "quantize_dequantize",
    "select_calibration",
]
