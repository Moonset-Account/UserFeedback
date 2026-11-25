"""Local synthetic audit data.

The scenario is entirely fabricated and exercises repartitioning, same-name
volume identity changes, late receipts, overlapping intervals, and exact
handoff-time boundaries. It contains no real device or user information.
"""

from __future__ import annotations

from typing import Any


def build_scenario() -> dict[str, Any]:
    return {
        "as_of": "2026-03-01T12:00:00Z",
        "devices": [
            {
                "device_id": "D-1",
                "hostname": "synthetic-laptop-1",
                "handoff_at": "2026-03-01T12:00:00Z",
                "current_layout_version": 2,
            },
            {
                "device_id": "D-2",
                "hostname": "synthetic-laptop-2",
                "handoff_at": "2026-03-02T09:30:00Z",
            },
            {
                "device_id": "D-3",
                "hostname": "synthetic-laptop-3",
                "handoff_at": "2026-03-02T10:00:00+00:00",
            },
        ],
        "layouts": [
            {
                "device_id": "D-1",
                "version": 1,
                "valid_from": "2026-01-05T08:00:00Z",
                "valid_to": "2026-02-01T08:00:00Z",
                "partitions": [
                    {
                        "volume_name": "System",
                        "volume_instance_id": "D1-SYS-OLD",
                        "start": 0,
                        "end": 400,
                        "unit": "MiB",
                    },
                    {
                        "volume_name": "Data",
                        "volume_instance_id": "D1-DATA-OLD",
                        "start": 400,
                        "end": 1000,
                        "unit": "MiB",
                    },
                ],
            },
            {
                "device_id": "D-1",
                "version": 2,
                "valid_from": "2026-02-01T08:00:00Z",
                "partitions": [
                    {
                        "volume_name": "System",
                        "volume_instance_id": "D1-SYS-NEW",
                        "start": 0,
                        "end": 500,
                        "unit": "MiB",
                        "encryption_policy": {"policy_id": "POL-SYS", "required": True},
                    },
                    {
                        "volume_name": "Data",
                        "volume_instance_id": "D1-DATA-NEW",
                        "start": 500,
                        "end": 1200,
                        "unit": "MiB",
                        "encryption_policy": {"policy_id": "POL-DATA", "required": True},
                    },
                ],
            },
            {
                "device_id": "D-2",
                "version": "v1",
                "valid_from": "2026-02-20T00:00:00Z",
                "partitions": [
                    {
                        "volume_name": "SecureData",
                        "volume_instance_id": "D2-DATA-NEW",
                        "start_offset": 100,
                        "size": 200,
                        "unit": "MiB",
                    }
                ],
            },
            {
                "device_id": "D-3",
                "version": 1,
                "partitions": [
                    {
                        "volume_name": "Scratch",
                        "volume_instance_id": "D3-SCRATCH",
                        "start": 0,
                        "end": 80,
                        "encryption_required": False,
                    }
                ],
            },
        ],
        "policies": [
            {
                "policy_id": "POL-D2-NEW-PARTITION",
                "device_id": "D-2",
                "volume_instance_id": "D2-DATA-NEW",
                "layout_version": "v1",
                "required": True,
                "enabled_at": "2026-02-20T00:00:00Z",
            }
        ],
        "receipts": [
            {
                "receipt_id": "R-OLD-SYS",
                "device_id": "D-1",
                "volume_instance_id": "D1-SYS-OLD",
                "start": 0,
                "end": 400,
                "status": "confirmed",
                "effective_at": "2026-01-10T10:00:00Z",
                "received_at": "2026-01-10T10:05:00Z",
            },
            {
                "receipt_id": "R-OLD-DATA-1",
                "device_id": "D-1",
                "volume_instance_id": "D1-DATA-OLD",
                "start": 400,
                "end": 700,
                "status": "success",
                "effective_at": "2026-01-11T09:00:00Z",
                "received_at": "2026-01-11T09:02:00Z",
            },
            {
                "receipt_id": "R-OLD-DATA-2",
                "device_id": "D-1",
                "volume_instance_id": "D1-DATA-OLD",
                "start": 700,
                "end": 1000,
                "confirmed": True,
                "effective_at": "2026-01-12T09:00:00Z",
                "available_at": "2026-01-12T09:03:00Z",
            },
            {
                "receipt_id": "R-NEW-SYS-2",
                "device_id": "D-1",
                "volume_instance_id": "D1-SYS-NEW",
                "start": 240,
                "end": 500,
                "status": "verified",
                "effective_at": "2026-02-12T15:00:00Z",
                "received_at": "2026-02-12T15:01:00Z",
            },
            {
                "receipt_id": "R-NEW-SYS-1",
                "device_id": "D-1",
                "volume_instance_id": "D1-SYS-NEW",
                "start": 0,
                "end": 250,
                "status": "confirmed",
                "effective_at": "2026-02-12T14:00:00Z",
                "received_at": "2026-02-12T14:01:00Z",
            },
            {
                "receipt_id": "R-NEW-DATA-PARTIAL",
                "device_id": "D-1",
                "volume_instance_id": "D1-DATA-NEW",
                "start": 500,
                "end": 900,
                "status": "confirmed",
                "effective_at": "2026-02-25T11:00:00Z",
                "available_at": "2026-02-25T11:04:00Z",
            },
            {
                "receipt_id": "R-NEW-DATA-LATE",
                "device_id": "D-1",
                "volume_instance_id": "D1-DATA-NEW",
                "start": 900,
                "end": 1200,
                "status": "confirmed",
                "effective_at": "2026-03-01T11:59:00Z",
                # One minute after the 12:00 handoff cutoff.
                "received_at": "2026-03-01T12:01:00Z",
            },
            {
                "receipt_id": "R-D2-LATE",
                "device_id": "D-2",
                "volume_instance_id": "D2-DATA-NEW",
                "start": 100,
                "end": 300,
                "status": "confirmed",
                "effective_at": "2026-03-02T09:29:00Z",
                "received_at": "2026-03-02T09:31:00Z",
            },
        ],
    }
