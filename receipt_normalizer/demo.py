"""Local synthetic demonstration.

Run with::

    python3 -m receipt_normalizer.demo

All data below is invented and stays in memory; it does not read audit logs,
credentials, or network resources.
"""

from __future__ import annotations

import json

from .normalizer import normalize_receipts


TABLES = [
    {
        "version": "synthetic-table-v1",
        "valid_from_seq": 1,
        "mappings": {
            "native": {
                1001: {
                    "name": "file.read",
                    "target": {"kind": "file_descriptor", "identifier": 0},
                },
                2001: {
                    "name": "ipc.dispatch",
                    "multiplex": {
                        "selector_param": 0,
                        "operations": {
                            1: {
                                "name": "semaphore.acquire",
                                "target": {"kind": "semaphore", "identifier": 1},
                            }
                        },
                    },
                },
            },
            "compat": {
                # The same numeric entry has a different meaning in compat ABI.
                1001: {
                    "name": "registry.value.write",
                    "target": {"kind": "registry_value", "identifier": 0},
                },
                2001: {
                    "name": "compat.ipc.dispatch",
                    "multiplex": {
                        # Compat calling convention places the selector elsewhere.
                        "selector_param": 2,
                        "operations": {
                            9: {
                                "name": "compat.shared_memory.attach",
                                "target": {"kind": "shm_key", "identifier": 1},
                            }
                        },
                    },
                },
            },
        },
    }
]

SWITCHES = [
    {"process_instance": "proc-native", "source_seq": 0, "image": "/synthetic/native-app", "abi": "native"},
    {"process_instance": "proc-compat", "source_seq": 0, "image": "/synthetic/compat-app", "abi": "compat"},
]

RECORDS = [
    {"process_instance": "proc-native", "source_seq": 10, "action_code": 1001, "params": [4]},
    {"process_instance": "proc-compat", "source_seq": 11, "action_code": 1001, "params": ["HKLM\\Synthetic\\Value"]},
    {"process_instance": "proc-native", "source_seq": 12, "action_code": 2001, "params": [1, 4242]},
    {"process_instance": "proc-compat", "source_seq": 13, "action_code": 2001, "params": ["shm", 77, 9]},
    {"process_instance": "proc-unknown", "source_seq": 14, "action_code": 1001, "params": []},
]


def main() -> None:
    results = normalize_receipts(
        RECORDS, tables=TABLES, image_switches=SWITCHES
    )
    print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
