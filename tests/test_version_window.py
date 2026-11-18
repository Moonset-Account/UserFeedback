"""版本范围边界与升级窗口交错的专项验收。"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compat_audit import audit_dict

TEMPLATE = {
    "versions": [
        {"name": "v1", "order": 1},
        {"name": "v2", "order": 2},
        {"name": "v3", "order": 3},
        {"name": "v4", "order": 4},
    ],
    "version_defaults": {
        "v1": {"unknown_fields": "reject"},
        "v2": {"unknown_fields": "reject"},
        "v3": {"unknown_fields": "drop"},
        "v4": {"unknown_fields": "drop"},
    },
    "layers": [{"name": "base", "priority": 10}],
    "message_types": [
        {
            "name": "MT",
            "required_unknown_field_policy": "reject",
            "allowed_policies": ["reject", "drop"],
        }
    ],
    "endpoints": [],
    "pairs": [{"producer": "p", "consumer": "c", "message_types": ["MT"]}],
}


def make_endpoint(name, role, lo, hi):
    return {
        "name": name,
        "role": role,
        "min_version": lo,
        "max_version": hi,
        "settings": [],
    }


def audit_ranges(producer_range, consumer_range):
    raw = copy.deepcopy(TEMPLATE)
    raw["endpoints"] = [
        make_endpoint("p", "producer", *producer_range),
        make_endpoint("c", "consumer", *consumer_range),
    ]
    return audit_dict(raw).conclusions[0]


class OverlapWindowTest(unittest.TestCase):
    def test_single_shared_version_boundary(self):
        # 边界：两端范围只在一个版本上相接；该版本默认 reject，恰好满足强制策略
        conclusion = audit_ranges(("v1", "v2"), ("v2", "v4"))
        self.assertEqual(conclusion.status, "compatible")
        self.assertEqual(conclusion.overlap_versions, ["v2"])
        self.assertEqual(len(conclusion.cells), 1)

    def test_single_shared_version_boundary_with_blocking_policy(self):
        # 边界交错：唯一相接版本上的默认策略不满足强制要求，整对仍须阻断
        conclusion = audit_ranges(("v3", "v4"), ("v1", "v3"))
        self.assertEqual(conclusion.overlap_versions, ["v3"])
        self.assertEqual(conclusion.status, "blocked")
        self.assertEqual(
            {f.reason_code for f in conclusion.findings},
            {"policy_not_allowed"},
        )

    def test_disjoint_ranges_no_overlap(self):
        # 应被拒绝评估的组合：范围完全错开
        conclusion = audit_ranges(("v1", "v1"), ("v3", "v4"))
        self.assertEqual(conclusion.status, "no_overlap")
        self.assertEqual(conclusion.overlap_versions, [])
        self.assertEqual(conclusion.cells, [])

    def test_full_range_overlap_evaluates_every_version(self):
        conclusion = audit_ranges(("v1", "v4"), ("v1", "v4"))
        self.assertEqual(conclusion.overlap_versions, ["v1", "v2", "v3", "v4"])
        # v1/v2 默认 reject 通过；v3/v4 默认 drop 与强制 reject 冲突
        blocked_versions = sorted({f.version for f in conclusion.findings})
        self.assertEqual(blocked_versions, ["v3", "v4"])
        ok_versions = sorted(c.version for c in conclusion.cells if c.ok)
        self.assertEqual(ok_versions, ["v1", "v2"])

    def test_blocking_in_one_window_cell_blocks_whole_pair(self):
        # 交错：同一升级窗口内部分单元通过、部分单元阻断 —— 整对结论必须是 blocked
        conclusion = audit_ranges(("v2", "v3"), ("v2", "v3"))
        self.assertEqual(conclusion.status, "blocked")
        cell_by_version = {c.version: c for c in conclusion.cells}
        self.assertTrue(cell_by_version["v2"].ok)
        self.assertFalse(cell_by_version["v3"].ok)

    def test_version_order_not_lexical(self):
        # 版本顺序以 order 字段为准，而不是文件顺序或字典序
        raw = copy.deepcopy(TEMPLATE)
        raw["versions"] = [
            {"name": "v10", "order": 10},
            {"name": "v2", "order": 2},
            {"name": "v1", "order": 1},
        ]
        raw["version_defaults"] = {
            "v1": {"unknown_fields": "reject"},
            "v2": {"unknown_fields": "reject"},
            "v10": {"unknown_fields": "drop"},
        }
        raw["endpoints"] = [
            make_endpoint("p", "producer", "v1", "v10"),
            make_endpoint("c", "consumer", "v2", "v2"),
        ]
        conclusion = audit_dict(raw).conclusions[0]
        self.assertEqual(conclusion.overlap_versions, ["v2"])
        self.assertEqual(conclusion.status, "compatible")


if __name__ == "__main__":
    unittest.main()
