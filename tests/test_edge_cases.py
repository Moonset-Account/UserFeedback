"""边界与交错：自定义动作、重复 ID、空条件、未知动作、空核准清单等。"""

import unittest


try:
    from tests._helpers import base_config, node, run_config
except ImportError:  # python -I 下 cwd 不在 sys.path
    from _helpers import base_config, node, run_config
from region_preflight import PreflightConfigError


class EdgeAndInterleaveTest(unittest.TestCase):
    def test_custom_action_extends_registry(self):
        raw = base_config(
            node(
                "entry", "edge_forward", region="cn-east",
                next=node("snapshot", "snapshot_write", region="cn-east"),
            )
        )
        raw["actions"] = {
            "edge_forward": {"persists": False},
            "snapshot_write": {"persists": True},
        }
        report = run_config(raw).reports["order"]
        self.assertEqual(report.persistable_regions, ["cn-east"])
        transit_ids = {r.node_id for r in report.transit_records}
        self.assertEqual(transit_ids, {"entry"})

    def test_unknown_action_is_rejected(self):
        raw = base_config(node("entry", "teleport", region="cn-east"))
        with self.assertRaises(PreflightConfigError):
            run_config(raw)

    def test_duplicate_node_id_is_rejected_even_across_branches(self):
        raw = base_config(
            node(
                "entry", "forward", region="cn-east",
                next=node(
                    "cache", "cache_write", region="cn-east",
                    on={"cache_down": node("entry", "forward", region="cn-north")},
                ),
            )
        )
        with self.assertRaises(PreflightConfigError):
            run_config(raw)

    def test_empty_failover_condition_is_rejected(self):
        raw = base_config(
            node(
                "entry", "forward", region="cn-east",
                next=node(
                    "cache", "cache_write", region="cn-east",
                    on={"": node("cache_dr", "cache_write", region="cn-north")},
                ),
            )
        )
        with self.assertRaises(PreflightConfigError):
            run_config(raw)

    def test_empty_approval_list_is_rejected(self):
        raw = base_config(
            node("entry", "forward", region="cn-east"),
            approvals={"order": []},
        )
        with self.assertRaises(PreflightConfigError):
            run_config(raw)

    def test_findings_deduplicated_when_region_reached_from_multiple_paths(self):
        # 同一持久化节点只会在树中出现一次（ID 唯一）；同一节点的 finding
        # 通过 (code, node, region) 去重，保证报告稳定。
        raw = base_config(
            node(
                "entry", "forward", region="cn-east",
                next=node(
                    "cache", "cache_write", region="cn-east",
                    on={
                        "cache_down": node(
                            "cache_dr", "replicate", region="eu-central"
                        ),
                        "network_partition": node(
                            "cache_dr2", "replicate", region="eu-central"
                        ),
                    },
                ),
            )
        )
        result = run_config(raw)
        errors = [f for f in result.findings if f.severity == "error"]
        # 两个不同节点各自一条 error，不会互相吞并。
        self.assertEqual({f.node_id for f in errors}, {"cache_dr", "cache_dr2"})

    def test_interleaved_forward_and_persist_steps_on_same_branch(self):
        # 转发与落地步骤交错出现：区域集合按动作语义分别归类。
        raw = base_config(
            node(
                "entry", "forward", region="cn-east",
                next=node(
                    "relay_a", "forward", region="cn-north",
                    next=node(
                        "cache", "cache_write", region="cn-north",
                        next=node(
                            "relay_b", "forward", region="eu-central",
                            next=node("replica", "replicate", region="cn-east"),
                        ),
                    ),
                ),
            ),
            approvals={"order": ["cn-east", "cn-north"]},
        )
        report = run_config(raw).reports["order"]
        self.assertEqual(
            report.transitable_regions,
            ["cn-east", "cn-north", "eu-central"],
        )
        self.assertEqual(report.persistable_regions, ["cn-east", "cn-north"])
        self.assertEqual(report.transit_only_regions, ["eu-central"])
        # eu-central 只有转发，不触发未核准持久化。
        self.assertFalse(
            any(
                f.code == "UNAPPROVED_PERSISTENCE" and f.region == "eu-central"
                for f in report.findings
            )
        )

    def test_single_node_pipeline_is_supported(self):
        raw = base_config(node("entry", "cache_write", region="cn-east"))
        result = run_config(raw)
        self.assertTrue(result.passed)
        report = result.reports["order"]
        self.assertEqual(report.persistable_regions, ["cn-east"])
        self.assertEqual(report.cross_region_landings, [])


if __name__ == "__main__":
    unittest.main()
