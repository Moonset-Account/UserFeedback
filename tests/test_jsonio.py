"""JSON serialisation tests, including the shipped example fixture."""

from __future__ import annotations

import json
import os
import unittest

from helpers import WORKSPACE, at

from revocation_store import Reason, TableEntry, table_from_dict, table_to_dict, tables_from_json

EXAMPLE = os.path.join(WORKSPACE, "examples", "tables.json")


class JsonRoundTripTests(unittest.TestCase):
    def test_shipped_example_fixture_parses(self) -> None:
        with open(EXAMPLE, encoding="utf-8") as handle:
            tables = tables_from_json(json.load(handle))
        self.assertEqual(len(tables), 2)
        self.assertEqual(tables[0].base_number, tables[0].number)
        self.assertEqual(tables[1].base_number, 1)
        self.assertEqual(tables[1].entries[0].reason, Reason.CERTIFICATE_HOLD)

    def test_round_trip_preserves_table(self) -> None:
        original = table_from_dict(
            {
                "kind": "DELTA",
                "issuer": "Example CA",
                "scope": "root",
                "base_number": 3,
                "number": 4,
                "this_update": at(0).isoformat(),
                "next_update": at(12).isoformat(),
                "entries": [
                    {
                        "serial": "AA",
                        "reason": "removeFromCRL",
                        "revocation_time": at(5).isoformat(),
                    }
                ],
            }
        )
        restored = table_from_dict(table_to_dict(original))
        self.assertEqual(original.fingerprint(), restored.fingerprint())

    def test_delta_requires_base_number(self) -> None:
        with self.assertRaises(Exception) as caught:
            table_from_dict(
                {
                    "kind": "DELTA",
                    "issuer": "Example CA",
                    "scope": "root",
                    "number": 2,
                    "this_update": at(0).isoformat(),
                    "next_update": at(12).isoformat(),
                }
            )
        self.assertIn("base_number", str(caught.exception))

    def test_entry_defaults_reason_to_unspecified(self) -> None:
        table = table_from_dict(
            {
                "kind": "FULL",
                "issuer": "Example CA",
                "scope": "root",
                "number": 1,
                "this_update": at(0).isoformat(),
                "next_update": at(12).isoformat(),
                "entries": [{"serial": "BB"}],
            }
        )
        self.assertEqual(table.entries[0].reason, Reason.UNSPECIFIED)


if __name__ == "__main__":
    unittest.main()
