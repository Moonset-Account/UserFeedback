"""配置加载与结构校验的拒绝路径验收。"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compat_audit import ConfigError, load_config
from compat_audit.config import parse_config

VALID = {
    "versions": [{"name": "v1", "order": 1}],
    "version_defaults": {"v1": {"unknown_fields": "reject"}},
    "layers": [{"name": "base", "priority": 10}],
    "message_types": [
        {
            "name": "MT",
            "required_unknown_field_policy": "reject",
            "allowed_policies": ["reject", "drop"],
        }
    ],
    "endpoints": [
        {
            "name": "p",
            "role": "producer",
            "min_version": "v1",
            "max_version": "v1",
            "settings": [],
        },
        {
            "name": "c",
            "role": "consumer",
            "min_version": "v1",
            "max_version": "v1",
            "settings": [],
        },
    ],
    "pairs": [{"producer": "p", "consumer": "c", "message_types": ["MT"]}],
}


class ValidationTest(unittest.TestCase):
    def assert_rejected(self, raw, fragment):
        with self.assertRaises(ConfigError) as ctx:
            parse_config(raw)
        self.assertIn(fragment, str(ctx.exception))

    def test_valid_config_loads(self):
        config = parse_config(copy.deepcopy(VALID))
        self.assertEqual([v.name for v in config.versions], ["v1"])
        self.assertEqual(len(config.pairs), 1)

    def test_duplicate_version_name_rejected(self):
        raw = copy.deepcopy(VALID)
        raw["versions"].append({"name": "v1", "order": 2})
        self.assert_rejected(raw, "版本名重复")

    def test_unknown_layer_reference_rejected(self):
        raw = copy.deepcopy(VALID)
        raw["endpoints"][0]["settings"].append(
            {
                "layer": "ghost",
                "version": None,
                "message_type": None,
                "values": {"unknown_fields": "reject"},
            }
        )
        self.assert_rejected(raw, "未定义配置层")

    def test_invalid_enum_value_rejected(self):
        raw = copy.deepcopy(VALID)
        raw["endpoints"][0]["settings"].append(
            {
                "layer": "base",
                "version": None,
                "message_type": None,
                "values": {"unknown_fields": "explode"},
            }
        )
        self.assert_rejected(raw, "unknown_fields")

    def test_wrong_type_for_boolean_option_rejected(self):
        raw = copy.deepcopy(VALID)
        raw["endpoints"][0]["settings"].append(
            {
                "layer": "base",
                "version": None,
                "message_type": None,
                "values": {"extra_validation": "false"},
            }
        )
        self.assert_rejected(raw, "extra_validation")

    def test_unknown_option_key_rejected(self):
        raw = copy.deepcopy(VALID)
        raw["endpoints"][0]["settings"].append(
            {
                "layer": "base",
                "version": None,
                "message_type": None,
                "values": {"mystery_option": True},
            }
        )
        self.assert_rejected(raw, "未知选项")

    def test_inverted_version_range_rejected(self):
        raw = copy.deepcopy(VALID)
        raw["versions"].append({"name": "v2", "order": 2})
        raw["endpoints"][0]["min_version"] = "v2"
        raw["endpoints"][0]["max_version"] = "v1"
        self.assert_rejected(raw, "min_version 高于 max_version")

    def test_required_policy_outside_allowed_set_rejected(self):
        raw = copy.deepcopy(VALID)
        raw["message_types"][0]["required_unknown_field_policy"] = "preserve"
        self.assert_rejected(raw, "强制策略")

    def test_role_mismatch_in_pair_rejected(self):
        raw = copy.deepcopy(VALID)
        raw["pairs"][0]["producer"] = "c"  # c 的角色是 consumer
        self.assert_rejected(raw, "角色不是 producer")

    def test_duplicate_pair_rejected(self):
        raw = copy.deepcopy(VALID)
        raw["pairs"].append(
            {"producer": "p", "consumer": "c", "message_types": ["MT"]}
        )
        self.assert_rejected(raw, "重复组合")

    def test_null_values_produce_no_defined_settings(self):
        # 全 null 条目应被静默剔除，而不是报错或污染解析
        raw = copy.deepcopy(VALID)
        raw["endpoints"][0]["settings"].append(
            {
                "layer": "base",
                "version": None,
                "message_type": None,
                "values": {"unknown_fields": None, "extra_validation": None},
            }
        )
        config = parse_config(raw)
        self.assertEqual(config.endpoints[0].settings, [])

    def test_missing_file_raises_config_family_error_or_oserror(self):
        with self.assertRaises((ConfigError, OSError)):
            load_config("/nonexistent/path/does-not-exist.json")


if __name__ == "__main__":
    unittest.main()
