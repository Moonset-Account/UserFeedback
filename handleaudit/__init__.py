"""Offline reconstruction of inherited sensitive object handles.

Public entry points:
    analyze_dataset(dataset) -> dict
    analyze_events(events, snapshots, launches, policy, ...) -> dict
    decode_access_mask(mask) -> list[str]
"""

from .analyzer import (
    analyze_dataset,
    analyze_events,
    decode_access_mask,
    KNOWN_ACCESS_BITS,
)

__all__ = [
    "analyze_dataset",
    "analyze_events",
    "decode_access_mask",
    "KNOWN_ACCESS_BITS",
]
