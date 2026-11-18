"""解析优先级与作用域交错的单元验收。"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compat_audit import audit_dict
from compat_audit.config import parse_config
from compat_audit.model import OPTION_UNKNOWN_FIELDS
from compat_audit.resolver import resolve_option

BASE_CONFIG = {
    "versions": [
        {"name": "v1", "order": 1},
        {"name": "v2", "order": 2},
    ],
    "version_defaults": {
        "v1": {"unknown_fields": "reject"},
        "v2": {"unknown_fields": "drop"},
    },
    "layers": [
        {"name": "base", "priority": 10},
        {"name": "service", "priority": 20},
        {"name": "global", "priority": 30},
    ],
    "message_types": [
        {
            "name": "MT",
            "required_unknown_field_policy": None,
            "allowed_policies": ["preserve", "reject", "drop"],
        }
    ],
    "endpoints": [
        {
            "name": "ep",
            "role": "consumer",
            "min_version": "v1",
            "max_version": "v2",
            "settings": [],
        },
        {
            "name": "prod",
            "role": "producer",
            "min_version": "v1",
            "max_version": "v2",
            "settings": [
                {
                    "layer": "base",
                    "version": None,
                    "message_type": None,
                    "values": {
                        "unknown_fields": "reject",
                        "extra_validation": True,
                    },
                }
            ],
        },
    ],
    "pairs": [{"producer": "prod", "consumer": "ep", "message_types": ["MT"]}],
}


def build_config(settings):
    raw = copy.deepcopy(BASE_CONFIG)
    raw["endpoints"][0]["settings"] = settings
    return parse_config(raw)


def resolve(settings, version="v2", message_type="MT", option=OPTION_UNKNOWN_FIELDS):
    config = build_config(settings)
    return resolve_option(config, config.endpoints[0], version, message_type, option)


class LayerPrecedenceTest(unittest.TestCase):
    def test_higher_priority_layer_beats_exact_scope_of_lower_layer(self):
        # 低层精确作用域 vs 高层通配 —— 层优先级先于作用域精确度
        result = resolve(
            [
                {
                    "layer": "base",
                    "version": "v2",
                    "message_type": "MT",
                    "values": {"unknown_fields": "reject"},
                },
                {
                    "layer": "global",
                    "version": None,
                    "message_type": None,
                    "values": {"unknown_fields": "preserve"},
                },
            ]
        )
        self.assertEqual(result.value, "preserve")
        self.assertEqual(result.source.layer, "global")
        self.assertEqual(result.source.layer_priority, 30)

    def test_same_layer_exact_scope_beats_wildcard(self):
        result = resolve(
            [
                {
                    "layer": "service",
                    "version": None,
                    "message_type": None,
                    "values": {"unknown_fields": "preserve"},
                },
                {
                    "layer": "service",
                    "version": "v2",
                    "message_type": "MT",
                    "values": {"unknown_fields": "reject"},
                },
            ]
        )
        self.assertEqual(result.value, "reject")
        self.assertEqual(result.source.version_scope, "v2")
        self.assertEqual(result.source.message_type_scope, "MT")

    def test_same_layer_same_scope_later_index_wins(self):
        # 同层同作用域的条目顺序也是确定性的交错裁决规则
        result = resolve(
            [
                {
                    "layer": "base",
                    "version": "v2",
                    "message_type": None,
                    "values": {"unknown_fields": "drop"},
                },
                {
                    "layer": "base",
                    "version": "v2",
                    "message_type": None,
                    "values": {"unknown_fields": "reject"},
                },
            ]
        )
        self.assertEqual(result.value, "reject")
        self.assertEqual(result.source.setting_index, 1)

    def test_non_matching_scopes_are_ignored(self):
        result = resolve(
            [
                {
                    "layer": "global",
                    "version": "v1",  # 查的是 v2，此条不匹配
                    "message_type": "MT",
                    "values": {"unknown_fields": "reject"},
                }
            ],
            version="v2",
        )
        # 回退到 v2 版本默认
        self.assertEqual(result.value, "drop")
        self.assertEqual(result.source.source_type, "version_default")


class FallbackChainTest(unittest.TestCase):
    def test_explicit_layer_value_overrides_version_default(self):
        result = resolve(
            [
                {
                    "layer": "base",
                    "version": None,
                    "message_type": None,
                    "values": {"unknown_fields": "preserve"},
                }
            ],
            version="v1",  # v1 默认是 reject
        )
        self.assertEqual(result.value, "preserve")
        self.assertEqual(result.source.source_type, "layer")
        self.assertTrue(result.defined)

    def test_version_default_used_when_no_layer_defines_option(self):
        result = resolve([], version="v1")
        self.assertEqual(result.value, "reject")
        self.assertEqual(result.source.source_type, "version_default")
        self.assertTrue(result.defined)
        self.assertFalse(result.explicitly_disabled)

    def test_builtin_default_when_everything_undefined(self):
        # v2 版本默认只给了 unknown_fields；查 extra_validation 时全部未定义
        config = build_config([])
        result = resolve_option(
            config, config.endpoints[0], "v2", "MT", "extra_validation"
        )
        self.assertFalse(result.value)
        self.assertFalse(result.defined)
        self.assertFalse(result.explicitly_disabled)
        self.assertEqual(result.source.source_type, "builtin_default")

    def test_null_value_treated_as_undefined_not_explicit_disable(self):
        result = resolve(
            [
                {
                    "layer": "base",
                    "version": None,
                    "message_type": None,
                    "values": {"unknown_fields": None},
                }
            ],
            version="v1",
        )
        # 显式 null 等同于未定义：回退到 v1 版本默认 reject
        self.assertEqual(result.value, "reject")
        self.assertEqual(result.source.source_type, "version_default")


class EndToEndPrecedenceTest(unittest.TestCase):
    def test_high_priority_preserve_blocks_despite_low_priority_reject(self):
        # 交错窗口的整体效果：高层通配 preserve 压过低层精确 reject，
        # 在要求 reject 的消息类型上必须阻断并指认 global 层
        raw = copy.deepcopy(BASE_CONFIG)
        raw["message_types"][0]["required_unknown_field_policy"] = "reject"
        raw["message_types"][0]["allowed_policies"] = ["reject", "drop"]
        # prod 已有 base 层 reject；追加一条会被高层通配压过的精确条目和 global preserve
        raw["endpoints"][1]["settings"] = [
            {
                "layer": "base",
                "version": "v2",
                "message_type": "MT",
                "values": {"unknown_fields": "reject"},
            },
            {
                "layer": "global",
                "version": None,
                "message_type": None,
                "values": {"unknown_fields": "preserve"},
            },
        ]
        report = audit_dict(raw)
        conclusion = report.conclusions[0]
        self.assertEqual(conclusion.status, "blocked")
        v2_cell = next(c for c in conclusion.cells if c.version == "v2")
        self.assertEqual(
            v2_cell.producer["unknown_fields"].source.layer, "global"
        )
        retained = [
            f
            for f in conclusion.findings
            if f.reason_code == "undeclared_fields_retained"
            and f.endpoint == "prod"
            and f.version == "v2"
        ]
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].source.layer, "global")


if __name__ == "__main__":
    unittest.main()
