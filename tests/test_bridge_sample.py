from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from topology_audit import AuditSession  # noqa: E402
from topology_audit.sample import (  # noqa: E402
    EXPECTED_SAMPLE_MAPPING,
    sample_data,
)


class BridgeSampleTests(unittest.TestCase):
    def test_global_bridge_ports_make_sample_unique(self) -> None:
        session = AuditSession.create(**sample_data())
        report = session.report

        self.assertTrue(report.feasible)
        self.assertTrue(report.complete)
        self.assertEqual(1, report.match_count)
        self.assertEqual(EXPECTED_SAMPLE_MAPPING, report.mapping)
        self.assertEqual({}, report.ambiguous)

        match = report.matches[0]
        self.assertTrue(match.orientation_dict()["alpha"])
        self.assertFalse(match.orientation_dict()["beta"])

    def test_output_is_deterministic(self) -> None:
        first = AuditSession.create(**sample_data()).report.to_dict()
        second = AuditSession.create(**sample_data()).report.to_dict()
        self.assertEqual(first, second)

    def test_local_chains_alone_are_swap_ambiguous(self) -> None:
        data = copy.deepcopy(sample_data())
        for topology_name in ("logical", "observed"):
            topology = data[topology_name]
            topology["devices"] = [
                device for device in topology["devices"] if device["id"] != "bridge-0"
            ]
            topology["connections"] = [
                conn
                for conn in topology["connections"]
                if not str(conn["a"]).startswith("bridge-0:")
                and not str(conn["b"]).startswith("bridge-0:")
            ]
        data["bridges"] = []

        report = AuditSession.create(**data).report
        self.assertEqual(2, report.match_count)
        self.assertEqual(
            ["ttyUSB1", "ttyUSB2"], report.candidates["alpha-meter"]
        )
        self.assertEqual(
            ["ttyUSB1", "ttyUSB2"], report.candidates["beta-meter"]
        )
        self.assertNotIn("alpha-meter", report.mapping)

    def test_reverse_permission_is_a_boundary(self) -> None:
        data = copy.deepcopy(sample_data())
        data["bridges"][0]["allow_reverse"] = False
        report = AuditSession.create(**data).report

        self.assertFalse(report.feasible)
        self.assertEqual(0, report.match_count)
        self.assertEqual({}, report.mapping)

    def test_segment_marked_non_reversible_also_rejects_reverse(self) -> None:
        data = copy.deepcopy(sample_data())
        data["logical"]["segments"][0]["reversible"] = False
        report = AuditSession.create(**data).report
        self.assertFalse(report.feasible)
        self.assertEqual(0, report.match_count)


if __name__ == "__main__":
    unittest.main()
