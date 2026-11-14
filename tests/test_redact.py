"""脱敏规则测试：显式标记与默认模式命中、嵌套脱敏、原对象不被修改。"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from errmap import REDACTED, sanitize, collect_secret_paths  # noqa: E402


class TestExplicitSecretFields(unittest.TestCase):
    def test_dotted_path_field_redacted(self):
        err = {
            "code": "X",
            "env": {"DB_PASSWORD": "s3cret", "APP_ENV": "prod"},
            "secret_fields": ["env.DB_PASSWORD"],
        }
        out = sanitize(err)
        self.assertEqual(out["env"]["DB_PASSWORD"], REDACTED)
        self.assertEqual(out["env"]["APP_ENV"], "prod")

    def test_bare_key_field_redacted(self):
        err = {
            "code": "X",
            "detail": {"card_number": "4111111111111111"},
            "secret_fields": ["card_number"],
        }
        out = sanitize(err)
        self.assertEqual(out["detail"]["card_number"], REDACTED)

    def test_secret_marker_in_nested_cause_applies(self):
        err = {
            "code": "A",
            "cause": {
                "code": "B",
                "env": {"API_TOKEN": "tok-1"},
                "secret_fields": ["env.API_TOKEN"],
            },
        }
        out = sanitize(err)
        self.assertEqual(out["cause"]["env"]["API_TOKEN"], REDACTED)


class TestDefaultSecretPatterns(unittest.TestCase):
    def test_default_pattern_key_redacted_without_marking(self):
        err = {"code": "X", "env": {"db_password": "p", "region": "cn-1"}}
        out = sanitize(err)
        self.assertEqual(out["env"]["db_password"], REDACTED)
        self.assertEqual(out["env"]["region"], "cn-1")

    def test_pattern_match_is_case_insensitive(self):
        err = {"code": "X", "env": {"Gateway_Api_Token": "t"}}
        out = sanitize(err)
        self.assertEqual(out["env"]["Gateway_Api_Token"], REDACTED)

    def test_secret_fields_metadata_kept_for_diagnostics(self):
        err = {
            "code": "X",
            "env": {"DB_PASSWORD": "s3cret"},
            "secret_fields": ["env.DB_PASSWORD"],
        }
        out = sanitize(err)
        # 标记列表本身只是字段名元数据，应保留以便支持人员核对脱敏范围
        self.assertEqual(out["secret_fields"], ["env.DB_PASSWORD"])


class TestSanitizeSafety(unittest.TestCase):
    def test_original_object_not_mutated(self):
        err = {
            "code": "X",
            "env": {"DB_PASSWORD": "s3cret"},
            "secret_fields": ["env.DB_PASSWORD"],
        }
        snapshot = copy.deepcopy(err)
        sanitize(err)
        self.assertEqual(err, snapshot)

    def test_collect_secret_paths_returns_keys_and_paths(self):
        err = {"env": {"DB_PASSWORD": "x"}, "secret_fields": ["env.DB_PASSWORD"]}
        keys, paths = collect_secret_paths(err)
        self.assertIn("DB_PASSWORD", keys)
        self.assertIn("env.DB_PASSWORD", paths)


if __name__ == "__main__":
    unittest.main()
