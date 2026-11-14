"""GraphQL 解析器授权覆盖校验器的单元测试。

覆盖需求点:
  1. 基接口与具体实现权限一致 -> 全部 covered
  2. 某个实现遗漏校验 -> 该实现 missing, 路径级结论 missing
  3. 同名别名指向不同字段 -> 别名映射回实际字段, 命中各自契约
  4. 继承顺序证据缺失 -> manual_review
  5. 修正一条契约后只改变相关结论
  6. 片段循环 / 缺失解析器元数据分别报告
  7. 共享片段在多个根操作中独立求值
"""
import copy
import unittest

from checker import Checker, COVERED, MANUAL_REVIEW, MISSING


def base_schema():
    return {
        "types": {
            "Query": {
                "kind": "object",
                "fields": {
                    "node": {"type": "Node"},
                    "user": {"type": "User"},
                    "org": {"type": "Org"},
                },
            },
            "Node": {
                "kind": "interface",
                "possible_types": ["User", "Org"],
                "fields": {
                    "id": {"type": "ID"},
                    "secret": {"type": "String", "sensitive": True},
                },
            },
            "User": {
                "kind": "object",
                "interfaces": ["Node"],
                "fields": {
                    "id": {"type": "ID"},
                    "secret": {"type": "String", "sensitive": True},
                    "email": {"type": "String", "sensitive": True},
                },
            },
            "Org": {
                "kind": "object",
                "interfaces": ["Node"],
                "fields": {
                    "id": {"type": "ID"},
                    "secret": {"type": "String", "sensitive": True},
                    "leadEmail": {"type": "String", "sensitive": True},
                },
            },
        }
    }


def op_get_node_secret():
    return {
        "operations": [
            {
                "name": "GetNode",
                "root_type": "Query",
                "selections": [
                    {
                        "kind": "field",
                        "name": "node",
                        "selections": [
                            {"kind": "field", "name": "secret"},
                        ],
                    }
                ],
            }
        ],
        "fragments": {},
    }


def findings_by_path(report):
    out = {}
    for group in (MISSING, COVERED, MANUAL_REVIEW):
        for f in report[group]:
            out[f["path"]] = f
    return out


class ConsistentPermissionsTest(unittest.TestCase):
    """基接口与具体实现权限一致: 接口契约 + 两个实现各自契约均有检查。"""

    def test_all_covered(self):
        resolvers = {
            "contracts": {
                "Node.secret": {"checks": ["require_internal"]},
                "User.secret": {"checks": ["require_owner_or_admin"]},
                "Org.secret": {"checks": ["require_org_admin"]},
            },
            "inheritance": [],
        }
        report = Checker(base_schema(), resolvers, op_get_node_secret()).run()
        self.assertEqual(report["summary"][MISSING], 0)
        self.assertEqual(report["summary"][MANUAL_REVIEW], 0)
        self.assertEqual(report["summary"][COVERED], 1)
        finding = report[COVERED][0]
        self.assertEqual(finding["path"], "GetNode.node.secret")
        by_type = {d["type"]: d for d in finding["applicable"]}
        self.assertEqual(by_type["User"]["status"], COVERED)
        self.assertEqual(by_type["Org"]["status"], COVERED)
        self.assertEqual(by_type["User"]["contract"], "User.secret")
        self.assertEqual(by_type["Org"]["contract"], "Org.secret")


class OneImplementationMissingTest(unittest.TestCase):
    """某个实现遗漏校验: Org.secret 契约存在但 checks 为空。"""

    def test_missing_reported_for_that_implementation(self):
        resolvers = {
            "contracts": {
                "User.secret": {"checks": ["require_owner_or_admin"]},
                "Org.secret": {"checks": []},
            },
            "inheritance": [],
        }
        report = Checker(base_schema(), resolvers, op_get_node_secret()).run()
        self.assertEqual(report["summary"][MISSING], 1)
        finding = report[MISSING][0]
        self.assertEqual(finding["path"], "GetNode.node.secret")
        by_type = {d["type"]: d for d in finding["applicable"]}
        self.assertEqual(by_type["User"]["status"], COVERED)
        self.assertEqual(by_type["Org"]["status"], MISSING)
        self.assertEqual(by_type["Org"]["checks"], [])


