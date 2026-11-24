"""JSON / dict 场景装载、校验与端到端审查。"""

import json
import unittest
from pathlib import Path

from route_audit import (
    ScenarioFormatError,
    TraceStatus,
    load_scenario_dict,
    load_scenario_file,
)

EXAMPLE = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "withdraw_scenario.json"
)


class TestLoader(unittest.TestCase):
    def test_load_example_file_end_to_end(self) -> None:
        scenario = load_scenario_file(EXAMPLE)
        report = scenario.run()

        # 基线两条探测都合规（走已批准的 edge-a）
        for trace in report.baseline.values():
            self.assertEqual(trace.status, TraceStatus.DELIVERED)

        # 撤下专用路由后恰好一段前缀报违规，且三元组齐全
        self.assertEqual(len(report.findings), 1)
        finding = report.findings[0]
        self.assertEqual(finding.affected_prefix.text, "10.10.1.0/24")
        self.assertEqual(finding.first_unauthorized_step.domain, "edge-b")
        self.assertEqual(
            finding.trigger_change_id, "chg-withdraw-ded-1"
        )

        # 变化后的追踪快照记录了回退路径
        after = report.traces_by_change["chg-withdraw-ded-1"]["probe-seg-1"]
        self.assertEqual(after.status, TraceStatus.UNAUTHORIZED_TRANSIT)
        # 恢复变化后重新合规
        restored = report.traces_by_change["chg-restore-ded-1"]["probe-seg-1"]
        self.assertEqual(restored.status, TraceStatus.DELIVERED)

    def test_round_trip_dict_payload(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        scenario = load_scenario_dict(data)
        self.assertEqual({d for d in scenario.state.domains},
                         {"corp", "edge-a", "edge-b", "dc"})
        self.assertEqual(len(scenario.probes), 2)
        self.assertEqual(len(scenario.changes), 2)

    def test_missing_required_field_rejected(self) -> None:
        with self.assertRaises(ScenarioFormatError):
            load_scenario_dict({"domains": ["d1"]})  # 缺 routes/probes

    def test_unknown_domain_reference_rejected(self) -> None:
        data = {
            "domains": ["d1"],
            "routes": [
                {"route_id": "r1", "domain": "ghost",
                 "prefix": "10.0.0.0/8", "next_hop": None}
            ],
            "probes": [
                {"probe_id": "p1", "origin_domain": "d1",
                 "target": "10.0.0.0/8"}
            ],
        }
        with self.assertRaises(ScenarioFormatError):
            load_scenario_dict(data)

    def test_duplicate_domain_registration_rejected(self) -> None:
        data = {"domains": ["d1", "d1"], "routes": [], "probes": []}
        with self.assertRaises(ScenarioFormatError):
            load_scenario_dict(data)

    def test_bad_direction_enum_rejected(self) -> None:
        data = {
            "domains": ["d1", "d2", "d3"],
            "grants": [
                {"transit_domain": "d2", "from_domain": "d1",
                 "to_domain": "d3", "direction": "sideways"}
            ],
            "routes": [],
            "probes": [],
        }
        with self.assertRaises(ValueError):
            load_scenario_dict(data)

    def test_add_change_requires_route_payload(self) -> None:
        data = {
            "domains": ["d1"],
            "routes": [],
            "probes": [
                {"probe_id": "p1", "origin_domain": "d1",
                 "target": "10.0.0.0/8"}
            ],
            "changes": [
                {"change_id": "c1", "action": "add",
                 "domain": "d1", "route_id": "r1"}
            ],
        }
        with self.assertRaises(ScenarioFormatError):
            load_scenario_dict(data)


if __name__ == "__main__":
    unittest.main()
