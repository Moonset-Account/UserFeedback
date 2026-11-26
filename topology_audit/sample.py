"""Synthetic sample: identical local chains, unique via global bridge ports."""

from __future__ import annotations

from typing import Any

from .session import AuditSession


def sample_data() -> dict[str, Any]:
    """Return the sample as JSON-serializable dictionaries.

    The two logical segments have the same local meter -> valve pattern.  In
    the observed topology, alpha has been reverse-hung on bridge port J1,
    while beta remains normal on J2.  The stable global bridge ports remove
    both the segment-swap and identity ambiguities.
    """

    def peripheral(device_id: str, device_type: str, location: str) -> dict[str, Any]:
        return {
            "id": device_id,
            "type": device_type,
            "location": location,
            "interfaces": [
                {"name": "up", "kind": "rs485", "role": "upstream"},
                {"name": "down", "kind": "rs485", "role": "downstream"},
            ],
        }

    logical = {
        "devices": [
            {
                "id": "bridge-0",
                "type": "rs485-bridge",
                "stable": True,
                "interfaces": [
                    {"name": "j1", "kind": "rs485", "role": "peer"},
                    {"name": "j2", "kind": "rs485", "role": "peer"},
                ],
            },
            peripheral("alpha-meter", "power-meter", "rack-1/j1/far"),
            peripheral("alpha-valve", "ball-valve", "rack-1/j1/near"),
            peripheral("beta-meter", "power-meter", "rack-2/j2/near"),
            peripheral("beta-valve", "ball-valve", "rack-2/j2/far"),
        ],
        "connections": [
            {"a": "bridge-0:j1", "b": "alpha-meter:up", "kind": "rs485"},
            {"a": "alpha-meter:down", "b": "alpha-valve:up", "kind": "rs485"},
            {"a": "bridge-0:j2", "b": "beta-meter:up", "kind": "rs485"},
            {"a": "beta-meter:down", "b": "beta-valve:up", "kind": "rs485"},
        ],
        "segments": [
            {
                "id": "alpha",
                "devices": ["alpha-meter", "alpha-valve"],
                "reversible": True,
                "first_interface": "up",
                "last_interface": "down",
            },
            {
                "id": "beta",
                "devices": ["beta-meter", "beta-valve"],
                "reversible": True,
                "first_interface": "up",
                "last_interface": "down",
            },
        ],
    }

    observed = {
        "devices": [
            {
                "id": "bridge-0",
                "type": "rs485-bridge",
                "stable": True,
                "interfaces": [
                    {"name": "j1", "kind": "rs485", "role": "peer"},
                    {"name": "j2", "kind": "rs485", "role": "peer"},
                ],
            },
            # J1 chain is physically valve -> meter from the bridge, i.e. the
            # reverse of alpha's ledger order meter -> valve.
            peripheral("ttyUSB0", "ball-valve", "rack-1/j1/near"),
            peripheral("ttyUSB1", "power-meter", "rack-1/j1/far"),
            # J2 chain remains meter -> valve.
            peripheral("ttyUSB2", "power-meter", "rack-2/j2/near"),
            peripheral("ttyUSB3", "ball-valve", "rack-2/j2/far"),
        ],
        "connections": [
            {"a": "bridge-0:j1", "b": "ttyUSB0:up", "kind": "rs485"},
            {"a": "ttyUSB0:down", "b": "ttyUSB1:up", "kind": "rs485"},
            {"a": "bridge-0:j2", "b": "ttyUSB2:up", "kind": "rs485"},
            {"a": "ttyUSB2:down", "b": "ttyUSB3:up", "kind": "rs485"},
        ],
        "segments": [
            {"id": "observed-j1", "devices": ["ttyUSB0", "ttyUSB1"], "reversible": False},
            {"id": "observed-j2", "devices": ["ttyUSB2", "ttyUSB3"], "reversible": False},
        ],
    }

    bridges = [
        {
            "id": "BRIDGE-ALPHA-J1",
            "bridge_port": "bridge-0:j1",
            "segment": "alpha",
            "end": "first",
            "interface": "up",
            "kind": "rs485",
            "allow_reverse": True,
        },
        {
            "id": "BRIDGE-BETA-J2",
            "bridge_port": "bridge-0:j2",
            "segment": "beta",
            "end": "first",
            "interface": "up",
            "kind": "rs485",
            "allow_reverse": True,
        },
    ]

    return {
        "logical": logical,
        "observed": observed,
        "bridges": bridges,
        "exclusive_locations": ["rack-1/j1/near", "rack-2/j2/near"],
    }


EXPECTED_SAMPLE_MAPPING = {
    "bridge-0": "bridge-0",
    "alpha-meter": "ttyUSB1",
    "alpha-valve": "ttyUSB0",
    "beta-meter": "ttyUSB2",
    "beta-valve": "ttyUSB3",
}


def build_sample_session() -> AuditSession:
    data = sample_data()
    return AuditSession.create(**data)