class SameAliasDifferentFieldsTest(unittest.TestCase):
    """同名别名 contact 在不同位置分别指向 User.email 与 Org.leadEmail。"""

    def test_alias_maps_back_to_actual_field(self):
        document = {
            "operations": [
                {
                    "name": "GetBoth",
                    "root_type": "Query",
                    "selections": [
                        {
                            "kind": "field",
                            "name": "user",
                            "selections": [
                                {"kind": "field", "name": "email", "alias": "contact"},
                            ],
                        },
                        {
                            "kind": "field",
                            "name": "org",
                            "selections": [
                                {"kind": "field", "name": "leadEmail", "alias": "contact"},
                            ],
                        },
                    ],
                }
            ],
            "fragments": {},
        }
        resolvers = {
            "contracts": {
                "User.email": {"checks": ["require_owner_or_admin"]},
                "Org.leadEmail": {"checks": ["require_org_admin"]},
            },
            "inheritance": [],
        }
        report = Checker(base_schema(), resolvers, document).run()
        paths = findings_by_path(report)
        user_f = paths["GetBoth.user.contact"]
        org_f = paths["GetBoth.org.contact"]
        self.assertEqual(user_f["field"], "email")
        self.assertEqual(user_f["alias"], "contact")
        self.assertEqual(user_f["applicable"][0]["contract"], "User.email")
        self.assertEqual(org_f["field"], "leadEmail")
        self.assertEqual(org_f["alias"], "contact")
        self.assertEqual(org_f["applicable"][0]["contract"], "Org.leadEmail")
        self.assertEqual(user_f["status"], COVERED)
        self.assertEqual(org_f["status"], COVERED)


class InheritanceEvidenceTest(unittest.TestCase):
    """继承顺序证据: 有证据 -> inherited/covered; 缺失 -> manual_review。"""

    def _document(self):
        return op_get_node_secret()

    def _resolvers(self, org_evidence):
        return {
            "contracts": {
                "Node.secret": {"checks": ["require_internal"]},
            },
            "inheritance": [
                {
                    "interface_field": "Node.secret",
                    "implementation": "User",
                    "resolver_inherits": True,
                    "evidence": "ci://resolver-map/1",
                },
                {
                    "interface_field": "Node.secret",
                    "implementation": "Org",
                    "resolver_inherits": True,
                    "evidence": org_evidence,
                },
            ],
        }

    def test_evidence_present_inherits_interface_contract(self):
        report = Checker(base_schema(), self._resolvers("ci://resolver-map/2"),
                         self._document()).run()
        self.assertEqual(report["summary"][MISSING], 0)
        self.assertEqual(report["summary"][MANUAL_REVIEW], 0)
        finding = report[COVERED][0]
        for d in finding["applicable"]:
            self.assertEqual(d["via"], "inherited")
            self.assertEqual(d["contract"], "Node.secret")
            self.assertEqual(d["status"], COVERED)

    def test_evidence_missing_requires_manual_review(self):
        report = Checker(base_schema(), self._resolvers(None),
                         self._document()).run()
        self.assertEqual(report["summary"][MISSING], 0)
        self.assertEqual(report["summary"][MANUAL_REVIEW], 1)
        finding = report[MANUAL_REVIEW][0]
        by_type = {d["type"]: d for d in finding["applicable"]}
        self.assertEqual(by_type["User"]["status"], COVERED)
        self.assertEqual(by_type["Org"]["status"], MANUAL_REVIEW)
        self.assertEqual(by_type["Org"]["reason"], "inheritance order evidence missing")
        self.assertEqual(by_type["Org"]["candidate_interfaces"], ["Node.secret"])


class ContractFixScopeTest(unittest.TestCase):
    """修正一条契约后应只改变相关结论。"""

    def _inputs(self):
        schema = base_schema()
        resolvers = {
            "contracts": {
                "User.secret": {"checks": ["require_owner_or_admin"]},
                "Org.secret": {"checks": []},
                "User.email": {"checks": ["require_owner_or_admin"]},
            },
            "inheritance": [],
        }
        document = {
            "operations": [
                {
                    "name": "GetNode",
                    "root_type": "Query",
                    "selections": [
                        {
                            "kind": "field",
                            "name": "node",
                            "selections": [{"kind": "field", "name": "secret"}],
                        }
                    ],
                },
                {
                    "name": "GetUser",
                    "root_type": "Query",
                    "selections": [
                        {
                            "kind": "field",
                            "name": "user",
                            "selections": [{"kind": "field", "name": "email"}],
                        }
                    ],
                },
            ],
            "fragments": {},
        }
        return schema, resolvers, document

    def test_fix_only_changes_related_conclusions(self):
        schema, resolvers, document = self._inputs()
        before = Checker(schema, copy.deepcopy(resolvers), copy.deepcopy(document)).run()

        fixed = copy.deepcopy(resolvers)
        fixed["contracts"]["Org.secret"] = {"checks": ["require_org_admin"]}
        after = Checker(schema, fixed, copy.deepcopy(document)).run()

        # 结论总数与路径集合不变
        self.assertEqual(findings_by_path(before).keys(), findings_by_path(after).keys())
        before_paths = findings_by_path(before)
        after_paths = findings_by_path(after)

        changed = [p for p in before_paths
                   if before_paths[p]["status"] != after_paths[p]["status"]]
        self.assertEqual(changed, ["GetNode.node.secret"])
        self.assertEqual(before_paths["GetNode.node.secret"]["status"], MISSING)
        self.assertEqual(after_paths["GetNode.node.secret"]["status"], COVERED)
        # 无关结论完全不变
        self.assertEqual(before_paths["GetUser.user.email"],
                         after_paths["GetUser.user.email"])
        # 相关结论中未受影响的实现细节也不变
        before_user = [d for d in before_paths["GetNode.node.secret"]["applicable"]
                       if d["type"] == "User"]
        after_user = [d for d in after_paths["GetNode.node.secret"]["applicable"]
                      if d["type"] == "User"]
        self.assertEqual(before_user, after_user)


