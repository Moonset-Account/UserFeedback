"""签章修订检查器的场景测试：正常流程、边界条件与交错修订。"""

import unittest

from sigcheck.inspector import (
    INFO,
    VIOLATION,
    R_ALLOWED_FIELD_FILL,
    R_BENIGN_REWRITE,
    R_BYTE_RANGE_TAMPER,
    R_INEFFECTIVE_ALLOWANCE,
    R_OBJECT_MODIFIED,
    R_REMOVED_REFERENCE,
    inspect_document,
)

from tests.builders import DocBuilder, standard_revisions


def baseline_builder() -> DocBuilder:
    """标准五版文书：

    r0：页面 P -> 正文 C1/C2（body），P -> 表单字典 F（field 边 f1）-> V1；
        另一个允许字段 f2 的值对象 V2 尚未填（field 边在 r2 才出现）。
    r1：认证签章 S1（允许 f1、f2 后填），签章对象 SIG1。
    r2-r4：后续增量保存，测试中按场景追加事件。
    """

    builder = DocBuilder(standard_revisions(5))
    (
        builder.set_page_roots("P")
        # r0 基础对象
        .add_object("P", "r0", "d-P")
        .add_object("C1", "r0", "d-C1-v0")
        .add_object("C2", "r0", "d-C2-v0")
        .add_object("F", "r0", "d-F")
        .add_object("V1", "r0", "d-V1-empty")
        .add_body_edge("P", "C1", "r0")
        .add_body_edge("C1", "C2", "r0")
        .add_field_edge("P", "F", "f1", "r0")
        .add_field_edge("F", "V1", "f1", "r0")
        # r1 签章
        .add_object("SIG1", "r1", "d-SIG1")
        .add_signature(
            "S1",
            "r1",
            byte_range=(100, 180),
            signature_object_id="SIG1",
            allowed_fields=("f1", "f2"),
        )
    )
    return builder


class RulesByRuleId:
    def __init__(self, findings) -> None:
        self.by_rule = {}
        for finding in findings:
            self.by_rule.setdefault(finding.rule_id, []).append(finding)

    def get(self, rule_id):
        return self.by_rule.get(rule_id, [])

    def one(self, rule_id):
        items = self.get(rule_id)
        assert len(items) == 1, f"规则 {rule_id} 期望 1 条，实际 {len(items)} 条"
        return items[0]


class NormalFlowTest(unittest.TestCase):
    def test_no_changes_after_signing_is_clean(self) -> None:
        doc = baseline_builder().build()
        report = inspect_document(doc)
        self.assertFalse(report.violated)
        self.assertIsNone(report.earliest_violation)
        assessment = report.assessment_for("S1")
        # F 只经 field 边可达，不属于正文覆盖；V1 同理。
        self.assertEqual(
            set(assessment.covered_object_ids), {"P", "C1", "C2"}
        )
        # 签章当时已通过允许字段可达的对象登记为可后填对象。
        self.assertEqual(
            set(assessment.allowed_fill_object_ids), {"F", "V1"}
        )

    def test_appending_unrelated_new_object_is_allowed(self) -> None:
        builder = baseline_builder()
        (
            builder.add_object("NEW1", "r2", "d-NEW1")
            .add_body_edge("C2", "NEW1", "r2")
            .add_object("NEW2", "r3", "d-NEW2")
            .add_body_edge("NEW1", "NEW2", "r3")
        )
        report = inspect_document(builder.build())
        self.assertFalse(report.violated)
        self.assertEqual(report.assessment_for("S1").findings, [])

    def test_allowed_field_fill_is_info_not_violation(self) -> None:
        builder = baseline_builder()
        builder.add_object("V1", "r2", "d-V1-filled")
        report = inspect_document(builder.build())
        self.assertFalse(report.violated)
        rules = RulesByRuleId(report.assessment_for("S1").findings)
        finding = rules.one(R_ALLOWED_FIELD_FILL)
        self.assertEqual(finding.severity, INFO)
        self.assertEqual(finding.revision_id, "r2")
        self.assertEqual(finding.object_id, "V1")
        self.assertEqual(finding.field_name, "f1")
        # 引用链是 field 链：P -> F -> V1
        self.assertEqual(
            [(hop.source_id, hop.target_id, hop.kind) for hop in finding.chain],
            [("P", "F", "field"), ("F", "V1", "field")],
        )

    def test_late_fill_of_previously_empty_allowed_field(self) -> None:
        # f2 在签章当时还没有值对象；r2 新增 field 边与值对象，属于允许后填。
        builder = baseline_builder()
        (
            builder.add_object("V2", "r2", "d-V2-filled")
            .add_field_edge("F", "V2", "f2", "r2")
            .add_object("V2", "r3", "d-V2-updated")
        )
        report = inspect_document(builder.build())
        self.assertFalse(report.violated)
        assessment = report.assessment_for("S1")
        # V2 在签章当时尚不存在，因此不在静态可见集合内，但动态豁免仍生效。
        self.assertNotIn("V2", assessment.allowed_fill_object_ids)
        rules = RulesByRuleId(assessment.findings)
        fills = rules.get(R_ALLOWED_FIELD_FILL)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].revision_id, "r3")
        self.assertEqual(fills[0].object_id, "V2")
        self.assertEqual(fills[0].field_name, "f2")


