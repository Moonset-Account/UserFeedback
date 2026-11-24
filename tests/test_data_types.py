"""多数据类型：每种数据独立给出可经过/可持久化区域与独立核准判定。"""

import unittest

try:
    from tests._helpers import base_config, node, run_config
except ImportError:  # python -I 下 cwd 不在 sys.path
    from _helpers import base_config, node, run_config


class MultiDataTypeTest(unittest.TestCase):
    def test_each_data_type_gets_independent_region_sets(self):
        raw = base_config(
            node(
                "entry", "forward", region="cn-east",
                next=node(
                    "cache", "cache_write", region="cn-east",
                    data_types=["order"],
                    on={
                        "cache_down": node(
                            "cache_dr", "cache_write", region="cn-north",
                            data_types=["order"],
                        )
                    },
                    next=node(
                        "audit_log", "async_log", region="cn-east",
                        data_types=["audit"],
                    ),
                ),
            ),
            data_types=["order", "audit"],
            approvals={
                "order": ["cn-east", "cn-north"],
                "audit": ["cn-east"],
            },
        )
        result = run_config(raw)
        order = result.reports["order"]
        audit = result.reports["audit"]

        # order 的缓存回退在 cn-north 落地；audit 不经过该步骤。
        self.assertEqual(order.persistable_regions, ["cn-east", "cn-north"])
        self.assertEqual(audit.persistable_regions, ["cn-east"])
        self.assertNotIn("cn-north", audit.transitable_regions)
        self.assertEqual(len(order.cross_region_landings), 1)
        self.assertEqual(audit.cross_region_landings, [])

        # audit 链路仍经过 entry/cache 两个对全数据生效的转发/作用域节点：
        # cache 限定 order，audit 不适用；entry 是转发且对全数据生效。
        self.assertEqual(audit.transitable_regions, ["cn-east"])

    def test_unapproved_region_for_one_type_does_not_block_other_type(self):
        raw = base_config(
            node(
                "entry", "forward", region="cn-east",
                next=node(
                    "cache", "cache_write", region="cn-east",
                    on={
                        "cache_down": node(
                            "cache_dr", "replicate", region="eu-central"
                        )
                    },
                ),
            ),
            data_types=["order", "audit"],
            approvals={
                "order": ["cn-east", "eu-central"],
                "audit": ["cn-east"],
            },
        )
        result = run_config(raw)
        order = result.reports["order"]
        audit = result.reports["audit"]

        # 同一异地副本：order 已核准（warning），audit 未核准（error）。
        self.assertFalse(
            any(f.severity == "error" for f in order.findings)
        )
        self.assertTrue(
            any(
                f.code == "UNAPPROVED_PERSISTENCE" and f.data_type == "audit"
                for f in audit.findings
            )
        )
        self.assertEqual(
            order.cross_region_landings[0].approved, True
        )
        self.assertEqual(
            audit.cross_region_landings[0].approved, False
        )

    def test_unknown_data_type_on_node_is_rejected(self):
        from region_preflight import PreflightConfigError

        raw = base_config(
            node(
                "entry", "forward", region="cn-east",
                next=node("cache", "cache_write", data_types=["metrics"]),
            )
        )
        with self.assertRaises(PreflightConfigError):
            run_config(raw)

    def test_missing_approval_entry_is_rejected(self):
        from region_preflight import PreflightConfigError

        raw = base_config(
            node("entry", "forward", region="cn-east"),
            data_types=["order", "audit"],
            approvals={"order": ["cn-east"]},  # audit 缺核准清单
        )
        with self.assertRaises(PreflightConfigError):
            run_config(raw)


if __name__ == "__main__":
    unittest.main()
