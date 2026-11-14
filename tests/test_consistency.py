"""SEC-VULN-006 单元测试：规范化一致性检查。"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import (  # noqa: E402
    RuleError,
    RuleSet,
    canonicalize,
    check_record,
    check_records,
    ruleset_from_dict,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_rules(**overrides):
    """构造一套默认规则，按需覆盖字段。"""
    conf = {
        "name": "rules",
        "version": "v1",
        "tenant_aliases": {
            "tenant-a-legacy": "tenant-a",
            "tenant-b-legacy": "tenant-b",
        },
        "known_tenants": ("tenant-a", "tenant-b", "tenant-c"),
        "duplicate_version": "reject",
        "missing_version": "reject",
        "ignored_query_keys": ("trace_id", "utm_*"),
    }
    conf.update(overrides)
    return RuleSet(**conf)


def make_record(**overrides):
    rec = {
        "record_no": "r-x",
        "tenant": "tenant-a",
        "object_no": "OBJ-1",
        "version": "v1",
        "query": {},
    }
    rec.update(overrides)
    return rec


class LegacyAliasTest(unittest.TestCase):
    """兼容的旧别名：两套规则都识别旧别名时应通过。"""

    def test_legacy_alias_resolves_on_both_sides(self):
        rules_a = make_rules(name="sig", version="sig-1.2")
        rules_b = make_rules(name="loc", version="loc-2.0")
        rec = make_record(tenant="tenant-a-legacy")
        result = check_record(rec, rules_a, rules_b)
        self.assertEqual("pass", result["status"])
        self.assertEqual([], result["divergent_fields"])
        canon = canonicalize(rules_a, rec)
        self.assertEqual("tenant-a", canon["tenant"])

    def test_alias_only_known_to_one_ruleset_diverges(self):
        rules_a = make_rules(
            name="sig",
            version="sig-1.2",
            tenant_aliases={
                "tenant-a-legacy": "tenant-a",
                "tenant-c-legacy": "tenant-c",
            },
        )
        rules_b = make_rules(name="loc", version="loc-2.0")
        rec = make_record(tenant="tenant-c-legacy")
        result = check_record(rec, rules_a, rules_b)
        self.assertEqual("divergent", result["status"])
        self.assertEqual(["tenant"], result["divergent_fields"])

    def test_unresolved_alias_rejected_by_both_is_not_divergence(self):
        rules_a = make_rules(name="sig", version="s1")
        rules_b = make_rules(name="loc", version="l1")
        rec = make_record(tenant="ghost-tenant")
        result = check_record(rec, rules_a, rules_b)
        self.assertEqual("both_rejected", result["status"])
        self.assertIn("unresolved tenant alias", result["errors"]["sig"])
        self.assertIn("unresolved tenant alias", result["errors"]["loc"])


class MissingVersionTest(unittest.TestCase):
    """对象版本丢失：一套规则给默认版本、另一套拒绝时产生 version 分歧。"""

    def test_missing_version_default_vs_reject(self):
        rules_a = make_rules(
            name="sig", version="s1", missing_version="default", default_version="0"
        )
        rules_b = make_rules(name="loc", version="l1", missing_version="reject")
        rec = make_record(version=None)
        result = check_record(rec, rules_a, rules_b)
        self.assertEqual("divergent", result["status"])
        self.assertEqual(["version"], result["divergent_fields"])

    def test_missing_version_field_absent_entirely(self):
        rules_a = make_rules(
            name="sig", version="s1", missing_version="default", default_version="0"
        )
        rules_b = make_rules(name="loc", version="l1", missing_version="reject")
        rec = make_record()
        del rec["version"]
        result = check_record(rec, rules_a, rules_b)
        self.assertEqual("divergent", result["status"])
        self.assertEqual(["version"], result["divergent_fields"])

    def test_missing_version_both_default_passes(self):
        rules_a = make_rules(
            name="sig", version="s1", missing_version="default", default_version="0"
        )
        rules_b = make_rules(
            name="loc", version="l1", missing_version="default", default_version="0"
        )
        result = check_record(make_record(version=None), rules_a, rules_b)
        self.assertEqual("pass", result["status"])


class DuplicateVersionTest(unittest.TestCase):
    """重复版本字段：first/last/reject 三种处理结果。"""

    def test_first_vs_last_diverges(self):
        rules_a = make_rules(name="sig", version="s1", duplicate_version="first")
        rules_b = make_rules(name="loc", version="l1", duplicate_version="last")
        rec = make_record(version=["v1", "v2"])
        result = check_record(rec, rules_a, rules_b)
        self.assertEqual("divergent", result["status"])
        self.assertEqual(["version"], result["divergent_fields"])

    def test_same_policy_passes(self):
        rules_a = make_rules(name="sig", version="s1", duplicate_version="first")
        rules_b = make_rules(name="loc", version="l1", duplicate_version="first")
        result = check_record(make_record(version=["v1", "v2"]), rules_a, rules_b)
        self.assertEqual("pass", result["status"])

    def test_reject_policy_raises_rule_error(self):
        rules = make_rules(duplicate_version="reject")
        with self.assertRaises(RuleError) as ctx:
            canonicalize(rules, make_record(version=["v1", "v2"]))
        self.assertEqual("version", ctx.exception.field_name)

    def test_identical_duplicates_pass_even_with_first_vs_last(self):
        rules_a = make_rules(name="sig", version="s1", duplicate_version="first")
        rules_b = make_rules(name="loc", version="l1", duplicate_version="last")
        result = check_record(make_record(version=["v1", "v1"]), rules_a, rules_b)
        self.assertEqual("pass", result["status"])


class TrackingFieldTest(unittest.TestCase):
    """可忽略的追踪字段：被忽略的查询键不参与比对。"""

    def test_ignored_tracking_fields_do_not_diverge(self):
        rules_a = make_rules(name="sig", version="s1")
        rules_b = make_rules(name="loc", version="l1")
        rec = make_record(query={"trace_id": "abc", "utm_source": "mail", "keep": "1"})
        result = check_record(rec, rules_a, rules_b)
        self.assertEqual("pass", result["status"])
        canon = canonicalize(rules_a, rec)
        self.assertEqual({"keep": "1"}, canon["query"])

    def test_ruleset_ignoring_fewer_keys_diverges_on_query(self):
        rules_a = make_rules(name="sig", version="s1")
        rules_b = make_rules(
            name="loc", version="l1", ignored_query_keys=("trace_id",)
        )
        rec = make_record(query={"utm_source": "mail"})
        result = check_record(rec, rules_a, rules_b)
        self.assertEqual("divergent", result["status"])
        self.assertEqual(["query"], result["divergent_fields"])

    def test_preserved_query_items_are_compared(self):
        rules_a = make_rules(name="sig", version="s1")
        rules_b = make_rules(name="loc", version="l1")
        rec_a = make_record(record_no="r-1", query={"keep": "1"})
        rec_b = make_record(record_no="r-2", query={"keep": "2"})
        # 同一条记录内两套规则一致 -> pass；不同保留值属于不同记录，各自通过。
        self.assertEqual("pass", check_record(rec_a, rules_a, rules_b)["status"])
        self.assertEqual("pass", check_record(rec_b, rules_a, rules_b)["status"])
        self.assertNotEqual(
            canonicalize(rules_a, rec_a)["query"],
            canonicalize(rules_a, rec_b)["query"],
        )


class CrossTenantTest(unittest.TestCase):
    """同编号跨租户：对象编号相同但租户不同的记录各自独立判定。"""

    def test_same_object_number_different_tenants(self):
        rules_a = make_rules(name="sig", version="s1")
        rules_b = make_rules(name="loc", version="l1")
        records = [
            make_record(record_no="r-a", tenant="tenant-a", object_no="OBJ-1"),
            make_record(record_no="r-b", tenant="tenant-b", object_no="OBJ-1"),
        ]
        outcome = check_records(records, rules_a, rules_b)
        self.assertEqual([], outcome["report"])
        canons = [
            canonicalize(rules_a, rec) for rec in records
        ]
        self.assertEqual(canons[0]["object_no"], canons[1]["object_no"])
        self.assertNotEqual(canons[0]["tenant"], canons[1]["tenant"])

    def test_tenant_mismatch_between_rulesets_is_caught(self):
        rules_a = make_rules(name="sig", version="s1")
        # 另一套规则把同一别名解析到不同租户 -> 编号相同也判定分歧。
        rules_b = make_rules(
            name="loc",
            version="l1",
            tenant_aliases={"alias-x": "tenant-b"},
        )
        rec = make_record(tenant="alias-x")
        # rules_a 不认识 alias-x -> 未解析别名；分歧字段为 tenant。
        result = check_record(rec, rules_a, rules_b)
        self.assertEqual("divergent", result["status"])
        self.assertEqual(["tenant"], result["divergent_fields"])


class InputOrderTest(unittest.TestCase):
    """输入顺序变化：报告按记录编号排序，与输入顺序无关。"""

    def test_report_is_order_invariant(self):
        rules_a = make_rules(
            name="sig", version="s1", missing_version="default", default_version="0"
        )
        rules_b = make_rules(name="loc", version="l1", missing_version="reject")
        records = [
            make_record(record_no="r-1", version=None),
            make_record(record_no="r-2"),
            make_record(record_no="r-3", version=None),
        ]
        forward = check_records(records, rules_a, rules_b)["report"]
        reversed_ = check_records(list(reversed(records)), rules_a, rules_b)["report"]
        self.assertEqual(forward, reversed_)
        self.assertEqual(["r-1", "r-3"], [e["record_no"] for e in forward])

    def test_per_record_status_independent_of_position(self):
        rules_a = make_rules(name="sig", version="s1", duplicate_version="first")
        rules_b = make_rules(name="loc", version="l1", duplicate_version="last")
        rec = make_record(record_no="r-dup", version=["v1", "v2"])
        alone = check_record(rec, rules_a, rules_b)
        among = check_records(
            [make_record(record_no="r-0"), rec, make_record(record_no="r-9")],
            rules_a,
            rules_b,
        )["results"][1]
        self.assertEqual(alone["status"], among["status"])
        self.assertEqual(alone["divergent_fields"], among["divergent_fields"])


class ReportShapeTest(unittest.TestCase):
    """报告只包含记录编号、两套规则版本和分歧字段名。"""

    def test_report_entry_keys(self):
        rules_a = make_rules(
            name="sig", version="sig-1.2", missing_version="default"
        )
        rules_b = make_rules(name="loc", version="loc-2.0", missing_version="reject")
        outcome = check_records([make_record(version=None)], rules_a, rules_b)
        self.assertEqual(1, len(outcome["report"]))
        entry = outcome["report"][0]
        self.assertEqual(
            {"record_no", "rule_versions", "divergent_fields"}, set(entry.keys())
        )
        self.assertEqual(["sig-1.2", "loc-2.0"], entry["rule_versions"])
        self.assertEqual(["version"], entry["divergent_fields"])


class SampleEndToEndTest(unittest.TestCase):
    """交付样例的端到端断言（与 tools/local_verify.py 一致）。"""

    def test_sample_report(self):
        with open(os.path.join(BASE_DIR, "samples", "records.json"), encoding="utf-8") as fh:
            records = json.load(fh)
        with open(os.path.join(BASE_DIR, "samples", "rules.json"), encoding="utf-8") as fh:
            conf = json.load(fh)
        rules_a = ruleset_from_dict(conf["rules_a"])
        rules_b = ruleset_from_dict(conf["rules_b"])
        report = check_records(records, rules_a, rules_b)["report"]
        self.assertEqual(
            [
                {
                    "record_no": "r-002",
                    "rule_versions": ["sig-1.2", "loc-2.0"],
                    "divergent_fields": ["version"],
                },
                {
                    "record_no": "r-005",
                    "rule_versions": ["sig-1.2", "loc-2.0"],
                    "divergent_fields": ["version"],
                },
                {
                    "record_no": "r-007",
                    "rule_versions": ["sig-1.2", "loc-2.0"],
                    "divergent_fields": ["tenant"],
                },
            ],
            report,
        )


if __name__ == "__main__":
    unittest.main()