class BoundaryConditionTest(unittest.TestCase):
    def test_body_referenced_field_object_change_violates(self) -> None:
        # V1 同时被正文引用：允许字段对它无效，改它就是改已签正文。
        builder = baseline_builder()
        builder.add_body_edge("C2", "V1", "r0")
        builder.add_object("V1", "r2", "d-V1-tampered")
        report = inspect_document(builder.build())
        self.assertTrue(report.violated)
        assessment = report.assessment_for("S1")
        self.assertNotIn("V1", assessment.allowed_fill_object_ids)
        rules = RulesByRuleId(assessment.findings)
        violation = rules.one(R_OBJECT_MODIFIED)
        self.assertEqual(violation.revision_id, "r2")
        self.assertEqual(violation.object_id, "V1")
        # 引用链必须是一条 body 链，从页面根直达被改对象。
        self.assertTrue(all(hop.kind == "body" for hop in violation.chain))
        self.assertEqual(violation.chain[0].source_id, "P")
        self.assertEqual(violation.chain[-1].target_id, "V1")
        self.assertIsNotNone(rules.one(R_INEFFECTIVE_ALLOWANCE))

    def test_signed_body_modification_earliest_revision_and_chain(self) -> None:
        builder = baseline_builder()
        builder.add_object("C2", "r3", "d-C2-v1")
        report = inspect_document(builder.build())
        self.assertTrue(report.violated)
        earliest = report.earliest_violation
        self.assertEqual(earliest.revision_id, "r3")
        self.assertEqual(earliest.rule_id, R_OBJECT_MODIFIED)
        self.assertEqual(earliest.object_id, "C2")
        self.assertEqual(
            [(h.source_id, h.target_id) for h in earliest.chain],
            [("P", "C1"), ("C1", "C2")],
        )

    def test_revert_to_original_digest_keeps_prior_violation(self) -> None:
        builder = baseline_builder()
        (
            builder.add_object("C1", "r2", "d-C1-evil")
            .add_object("C1", "r3", "d-C1-v0")  # 改回原样
        )
        report = inspect_document(builder.build())
        self.assertTrue(report.violated)
        assessment = report.assessment_for("S1")
        self.assertEqual(assessment.earliest_violation.revision_id, "r2")
        rules = RulesByRuleId(assessment.findings)
        # r2 是首次越界；r3 相对 r2 的污染版又发生了一次真实替换（虽然
        # 内容回到了签章时的摘要），同样留痕；最早越界不受影响。
        violations = rules.get(R_OBJECT_MODIFIED)
        self.assertEqual([v.revision_id for v in violations], ["r2", "r3"])
        # 没有“摘要未变”的良性重写：r3 的上一版是 evil 版。
        self.assertEqual(rules.get(R_BENIGN_REWRITE), [])
        # 即便后来改回，报告级最早越界仍定格在 r2。
        self.assertEqual(report.earliest_violation.revision_id, "r2")

    def test_same_digest_rewrite_is_benign(self) -> None:
        builder = baseline_builder()
        builder.add_object("C1", "r2", "d-C1-v0")
        report = inspect_document(builder.build())
        self.assertFalse(report.violated)
        rules = RulesByRuleId(report.assessment_for("S1").findings)
        self.assertEqual(rules.one(R_BENIGN_REWRITE).object_id, "C1")

    def test_approval_signature_rejects_even_field_fill(self) -> None:
        builder = DocBuilder(standard_revisions(4))
        (
            builder.set_page_roots("P")
            .add_object("P", "r0", "d-P")
            .add_object("F", "r0", "d-F")
            .add_object("V", "r0", "d-V-empty")
            .add_body_edge("P", "V", "r0")
            .add_field_edge("P", "F", "fa", "r0")
            .add_field_edge("F", "V", "fa", "r0")
            .add_object("SIGA", "r1", "d-SIGA")
            .add_signature("SA", "r1", (100, 170), "SIGA", sig_type="approval")
            .add_object("V", "r2", "d-V-filled")
        )
        report = inspect_document(builder.build())
        self.assertTrue(report.violated)
        rules = RulesByRuleId(report.assessment_for("SA").findings)
        self.assertEqual(rules.one(R_OBJECT_MODIFIED).revision_id, "r2")
        self.assertEqual(rules.get(R_ALLOWED_FIELD_FILL), [])

    def test_modifying_signature_object_is_outside_coverage_not_flagged_as_body(
        self,
    ) -> None:
        # 签章对象自身不属于正文覆盖范围；对它的替换由字节范围/密码学验真负责，
        # 本检查器的对象修改规则不应误报为正文越界。
        builder = baseline_builder()
        builder.add_object("SIG1", "r2", "d-SIG1-replaced")
        report = inspect_document(builder.build())
        assessment = report.assessment_for("S1")
        self.assertNotIn("SIG1", assessment.covered_object_ids)
        self.assertEqual(
            [f for f in assessment.findings if f.rule_id == R_OBJECT_MODIFIED],
            [],
        )


