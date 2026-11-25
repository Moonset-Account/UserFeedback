"""Small local synthetic topology used across tests.

Topology::

    device-1 (ancestor being revoked)
      └── browser-1, derived at dc-source
             └── transferred to dc-target and closed there

All data is invented in-process; there are no network or filesystem fixtures.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

SOURCE = "dc-source"
TARGET = "dc-target"
SLOW = "dc-slow-unrelated"

ROOT = "device-1"
BROWSER = "browser-1"
TRANSFER = "transfer-1"

SOURCE_BARRIER = 4
TARGET_BARRIER = 9


def main_events() -> list[dict[str, Any]]:
    """Return a fresh copy of the happy-path causal event log."""

    return [
        {
            "event_id": "derive-1",
            "type": "derivation",
            "issuer": SOURCE,
            "sequence": 2,
            "session_id": BROWSER,
            "parent_session_id": ROOT,
            "predecessor_id": None,
        },
        {
            "event_id": "transfer-send-1",
            "type": "transfer",
            "issuer": SOURCE,
            "sequence": 3,
            "session_id": BROWSER,
            "transfer_id": TRANSFER,
            "from_endpoint": SOURCE,
            "to_endpoint": TARGET,
            "predecessor_id": "derive-1",
        },
        {
            "event_id": "transfer-ack-1",
            "type": "transfer_receipt",
            "issuer": TARGET,
            "sequence": 7,
            "session_id": BROWSER,
            "transfer_id": TRANSFER,
            "receipt_id": "receipt-1",
            "predecessor_id": "transfer-send-1",
        },
        {
            "event_id": "close-1",
            "type": "close",
            "issuer": TARGET,
            "sequence": 8,
            "session_id": BROWSER,
            "predecessor_id": "transfer-ack-1",
        },
    ]


def barrier_positions() -> dict[str, int]:
    return {SOURCE: SOURCE_BARRIER, TARGET: TARGET_BARRIER}


def passed_watermarks() -> dict[str, int]:
    return {SOURCE: SOURCE_BARRIER, TARGET: TARGET_BARRIER + 1}


def copied_events(events: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return deepcopy(events if events is not None else main_events())


def unrelated_backlog_events() -> list[dict[str, Any]]:
    """Many open, unacked sessions that must not affect ``device-1``."""

    events: list[dict[str, Any]] = []
    for index in range(40):
        parent = f"unrelated-device-{index}"
        child = f"unrelated-browser-{index}"
        transfer = f"unrelated-transfer-{index}"
        events.extend(
            [
                {
                    "event_id": f"unrelated-derive-{index}",
                    "type": "derivation",
                    "issuer": SLOW,
                    "sequence": 1000 + index * 3,
                    "session_id": child,
                    "parent_session_id": parent,
                },
                {
                    "event_id": f"unrelated-transfer-send-{index}",
                    "type": "transfer",
                    "issuer": SLOW,
                    "sequence": 1001 + index * 3,
                    "session_id": child,
                    "transfer_id": transfer,
                    "from_endpoint": SLOW,
                    "to_endpoint": "dc-other",
                },
                # Intentionally no receipt and no close.
            ]
        )
    return events
