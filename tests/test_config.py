"""配置加载与校验的单元测试。"""

import unittest

from splitdns.config import ConfigError, validate_config


def base_config():
    return {
        "branches": [{"id": "bj-01"}, {"id": "sh-02"}],
        "resolvers": [
            {"id": "internal-corp", "type": "internal",
             "address": "10.1.0.53", "enabled": True},
            {"id": "public-default", "type": "public",
             "address": "203.0.113.53", "enabled": True},
        ],
        "suffix_rules": [
            {"suffix": "corp.example.com", "resolver": "internal-corp"},
        ],
        "default_resolver": "public-default",
    }


class TestValidateConfig(unittest.TestCase):
    def test_valid_config_normalizes_suffix(self):
        cfg = validate_config(base_config())
        self.assertEqual(cfg["suffix_rules"][0]["suffix"], "corp.example.com")
        self.assertEqual(cfg["default_resolver"], "public-default")
        self.assertEqual({b["id"] for b in cfg["branches"]},
                         {"bj-01", "sh-02"})

    def test_suffix_normalized_on_load(self):
        raw = base_config()
        raw["suffix_rules"] = [
            {"suffix": "CORP.Example.Com.", "resolver": "internal-corp"}]
        cfg = validate_config(raw)
        self.assertEqual(cfg["suffix_rules"][0]["suffix"], "corp.example.com")

    def test_empty_branches_rejected(self):
        raw = base_config()
        raw["branches"] = []
        with self.assertRaises(ConfigError):
            validate_config(raw)

    def test_duplicate_branch_rejected(self):
        raw = base_config()
        raw["branches"].append({"id": "bj-01"})
        with self.assertRaises(ConfigError):
            validate_config(raw)

    def test_unknown_resolver_in_rule_rejected(self):
        raw = base_config()
        raw["suffix_rules"] = [
            {"suffix": "a.example.com", "resolver": "no-such"}]
        with self.assertRaises(ConfigError):
            validate_config(raw)

    def test_unknown_branch_in_rule_rejected(self):
        raw = base_config()
        raw["suffix_rules"] = [
            {"suffix": "a.example.com", "resolver": "internal-corp",
             "branch": "gz-99"}]
        with self.assertRaises(ConfigError):
            validate_config(raw)

    def test_unknown_default_resolver_rejected(self):
        raw = base_config()
        raw["default_resolver"] = "no-such"
        with self.assertRaises(ConfigError):
            validate_config(raw)

    def test_bad_resolver_type_rejected(self):
        raw = base_config()
        raw["resolvers"][0]["type"] = "recursive"
        with self.assertRaises(ConfigError):
            validate_config(raw)

    def test_default_resolver_optional(self):
        raw = base_config()
        del raw["default_resolver"]
        cfg = validate_config(raw)
        self.assertIsNone(cfg["default_resolver"])


if __name__ == "__main__":
    unittest.main()