class InterleavingTest(unittest.TestCase):
    def test_field_then_body_interleaving_violates_at_body_change(self) -> None:
        # r2 先给 V1 补一条正文引用（结构变化本身是新边追加，不触碰旧对象）；
        # r3 再改 V1 时，它已经是正文可达对象 -> r3 越界，且给出 body 链。
        builder = baseline_builder()
        (
            builder.add_body_edge("C2", "V1", "r2")
            .add_object("V1", "r3", "d-V1-tampered")
        )
        report = inspect_document(builder.build())
        self.assertTrue(report.violated)
        earliest = report.earliest_violation
        self.assertEqual(earliest.revision_id, "r3")
        self.assertEqual(earliest.object_id, "V1")
        self.assertTrue(all(hop.kind == "body" for hop in earliest.chain))
        self.assertEqual(earliest.chain[-1].target_id, "V1")

    def test_promotion_violation_reported_only_at_first_revision(self) -> None:
        # 同版填值 + 接入正文：r2 即越界；r3 再次改写 V1 时它已稳定是正文
        # 对象，按覆盖对象规则继续留痕，但升格类提示只在首版出现一次。
        builder = baseline_builder()
        (
            builder.add_object("V1", "r2", "d-V1-filled")
            .add_body_edge("C2", "V1", "r2")
            .add_object("V1", "r3", "d-V1-again")
        )
        report = inspect_document(builder.build())
        findings = report.assessment_for("S1").findings
        v1_violations = [
            f
            for f in findings
            if f.object_id == "V1" and f.rule_id == R_OBJECT_MODIFIED
        ]
        self.assertEqual([f.revision_id for f in v1_violations], ["r2", "r3"])
        self.assertEqual(report.earliest_violation.revision_id, "r2")
        self.assertFalse(
            any(f.rule_id == R_ALLOWED_FIELD_FILL for f in findings)
        )

    def test_fill_before_body_edge_in_later_revision_is_allowed(self) -> None:
        # r2 仅填值（仍只经 field 可达）-> 允许；r3 才接入正文但内容未再变，
        # 不产生越界（没有新的替换事件，已签正文对象的字节未被改动）。
        builder = baseline_builder()
        (
            builder.add_object("V1", "r2", "d-V1-filled")
            .add_body_edge("C2", "V1", "r3")
        )
        report = inspect_document(builder.build())
        self.assertFalse(report.violated)
        rules = RulesByRuleId(report.assessment_for("S1").findings)
        self.assertEqual(rules.one(R_ALLOWED_FIELD_FILL).revision_id, "r2")

    def test_multiple_violations_report_earliest_revision(self) -> None:
        builder = baseline_builder()
        (
            builder.add_object("C2", "r3", "d-C2-v1")
            .add_object("C1", "r2", "d-C1-v1")
        )
        report = inspect_document(builder.build())
        self.assertEqual(report.earliest_violation.revision_id, "r2")
        assessment = report.assessment_for("S1")
        rule_map = RulesByRuleId(assessment.findings)
        self.assertEqual(
            {f.revision_id for f in rule_map.get(R_OBJECT_MODIFIED)},
            {"r2", "r3"},
        )

    def test_later_signature_computed_independently(self) -> None:
        # r2 旧签章 S1 已被 C1 改写越界；r3 新增签章 S2 从 r3 状态独立计算，
        # 对 S2 而言 r3 的 C1 是已签正文，之后 r4 再改才越界。
        builder = baseline_builder()
        (
            builder.add_object("C1", "r2", "d-C1-evil")
            .add_object("SIG2", "r3", "d-SIG2")
            .add_signature(
                "S2",
                "r3",
                byte_range=(300, 360),
                signature_object_id="SIG2",
                allowed_fields=("f1",),
            )
            .add_object("C1", "r4", "d-C1-eviler")
        )
        report = inspect_document(builder.build())
        s1 = report.assessment_for("S1")
        s2 = report.assessment_for("S2")
        self.assertEqual(s1.earliest_violation.revision_id, "r2")
        self.assertEqual(s2.earliest_violation.revision_id, "r4")
        self.assertEqual(report.earliest_violation.revision_id, "r2")
        # S2 的覆盖范围基于 r3 的实际对象状态计算，摘要已是 evil 版。
        self.assertIn("C1", s2.covered_object_ids)

    def test_byte_range_overlap_flags_tamper(self) -> None:
        # 构造一个与签章字节区间重叠的修订（模型层拒绝重叠区间，
        # 因此用 assemble_unchecked 装配，由检查器负责识别篡改）。
        from sigcheck.model import Revision

        builder = baseline_builder()
        overlapping = builder.build_with_revisions(
            [
                Revision("r0", 0, 100),
                Revision("r1", 100, 200),
                Revision("r2", 150, 250),  # 与 r1 的 [100,200) 重叠
                Revision("r3", 250, 350),
                Revision("r4", 350, 450),
            ]
        )
        report = inspect_document(overlapping)
        rules = RulesByRuleId(report.assessment_for("S1").findings)
        tampers = rules.get(R_BYTE_RANGE_TAMPER)
        # 只有 r2[150,250) 与签章全局范围 [100,180) 真正重叠；
        # r3[250,350)、r4[350,450) 位于签章之后，不构成字节区间侵入。
        self.assertEqual([item.revision_id for item in tampers], ["r2"])
        first = tampers[0]
        self.assertEqual(first.severity, VIOLATION)
        self.assertEqual(first.byte_range, (150, 250))

    def test_removed_body_reference_is_violation_with_chain(self) -> None:
        # 独立文书：P -> C1 的正文边在 r2 被移除，已签正文结构被改变。
        builder = DocBuilder(standard_revisions(4))
        (
            builder.set_page_roots("P")
            .add_object("P", "r0", "d-P")
            .add_object("C1", "r0", "d-C1-v0")
            .add_body_edge("P", "C1", "r0", removed_in="r2")
            .add_object("SIG1", "r1", "d-SIG1")
            .add_signature("S1", "r1", (100, 180), "SIG1")
        )
        report = inspect_document(builder.build())
        self.assertTrue(report.violated)
        rules = RulesByRuleId(report.assessment_for("S1").findings)
        removed = rules.one(R_REMOVED_REFERENCE)
        self.assertEqual(removed.revision_id, "r2")
        self.assertEqual(
            [(h.source_id, h.target_id, h.kind) for h in removed.chain],
            [("P", "C1", "body")],
        )

    def test_new_signature_violating_older_one_still_recorded_for_both(self) -> None:
        # r3 新签章 S2 所在修订同时改写了 S1 覆盖的 C2：
        # S1 记 r3 越界；S2 自己从 r3 起算，不把该事件算作自己的越界。
        builder = baseline_builder()
        (
            builder.add_object("C2", "r3", "d-C2-v1")
            .add_object("SIG2", "r3", "d-SIG2")
            .add_signature("S2", "r3", (300, 370), "SIG2", sig_type="approval")
        )
        report = inspect_document(builder.build())
        s1 = report.assessment_for("S1")
        s2 = report.assessment_for("S2")
        self.assertEqual(s1.earliest_violation.revision_id, "r3")
        self.assertFalse(s2.violated)
        self.assertTrue(report.violated)


class ReportShapeTest(unittest.TestCase):
    def test_report_is_json_serializable_and_roundtrips(self) -> None:
        import json

        builder = baseline_builder()
        builder.add_object("C1", "r2", "d-C1-evil")
        report = inspect_document(builder.build())
        encoded = report.to_json()
        decoded = json.loads(encoded)
        self.assertTrue(decoded["violated"])
        self.assertEqual(decoded["earliest_violation"]["revision_id"], "r2")
        self.assertEqual(len(decoded["assessments"]), 1)
        chain = decoded["earliest_violation"]["chain"]
        self.assertEqual(chain[0]["source_id"], "P")

    def test_findings_sorted_by_revision_order(self) -> None:
        builder = baseline_builder()
        (
            builder.add_object("C1", "r2", "d-C1-a")
            .add_object("C1", "r3", "d-C1-b")
            .add_object("C2", "r4", "d-C2-c")
        )
        report = inspect_document(builder.build())
        findings = report.assessment_for("S1").findings
        revisions = [f.revision_id for f in findings]
        self.assertEqual(revisions, ["r2", "r3", "r4"])


if __name__ == "__main__":
    unittest.main()
