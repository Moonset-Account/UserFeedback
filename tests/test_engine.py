"""分流规则生成与试算查询的单元测试。"""

import unittest

from splitdns.config import validate_config
from splitdns.engine import Engine, UnknownBranchError


def make_engine(**overrides):
    raw = {
        "branches": [{"id": "bj-01"}, {"id": "sh-02"}],
        "resolvers": [
            {"id": "internal-corp", "type": "internal",
             "address": "10.1.0.53", "enabled": True},
            {"id": "internal-bj", "type": "internal",
             "address": "10.2.0.53", "enabled": True},
            {"id": "internal-legacy", "type": "internal",
             "address": "10.9.0.53", "enabled": False},
            {"id": "public-default", "type": "public",
             "address": "203.0.113.53", "enabled": True},
        ],
        "suffix_rules": [
            {"suffix": "corp.example.com", "resolver": "internal-corp"},
            {"suffix": "corp.example.com", "resolver": "internal-bj",
             "branch": "bj-01"},
            {"suffix": "hr.corp.example.com", "resolver": "internal-corp"},
            {"suffix": "legacy.example.com", "resolver": "internal-legacy"},
            {"suffix": "内部.example", "resolver": "internal-corp"},
        ],
        "default_resolver": "public-default",
    }
    raw.update(overrides)
    return Engine(validate_config(raw))


class TestGenerate(unittest.TestCase):
    def test_generate_covers_all_branches(self):
        engine = make_engine()
        out = engine.generate()
        self.assertEqual(set(out), {"bj-01", "sh-02"})

    def test_branch_override_replaces_global_same_suffix(self):
        engine = make_engine()
        rules = {r["suffix"]: r for r in engine.effective_rules("bj-01")}
        self.assertEqual(rules["corp.example.com"]["resolver"], "internal-bj")
        self.assertEqual(rules["corp.example.com"]["source"], "branch")

    def test_other_branch_keeps_global(self):
        engine = make_engine()
        rules = {r["suffix"]: r for r in engine.effective_rules("sh-02")}
        self.assertEqual(rules["corp.example.com"]["resolver"],
                         "internal-corp")
        self.assertEqual(rules["corp.example.com"]["source"], "global")

    def test_rules_sorted_longest_first(self):
        engine = make_engine()
        rules = engine.effective_rules("bj-01")
        counts = [r["label_count"] for r in rules]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_unknown_branch_rejected(self):
        engine = make_engine()
        with self.assertRaises(UnknownBranchError):
            engine.effective_rules("gz-99")


class TestQuery(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()

    def test_branch_override_wins_for_same_suffix(self):
        res = self.engine.query("bj-01", "www.corp.example.com")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["resolver"]["id"], "internal-bj")
        self.assertEqual(res["rule_source"], "branch")

    def test_longest_suffix_wins(self):
        res = self.engine.query("bj-01", "app.hr.corp.example.com")
        self.assertEqual(res["matched_suffix"], "hr.corp.example.com")
        self.assertEqual(res["resolver"]["id"], "internal-corp")

    def test_unrelated_suffix_goes_default_egress(self):
        res = self.engine.query("bj-01", "www.public-site.net")
        self.assertEqual(res["status"], "ok")
        self.assertIsNone(res["matched_suffix"])
        self.assertEqual(res["resolver"]["id"], "public-default")
        self.assertTrue(res["forwarded_to_public"])

    def test_disabled_internal_resolver_fails_without_public_fallback(self):
        res = self.engine.query("sh-02", "db.legacy.example.com")
        self.assertEqual(res["status"], "internal_failure")
        self.assertEqual(res["resolver"]["id"], "internal-legacy")
        self.assertFalse(res["forwarded_to_public"])

    def test_trailing_dot_and_case(self):
        res = self.engine.query("bj-01", "WWW.Corp.Example.Com.")
        self.assertEqual(res["normalized"], "www.corp.example.com")
        self.assertEqual(res["resolver"]["id"], "internal-bj")

    def test_idn_punycode_matches_unicode_suffix(self):
        puny = "内部".encode("idna").decode("ascii")
        res = self.engine.query("sh-02", "mail.%s.example" % puny)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["matched_suffix"], "内部.example")
        self.assertEqual(res["normalized"], "mail.内部.example")

    def test_idn_unicode_query(self):
        res = self.engine.query("sh-02", "mail.内部.example")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["resolver"]["id"], "internal-corp")

    def test_unknown_branch_rejected(self):
        with self.assertRaises(UnknownBranchError):
            self.engine.query("gz-99", "www.corp.example.com")

    def test_no_default_resolver_configured(self):
        engine = make_engine(default_resolver=None)
        res = engine.query("bj-01", "www.public-site.net")
        self.assertEqual(res["status"], "no_match")
        self.assertFalse(res["forwarded_to_public"])

    def test_disabled_default_resolver(self):
        engine = make_engine()
        engine._resolvers["public-default"]["enabled"] = False
        res = engine.query("bj-01", "www.public-site.net")
        self.assertEqual(res["status"], "resolver_unavailable")
        self.assertFalse(res["forwarded_to_public"])


if __name__ == "__main__":
    unittest.main()
