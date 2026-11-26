from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from topology_audit import AuditSession, Topology, audit_topology  # noqa: E402


class ConstraintTests(unittest.TestCase):
    def test_forbidden_shared_location_eliminates_colocated_match(self) -> None:
        logical = Topology.from_dict(
            {"devices": [{"id": "a", "type": "x"}, {"id": "b", "type": "x"}]}
        )
        shared_observed = Topology.from_dict(
            {
                "devices": [
                    {"id": "o0", "type": "x", "location": "same-hub-port"},
                    {"id": "o1", "type": "x", "location": "same-hub-port"},
                ]
            }
        )

        allowed = AuditSession(logical, shared_observed)
        self.assertEqual(2, allowed.report.match_count)

        forbidden = AuditSession(
            logical,
            shared_observed,
            exclusive_locations=["same-hub-port"],
        )
        self.assertFalse(forbidden.report.feasible)
        self.assertEqual(0, forbidden.report.match_count)

        alias_report = audit_topology(
            logical,
            shared_observed,
            forbidden_share_locations=["same-hub-port"],
        )
        self.assertEqual(0, alias_report.match_count)

    def test_non_exclusive_locations_may_still_be_shared(self) -> None:
        logical = Topology.from_dict(
            {"devices": [{"id": "a", "type": "x"}, {"id": "b", "type": "x"}]}
        )
        observed = Topology.from_dict(
            {
                "devices": [
                    {"id": "o0", "type": "x", "location": "ordinary-bus"},
                    {"id": "o1", "type": "x", "location": "ordinary-bus"},
                ]
            }
        )
        session = AuditSession(
            logical, observed, exclusive_locations=["different-location"]
        )
        self.assertEqual(2, session.report.match_count)

    def test_anonymous_type_family_is_compatible_only_when_marked(self) -> None:
        logical = Topology.from_dict({"devices": [{"id": "m1", "type": "meter-v2"}]})
        observed = Topology.from_dict({"devices": [{"id": "ttyUSB9", "type": "meter-v3"}]})
        families = {"meter-v2": "meter", "meter-v3": "meter"}

        strict = AuditSession(logical, observed, type_families=families)
        self.assertEqual(0, strict.report.match_count)

        anonymous = AuditSession(
            logical,
            observed,
            type_families=families,
            anonymous_types=["meter"],
        )
        self.assertEqual(1, anonymous.report.match_count)
        self.assertEqual("ttyUSB9", anonymous.report.mapping["m1"])

    def test_interface_count_and_kind_are_checked(self) -> None:
        logical = Topology.from_dict(
            {
                "devices": [
                    {
                        "id": "io1",
                        "type": "io",
                        "interfaces": [
                            {"name": "up", "kind": "rs485", "role": "upstream"},
                            {"name": "down", "kind": "rs485", "role": "downstream"},
                        ],
                    }
                ]
            }
        )
        bad_observed = Topology.from_dict(
            {
                "devices": [
                    {
                        "id": "o0",
                        "type": "io",
                        "interfaces": [{"name": "only", "kind": "ethernet"}],
                    }
                ]
            }
        )
        good_observed = Topology.from_dict(
            {
                "devices": [
                    {
                        "id": "o0",
                        "type": "io",
                        "interfaces": [
                            {"name": "p1", "kind": "rs485", "role": "upstream"},
                            {"name": "p2", "kind": "rs485", "role": "downstream"},
                        ],
                    }
                ]
            }
        )

        self.assertEqual(0, AuditSession(logical, bad_observed).report.match_count)
        good = AuditSession(logical, good_observed).report
        self.assertEqual(1, good.match_count)
        self.assertEqual("o0", good.mapping["io1"])

    def test_unequal_populations_have_no_complete_bijection(self) -> None:
        logical = Topology.from_dict({"devices": [{"id": "a", "type": "x"}]})
        observed = Topology.from_dict(
            {"devices": [{"id": "o0", "type": "x"}, {"id": "o1", "type": "x"}]}
        )
        report = AuditSession(logical, observed).report
        self.assertFalse(report.feasible)
        self.assertEqual(0, report.match_count)

    def test_solution_limit_is_reported_as_incomplete(self) -> None:
        logical = Topology.from_dict(
            {"devices": [{"id": f"l{i}", "type": "x"} for i in range(3)]}
        )
        observed = Topology.from_dict(
            {"devices": [{"id": f"o{i}", "type": "x"} for i in range(3)]}
        )
        session = AuditSession(logical, observed, solution_limit=1)
        self.assertEqual(1, session.report.match_count)
        self.assertFalse(session.report.complete)

    def test_connection_pattern_survives_temporary_id_permutation(self) -> None:
        logical = Topology.from_dict(
            {
                "devices": [
                    {
                        "id": "meter",
                        "type": "meter",
                        "interfaces": [
                            {"name": "up", "role": "upstream", "kind": "rs485"},
                            {"name": "down", "role": "downstream", "kind": "rs485"},
                        ],
                    },
                    {
                        "id": "valve",
                        "type": "valve",
                        "interfaces": [
                            {"name": "up", "role": "upstream", "kind": "rs485"},
                            {"name": "down", "role": "downstream", "kind": "rs485"},
                        ],
                    },
                ],
                "connections": [
                    {"a": "meter:down", "b": "valve:up", "kind": "rs485"}
                ],
            }
        )
        observed = Topology.from_dict(
            {
                "devices": [
                    {
                        "id": "ttyUSB0",
                        "type": "valve",
                        "interfaces": [
                            {"name": "up", "role": "upstream", "kind": "rs485"},
                            {"name": "down", "role": "downstream", "kind": "rs485"},
                        ],
                    },
                    {
                        "id": "ttyUSB1",
                        "type": "meter",
                        "interfaces": [
                            {"name": "up", "role": "upstream", "kind": "rs485"},
                            {"name": "down", "role": "downstream", "kind": "rs485"},
                        ],
                    },
                ],
                "connections": [
                    {"a": "ttyUSB1:down", "b": "ttyUSB0:up", "kind": "rs485"}
                ],
            }
        )
        report = AuditSession(logical, observed).report
        self.assertEqual(1, report.match_count)
        self.assertEqual(
            {"meter": "ttyUSB1", "valve": "ttyUSB0"},
            report.mapping,
        )


if __name__ == "__main__":
    unittest.main()
