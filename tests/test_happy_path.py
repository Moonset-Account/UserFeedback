"""正常场景：全部步骤在本区、仅转发节点与持久化节点分类正确。"""

import unittest

try:
    from tests._helpers import base_config, node, run_config
except ImportError:  # python -I 下 cwd 不在 sys.path
    from _helpers import base_config, node, run_config


class HappyPathTest(unittest.TestCase):
    def test_all_home_passes_without_findings(self):
        raw = base_config(
            node(
                "entry", "forward", region="cn-east",
                next=node(
                    "cache", "cache_write",
                    next=node(
                        "async_logger", "async_log",
                        next=node("dr_replica", "replicate"),
                    ),
                ),
            )
        )
        result = run_config(raw)

        self.assertTrue(result.passed)
        report = result.reports["order"]
        self.assertEqual(report.transitable_regions, ["cn-east"])
        self.assertEqual(report.persistable_regions, ["cn-east"])
        self.assertEqual(report.transit_only_regions, [])
        self.assertEqual(report.main_route_regions, ["cn-east"])
        self.assertEqual(report.cross_region_landings, [])
        self.assertEqual(report.findings, [])

    def test_forward_only_region_recorded_as_transit_not_persistable(self):
        raw = base_config(
            node(
                "entry", "forward", region="cn-east",
                next=node(
                    "edge", "forward", region="cn-north",
                    next=node("cache", "cache_write", region="cn-east"),
                ),
            )
        )
        result = run_config(raw)
        report = result.reports["order"]

        # 链路可经过 cn-north，但没有数据在那里落地。
        self.assertEqual(report.transitable_regions, ["cn-east", "cn-north"])
        self.assertEqual(report.persistable_regions, ["cn-east"])
        self.assertEqual(report.transit_only_regions, ["cn-north"])

        transit = {r.node_id: r for r in report.transit_records}
        self.assertEqual(set(transit), {"entry", "edge"})
        self.assertFalse(transit["edge"].on_failover)
        # 主路径经过异地转发节点 -> 提示主路径未全程本区（warning，不阻断）。
        codes = {f.code for f in report.findings}
        self.assertIn("MAIN_ROUTE_OUTSIDE_HOME", codes)
        self.assertTrue(result.passed)

    def test_persistable_and_transitable_sets_are_disjoint_in_report(self):
        raw = base_config(
            node(
                "entry", "forward", region="cn-east",
                next=node("cache", "cache_write", region="cn-east"),
            )
        )
        report = run_config(raw).reports["order"]
        self.assertEqual(
            set(report.transit_only_regions),
            set(report.transitable_regions) - set(report.persistable_regions),
        )


if __name__ == "__main__":
    unittest.main()
