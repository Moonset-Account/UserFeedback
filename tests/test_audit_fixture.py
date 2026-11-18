"""基于合成夹具的端到端核查验收。

覆盖：
* 可接受结果：重叠窗口内所有单元通过，结论 compatible；
* 应被阻断的结果：preserve 落在要求 reject 的消息类型上，
  阻断并定位到生效配置层；
* 边界/交错场景：版本范围恰好相接、无交集（no_overlap）、
  未定义走内建兜底、版本默认回退、显式禁用与未定义分开归因。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compat_audit import audit_config, load_config
from compat_audit.model import PairStatus, ReasonCode

FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fixtures",
    "sample_config.json",
)


class FixtureAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = audit_config(load_config(FIXTURE))
        cls.by_pair = {c.pair_id: c for c in cls.report.conclusions}

    def test_summary_counts(self):
        self.assertEqual(
            self.report.summary,
            {"total": 5, "compatible": 1, "blocked": 3, "no_overlap": 1},
        )
        self.assertTrue(self.report.has_blocking)

    def test_acceptable_pair_is_compatible(self):
        conclusion = self.by_pair["producer-old -> consumer-old"]
        self.assertEqual(conclusion.status, PairStatus.COMPATIBLE.value)
        # 边界：重叠窗口恰好覆盖 v1/v2 两个版本（两端范围完全相接）
        self.assertEqual(conclusion.overlap_versions, ["v1", "v2"])
        self.assertEqual(len(conclusion.cells), 4)
        self.assertTrue(all(cell.ok for cell in conclusion.cells))
        self.assertEqual(conclusion.findings, [])

    def test_explicit_null_is_undefined_and_version_default_takes_over(self):
        # consumer-old 只在 base 层显式定义 unknown_fields，
        # extra_validation 给的是 null（未定义），v1 应回退到版本默认 true
        cell = next(
            c
            for c in self.by_pair["producer-old -> consumer-old"].cells
            if c.version == "v1" and c.message_type == "OrderEvent"
        )
        ev = cell.consumer["extra_validation"]
        self.assertTrue(ev.value)
        self.assertTrue(ev.defined)
        self.assertEqual(ev.source.source_type, "version_default")
        self.assertIsNone(ev.source.layer)

    def test_blocked_pair_locates_effective_layer_for_retained_fields(self):
        conclusion = self.by_pair["producer-new -> consumer-strict"]
        self.assertEqual(conclusion.status, PairStatus.BLOCKED.value)
        self.assertEqual(conclusion.overlap_versions, ["v2", "v3", "v4"])

        retained = [
            f
            for f in conclusion.findings
            if f.reason_code == ReasonCode.UNDECLARED_FIELDS_RETAINED.value
            and f.endpoint == "producer-new"
            and f.version == "v3"
        ]
        self.assertEqual(len(retained), 1)
        finding = retained[0]
        # 本应 reject 的未声明字段被 preserve —— 题面核心阻断条件
        self.assertEqual(finding.effective_value, "preserve")
        self.assertEqual(finding.required_value, "reject")
        # 必须定位到生效配置层：service 层的精确作用域条目
        self.assertEqual(finding.source.source_type, "layer")
        self.assertEqual(finding.source.layer, "service")
        self.assertEqual(finding.source.layer_priority, 20)
        self.assertEqual(finding.source.version_scope, "v3")
        self.assertEqual(finding.source.message_type_scope, "OrderEvent")

    def test_undefined_option_falls_back_to_builtin_and_is_still_blocked(self):
        # consumer-strict 在 v3 没有任何 unknown_fields 定义，
        # v3 版本默认也是 null —— 落到内建兜底 preserve，defined 必须为 False，
        # 但对要求 reject 的 OrderEvent 一样要阻断
        conclusion = self.by_pair["producer-new -> consumer-strict"]
        cell = next(c for c in conclusion.cells if c.version == "v3")
        uf = cell.consumer["unknown_fields"]
        self.assertEqual(uf.value, "preserve")
        self.assertFalse(uf.defined)
        self.assertFalse(uf.explicitly_disabled)
        self.assertEqual(uf.source.source_type, "builtin_default")

        retained = [
            f
            for f in cell.findings
            if f.endpoint == "consumer-strict"
            and f.reason_code == ReasonCode.UNDECLARED_FIELDS_RETAINED.value
        ]
        self.assertEqual(len(retained), 1)

    def test_drop_when_reject_required_is_blocked_with_layer_location(self):
        # 边界交错：v4 上 service 层显式 drop，消息类型强制 reject，
        # drop 不保留字段所以不报 retained，但仍属策略冲突，定位到 service 层
        conclusion = self.by_pair["producer-new -> consumer-strict"]
        v4_findings = [f for f in conclusion.findings if f.version == "v4"]
        self.assertEqual(len(v4_findings), 1)
        finding = v4_findings[0]
        self.assertEqual(finding.reason_code, ReasonCode.POLICY_NOT_ALLOWED.value)
        self.assertEqual(finding.effective_value, "drop")
        self.assertEqual(finding.source.layer, "service")

    def test_explicitly_disabled_distinguished_from_undefined(self):
        # consumer-future：base 层显式 false —— 必须报 explicitly_disabled
        future = self.by_pair["producer-new -> consumer-future"]
        self.assertEqual(future.status, PairStatus.BLOCKED.value)
        self.assertEqual(future.overlap_versions, ["v4"])  # 边界：单点重叠
        disabled = [
            f
            for f in future.findings
            if f.reason_code == ReasonCode.SAFEGUARD_EXPLICITLY_DISABLED.value
        ]
        self.assertEqual(len(disabled), 1)
        self.assertEqual(disabled[0].source.layer, "base")
        self.assertTrue(
            future.cells[0].consumer["extra_validation"].explicitly_disabled
        )

        # consumer-loose：什么都没配 —— 同样是 false 效果，但原因码必须是 undefined
        loose = self.by_pair["producer-new -> consumer-loose"]
        undefined = [
            f
            for f in loose.findings
            if f.reason_code == ReasonCode.SAFEGUARD_UNDEFINED.value
        ]
        self.assertTrue(undefined)
        self.assertTrue(
            all(
                not loose.cells[0].consumer["extra_validation"].explicitly_disabled
                for _ in [0]
            )
        )
        sources = {f.source.source_type for f in undefined}
        self.assertIn("builtin_default", sources)  # v3：完全未定义
        self.assertIn("version_default", sources)  # v4：由版本默认给 false

    def test_no_overlap_pair_is_not_evaluated(self):
        conclusion = self.by_pair["producer-old -> consumer-future"]
        self.assertEqual(conclusion.status, PairStatus.NO_OVERLAP.value)
        self.assertEqual(conclusion.overlap_versions, [])
        self.assertEqual(conclusion.cells, [])
        self.assertEqual(conclusion.findings, [])


if __name__ == "__main__":
    unittest.main()