class FragmentCycleTest(unittest.TestCase):
    """片段循环单独报告, 且分析必须终止。"""

    def test_cycle_reported_and_analysis_terminates(self):
        document = {
            "operations": [
                {
                    "name": "GetUser",
                    "root_type": "Query",
                    "selections": [
                        {
                            "kind": "field",
                            "name": "user",
                            "selections": [
                                {"kind": "fragment_spread", "name": "FragA"},
                            ],
                        }
                    ],
                }
            ],
            "fragments": {
                "FragA": {
                    "type_condition": "User",
                    "selections": [
                        {"kind": "field", "name": "email"},
                        {"kind": "fragment_spread", "name": "FragB"},
                    ],
                },
                "FragB": {
                    "type_condition": "User",
                    "selections": [
                        {"kind": "fragment_spread", "name": "FragA"},
                    ],
                },
            },
        }
        resolvers = {"contracts": {"User.email": {"checks": ["require_owner"]}},
                     "inheritance": []}
        report = Checker(base_schema(), resolvers, document).run()
        cycles = report["diagnostics"]["fragment_cycles"]
        self.assertEqual(len(cycles), 1)
        self.assertEqual(set(cycles[0]), {"FragA", "FragB"})
        # 循环不阻断正常字段的求值
        self.assertEqual(report["summary"][COVERED], 1)


class MissingResolverMetadataTest(unittest.TestCase):
    """缺失的解析器元数据单独报告, 结论为 manual_review。"""

    def test_missing_metadata_reported(self):
        resolvers = {"contracts": {}, "inheritance": []}
        report = Checker(base_schema(), resolvers, op_get_node_secret()).run()
        self.assertEqual(report["summary"][MISSING], 0)
        self.assertEqual(report["summary"][MANUAL_REVIEW], 1)
        diag = report["diagnostics"]["missing_resolver_metadata"]
        self.assertEqual({(d["type"], d["field"]) for d in diag},
                         {("User", "secret"), ("Org", "secret")})
        for d in diag:
            self.assertEqual(d["path"], "GetNode.node.secret")
        finding = report[MANUAL_REVIEW][0]
        for d in finding["applicable"]:
            self.assertEqual(d["reason"], "resolver metadata missing")


class SharedFragmentTest(unittest.TestCase):
    """共享片段在多个根操作中独立求值, 各自生成独立路径。"""

    def test_shared_fragment_evaluated_per_operation(self):
        document = {
            "operations": [
                {
                    "name": "OpA",
                    "root_type": "Query",
                    "selections": [
                        {
                            "kind": "field",
                            "name": "user",
                            "selections": [
                                {"kind": "fragment_spread", "name": "EmailBits"},
                            ],
                        }
                    ],
                },
                {
                    "name": "OpB",
                    "root_type": "Query",
                    "selections": [
                        {
                            "kind": "field",
                            "name": "user",
                            "selections": [
                                {"kind": "fragment_spread", "name": "EmailBits"},
                            ],
                        }
                    ],
                },
            ],
            "fragments": {
                "EmailBits": {
                    "type_condition": "User",
                    "selections": [{"kind": "field", "name": "email"}],
                }
            },
        }
        resolvers = {"contracts": {"User.email": {"checks": ["require_owner"]}},
                     "inheritance": []}
        report = Checker(base_schema(), resolvers, document).run()
        paths = sorted(findings_by_path(report))
        self.assertEqual(paths, ["OpA.user.email", "OpB.user.email"])
        ops = {findings_by_path(report)[p]["operation"] for p in paths}
        self.assertEqual(ops, {"OpA", "OpB"})


class FragmentTypeConditionTest(unittest.TestCase):
    """片段类型条件收窄接口的可能实现类型。"""

    def test_inline_fragment_narrows_applicable_types(self):
        document = {
            "operations": [
                {
                    "name": "GetNode",
                    "root_type": "Query",
                    "selections": [
                        {
                            "kind": "field",
                            "name": "node",
                            "selections": [
                                {
                                    "kind": "inline_fragment",
                                    "type_condition": "User",
                                    "selections": [
                                        {"kind": "field", "name": "secret"},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
            "fragments": {},
        }
        resolvers = {
            "contracts": {
                "User.secret": {"checks": ["require_owner"]},
                "Org.secret": {"checks": []},  # 被类型条件排除, 不应触发 missing
            },
            "inheritance": [],
        }
        report = Checker(base_schema(), resolvers, document).run()
        self.assertEqual(report["summary"][MISSING], 0)
        self.assertEqual(report["summary"][COVERED], 1)
        finding = report[COVERED][0]
        self.assertEqual([d["type"] for d in finding["applicable"]], ["User"])


if __name__ == "__main__":
    unittest.main()
