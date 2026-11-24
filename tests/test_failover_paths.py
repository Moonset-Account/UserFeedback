"""核心场景：主路径全程本区，回退分支在其他区域留下缓存/副本。"""

import unittest

try:
    from tests._helpers import base_config, node, run_config
except ImportError:  # python -I 下 cwd 不在 sys.path
    from _helpers import base_config, node, run_config


class CrossRegionFailoverTest(unittest.TestCase):
    def _pipeline(self, failover_region, action="cache_write"):
        return base_config(
            node(
                "entry", "forward", region="cn-east",
                next=node(
                    "cache", "cache_write", region="cn-east",
                    on={
                        "cache_down": node(
                            "cache_dr", action, region=failover_region,
                        )
                    },
                    next=node("async_logger", "async_log", region="cn-east"),
                ),
            ),
            approvals={"order": ["cn-east", "cn-north"]},
        )

    def test_returns_path_and_trigger_conditions_to_remote_replica(self):
        result = run_config(self._pipeline("cn-north"))
        report = result.reports["order"]

        self.assertEqual(len(report.cross_region_landings), 1)
        landing = report.cross_region_landings[0]
        self.assertEqual(landing.node_id, "cache_dr")
        self.assertEqual(landing.region, "cn-north")
        # 从数据入口到副本的完整节点路径。
        self.assertEqual(landing.path, ["entry", "cache", "cache_dr"])
        # 触发条件链。
        self.assertEqual(landing.conditions, ["cache[cache_down]"])
        self.assertTrue(landing.approved)

    def test_approved_failover_landing_is_warning_not_error(self):
        result = run_config(self._pipeline("cn-north"))
        self.assertTrue(result.passed)
        codes = {f.code for f in result.findings}
        self.assertIn("APPROVED_FAILOVER_LANDING", codes)
        self.assertNotIn("UNAPPROVED_PERSISTENCE", codes)

    def test_unapproved_failover_landing_is_error_but_path_still_returned(self):
        result = run_config(self._pipeline("eu-central"))
        report = result.reports["order"]

        self.assertFalse(result.passed)
        finding = next(
            f for f in result.findings if f.code == "UNAPPROVED_PERSISTENCE"
        )
        self.assertEqual(finding.severity, "error")
        self.assertEqual(finding.node_id, "cache_dr")
        # 即使未核准，入口到副本的路径与触发条件仍要返回。
        landing = report.cross_region_landings[0]
        self.assertFalse(landing.approved)
        self.assertEqual(landing.path, ["entry", "cache", "cache_dr"])
        self.assertEqual(landing.conditions, ["cache[cache_down]"])

    def test_forward_node_on_failover_recorded_separately_without_approval_check(self):
        raw = base_config(
            node(
                "entry", "forward", region="cn-east",
                next=node(
                    "cache", "cache_write", region="cn-east",
                    on={
                        "cache_down": node(
                            "cache_dr", "cache_write", region="cn-north",
                            next=node("relay", "forward", region="us-west"),
                        )
                    },
                ),
            ),
            approvals={"order": ["cn-east", "cn-north"]},
        )
        report = run_config(raw).reports["order"]

        relay = next(r for r in report.transit_records if r.node_id == "relay")
        self.assertTrue(relay.on_failover)
        self.assertEqual(relay.region, "us-west")
        # 转发节点不产生未核准持久化错误，也不进入可持久化区域。
        self.assertNotIn("us-west", report.persistable_regions)
        self.assertIn("us-west", report.transitable_regions)
        self.assertIn("us-west", report.transit_only_regions)

    def test_nested_failover_accumulates_trigger_chain(self):
        raw = base_config(
            node(
                "entry", "forward", region="cn-east",
                next=node(
                    "cache", "cache_write", region="cn-east",
                    on={
                        "cache_down": node(
                            "cache_dr", "cache_write", region="cn-north",
                            on={
                                "dr_full": node(
                                    "cache_dr2", "cache_write",
                                    region="eu-central",
                                )
                            },
                        )
                    },
                ),
            ),
            approvals={"order": ["cn-east", "cn-north"]},
        )
        report = run_config(raw).reports["order"]
        deep = next(
            landing
            for landing in report.cross_region_landings
            if landing.node_id == "cache_dr2"
        )
        self.assertEqual(
            deep.path, ["entry", "cache", "cache_dr", "cache_dr2"]
        )
        self.assertEqual(
            deep.conditions,
            ["cache[cache_down]", "cache_dr[dr_full]"],
        )
        self.assertFalse(deep.approved)

    def test_main_route_remote_persistence_disables_special_landing_record(self):
        # 主路径本身就在异地落地：不属于题面“主路径一直在本区”的场景，
        # 不产生 cross_region_landings 记录，但仍有 finding 说明。
        raw = base_config(
            node(
                "entry", "forward", region="cn-east",
                next=node(
                    "cache", "cache_write", region="cn-north",
                    on={
                        "cache_down": node(
                            "cache_dr", "cache_write", region="eu-central"
                        )
                    },
                ),
            ),
            approvals={"order": ["cn-east", "cn-north"]},
        )
        report = run_config(raw).reports["order"]
        self.assertEqual(report.cross_region_landings, [])
        codes = {f.code for f in report.findings}
        self.assertIn("MAIN_ROUTE_OUTSIDE_HOME", codes)
        self.assertIn("UNAPPROVED_PERSISTENCE", codes)


if __name__ == "__main__":
    unittest.main()
