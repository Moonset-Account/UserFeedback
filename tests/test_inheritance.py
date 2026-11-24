"""地域继承：未显式写 region 的子配置按父配置判定。"""

import unittest


try:
    from tests._helpers import base_config, node, run_config
except ImportError:  # python -I 下 cwd 不在 sys.path
    from _helpers import base_config, node, run_config
from region_preflight import PreflightConfigError, from_dict, load_config


class RegionInheritanceTest(unittest.TestCase):
    def test_nested_children_inherit_parent_region(self):
        # 只有 entry 显式写 region，后续两层全部缺省。
        raw = base_config(
            node(
                "entry", "forward", region="cn-east",
                next=node(
                    "cache", "cache_write",
                    next=node("async_logger", "async_log"),
                ),
            )
        )
        result = run_config(raw)
        # 直接复核解析后的节点生效地域。
        parsed = from_dict(raw)
        cache = parsed.root.next
        logger = cache.next
        self.assertEqual(cache.region, "cn-east")
        self.assertEqual(logger.region, "cn-east")
        self.assertTrue(cache.region_inherited)
        self.assertTrue(logger.region_inherited)
        self.assertFalse(parsed.root.region_inherited)
        self.assertEqual(
            result.reports["order"].persistable_regions, ["cn-east"]
        )
        self.assertTrue(result.passed)

    def test_failover_child_inherits_then_explicit_grandchild_overrides(self):
        raw = base_config(
            node(
                "entry", "forward", region="cn-east",
                next=node(
                    "cache", "cache_write",
                    on={
                        "cache_down": node(
                            "cache_dr", "cache_write",  # 继承 cn-east
                            next=node(
                                "dr_relay", "forward", region="cn-north"
                            ),
                        )
                    },
                ),
            ),
            approvals={"order": ["cn-east", "cn-north"]},
        )
        parsed = from_dict(raw)
        dr = parsed.root.next.failovers["cache_down"]
        self.assertEqual(dr.region, "cn-east")
        self.assertTrue(dr.region_inherited)
        self.assertEqual(dr.next.region, "cn-north")
        self.assertFalse(dr.next.region_inherited)

    def test_root_without_region_falls_back_to_home_region(self):
        raw = base_config(
            node("entry", "forward", next=node("cache", "cache_write"))
        )
        parsed = from_dict(raw)
        self.assertEqual(parsed.root.region, "cn-east")
        self.assertEqual(parsed.root.next.region, "cn-east")

    def test_empty_region_string_is_rejected(self):
        raw = base_config(node("entry", "forward", region=""))
        with self.assertRaises(PreflightConfigError):
            run_config(raw)

    def test_loaded_json_preserves_inheritance_semantics(self):
        path = (
            __import__("pathlib").Path(__file__).resolve().parent
            / "fixtures"
            / "sample_pipeline.json"
        )
        config = load_config(path)
        # async_logger / dr_replica 均未显式写 region，应继承 cn-east。
        cache = config.root.next
        self.assertEqual(cache.next.region, "cn-east")
        self.assertEqual(cache.next.next.region, "cn-east")
        self.assertTrue(cache.next.region_inherited)


if __name__ == "__main__":
    unittest.main()
