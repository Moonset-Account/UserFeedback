"""三态语义专项验收：未定义、显式禁用、显式启用必须分开。"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compat_audit import audit_dict

TEMPLATE = {
    "versions": [{"name": "v1", "order": 1}],
    "version_defaults": {},
    "layers": [{"name": "base", "priority": 10}],
    "message_types": [
        {
            "name": "Strict",
            "required_unknown_field_policy": "reject",
            "allowed_policies": ["reject", "drop"],
            "require_extra_validation": True,
        },
        {
            "name": "Loose",
            "required_unknown_field_policy": None,
            "allowed_policies": ["preserve", "reject", "drop"],
        },
    ],
    "endpoints": [
        {
            "name": "p",
            "role": "producer",
            "min_version": "v1",
            "max_version": "v1",
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
        {
            "name": "c",
            "role": "consumer",
            "min_version": "v1",
            "max_version": "v1",
            "settings": [],
        },
    ],
    "pairs": [{"producer": "p", "consumer": "c", "message_types": ["Strict"]}],
}


def audit_with(consumer_settings, message_type="Strict", version_defaults=None):
    raw = copy.deepcopy(TEMPLATE)
    raw["endpoints"][1]["settings"] = consumer_settings
    raw["pairs"][0]["message_types"] = [message_type]
    if version_defaults is not None:
        raw["version_defaults"] = version_defaults
    return audit_dict(raw)


def consumer_cell(report):
    return report.conclusions[0].cells[0].consumer


class TriStateTest(unittest.TestCase):
    def test_explicit_disable_is_flagged_and_located(self):
        report = audit_with(
            [
                {
                    "layer": "base",
                    "version": None,
                    "message_type": None,
                    "values": {"unknown_fields": "reject", "extra_validation": False},
                }
            ]
        )
        ev = consumer_cell(report)["extra_validation"]
        self.assertFalse(ev.value)
        self.assertTrue(ev.defined)
        self.assertTrue(ev.explicitly_disabled)
        self.assertEqual(ev.source.source_type, "layer")
        self.assertEqual(ev.source.layer, "base")

        codes = {f.reason_code for f in report.conclusions[0].findings}
        self.assertIn("safeguard_explicitly_disabled", codes)
        # 同一选项只能归因为显式禁用，不能同时又报未定义
        safeguard_codes = {
            f.reason_code
            for f in report.conclusions[0].findings
            if f.option == "extra_validation"
        }
        self.assertEqual(safeguard_codes, {"safeguard_explicitly_disabled"})
        self.assertEqual(report.conclusions[0].status, "blocked")

    def test_undefined_is_distinct_from_explicit_disable(self):
        # 无任何层设置、无版本默认：走内建兜底，defined=False，不允许标成显式禁用
        report = audit_with([])
        ev = consumer_cell(report)["extra_validation"]
        self.assertFalse(ev.value)
        self.assertFalse(ev.defined)
        self.assertFalse(ev.explicitly_disabled)
        self.assertEqual(ev.source.source_type, "builtin_default")

        codes = {f.reason_code for f in report.conclusions[0].findings}
        self.assertIn("safeguard_undefined", codes)
        self.assertNotIn("safeguard_explicitly_disabled", codes)

    def test_null_is_undefined_even_when_key_present(self):
        report = audit_with(
            [
                {
                    "layer": "base",
                    "version": None,
                    "message_type": None,
                    "values": {"extra_validation": None},
                }
            ]
        )
        ev = consumer_cell(report)["extra_validation"]
        self.assertFalse(ev.defined)
        self.assertFalse(ev.explicitly_disabled)
        self.assertEqual(ev.source.source_type, "builtin_default")

    def test_version_default_false_is_defined_but_not_explicit_layer_disable(self):
        # 版本默认给 false：defined=True（不是“未定义”），但显式禁用标记只属于配置层
        report = audit_with(
            [],
            version_defaults={"v1": {"extra_validation": False}},
        )
        ev = consumer_cell(report)["extra_validation"]
        self.assertFalse(ev.value)
        self.assertTrue(ev.defined)
        self.assertFalse(ev.explicitly_disabled)
        self.assertEqual(ev.source.source_type, "version_default")

        codes = {f.reason_code for f in report.conclusions[0].findings}
        self.assertNotIn("safeguard_explicitly_disabled", codes)
        self.assertIn("safeguard_undefined", codes)

    def test_explicit_enable_passes(self):
        report = audit_with(
            [
                {
                    "layer": "base",
                    "version": None,
                    "message_type": None,
                    "values": {"unknown_fields": "reject", "extra_validation": True},
                }
            ]
        )
        self.assertEqual(report.conclusions[0].status, "compatible")
        ev = consumer_cell(report)["extra_validation"]
        self.assertTrue(ev.value)
        self.assertFalse(ev.explicitly_disabled)

    def test_undefined_preserve_on_loose_message_type_is_allowed(self):
        # 未定义不必然阻断：Loose 消息类型允许 preserve 时，内建兜底 preserve 可通过
        report = audit_with([], message_type="Loose")
        self.assertEqual(report.conclusions[0].status, "compatible")
        uf = consumer_cell(report)["unknown_fields"]
        self.assertEqual(uf.value, "preserve")
        self.assertFalse(uf.defined)


if __name__ == "__main__":
    unittest.main()
