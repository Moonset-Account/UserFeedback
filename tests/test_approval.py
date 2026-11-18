"""审批权限判定模块的验收测试（unittest）。

覆盖维度：
- 可接受结果：敏感请求满足部门/在岗/新鲜度时 ALLOW；普通请求在业务范围内 ALLOW；
- 应被阻止/拒绝：在岗状态、部门、冲突、无记录等场景的 MANUAL_REVIEW / DENY；
- 边界与交错：生效时间、接收时间、新鲜度阈值的精确边界，迟到历史更正对
  当时决定与事后复核的不同影响，supersedes 链对冲突的消解。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from approval_service import (
    AccessRequest,
    ApprovalService,
    BusinessScope,
    DirectoryCatalog,
    DirectoryRecord,
    Outcome,
    ResourceRule,
    ReviewOutcome,
    resolve_view,
)


def t(year, month, day, hour=0, minute=0, second=0, microsecond=0):
    return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=timezone.utc)


THRESHOLD = timedelta(hours=24)

SENSITIVE = ResourceRule(
    resource_id="res-sensitive",
    sensitive=True,
    allowed_departments=frozenset({"fin"}),
)
NORMAL = ResourceRule(resource_id="res-normal", sensitive=False)
PAYMENT_SCOPE = BusinessScope(
    business_line="payment",
    allowed_departments=frozenset({"fin", "ops"}),
    require_on_duty=True,
)
NO_DUTY_SCOPE = BusinessScope(
    business_line="reporting",
    allowed_departments=frozenset({"fin"}),
    require_on_duty=False,
)


def make_service():
    catalog = DirectoryCatalog()
    service = ApprovalService(catalog, freshness_threshold=THRESHOLD)
    service.register_resource(SENSITIVE)
    service.register_resource(NORMAL)
    service.register_business_scope(PAYMENT_SCOPE)
    service.register_business_scope(NO_DUTY_SCOPE)
    return catalog, service


def req(request_id, subject_id, resource_id, at, business_line=None):
    return AccessRequest(
        request_id=request_id,
        subject_id=subject_id,
        resource_id=resource_id,
        business_line=business_line,
        requested_at=at,
    )


class SensitiveDecisionTests(unittest.TestCase):
    def test_acceptable_sensitive_request_allowed(self):
        catalog, service = make_service()
        catalog.add_record(
            DirectoryRecord(
                record_id="r1",
                subject_id="u1",
                department="fin",
                on_duty=True,
                effective_at=t(2026, 10, 31, 10),
                received_at=t(2026, 10, 31, 10, 5),
            )
        )
        decision = service.decide(req("d1", "u1", "res-sensitive", t(2026, 11, 1, 9)))
        self.assertEqual(decision.outcome, Outcome.ALLOW)
        self.assertEqual(decision.reasons, ())
        self.assertEqual(decision.applicable_record_id, "r1")
        self.assertEqual(decision.visible_record_ids, ("r1",))
        self.assertEqual(len(decision.input_fingerprint), 64)

    def test_off_duty_sensitive_request_blocked_to_manual(self):
        catalog, service = make_service()
        catalog.add_record(
            DirectoryRecord(
                record_id="r1",
                subject_id="u1",
                department="fin",
                on_duty=False,
                effective_at=t(2026, 10, 31, 10),
                received_at=t(2026, 10, 31, 10, 5),
            )
        )
        decision = service.decide(req("d1", "u1", "res-sensitive", t(2026, 11, 1, 9)))
        self.assertEqual(decision.outcome, Outcome.MANUAL_REVIEW)
        self.assertIn("subject_not_on_duty", decision.reasons)

    def test_department_outside_allowlist_blocked_to_manual(self):
        catalog, service = make_service()
        catalog.add_record(
            DirectoryRecord(
                record_id="r1",
                subject_id="u1",
                department="hr",
                on_duty=True,
                effective_at=t(2026, 10, 31, 10),
                received_at=t(2026, 10, 31, 10, 5),
            )
        )
        decision = service.decide(req("d1", "u1", "res-sensitive", t(2026, 11, 1, 9)))
        self.assertEqual(decision.outcome, Outcome.MANUAL_REVIEW)
        self.assertIn("department_not_allowed", decision.reasons)

    def test_freshness_threshold_inclusive_boundary(self):
        catalog, service = make_service()
        decision_instant = t(2026, 11, 1, 9)
        # 恰好在阈值边界上：年龄 == 阈值，不算过期。
        catalog.add_record(
            DirectoryRecord(
                record_id="fresh",
                subject_id="u-fresh",
                department="fin",
                on_duty=True,
                effective_at=decision_instant - THRESHOLD,
                received_at=decision_instant - THRESHOLD + timedelta(minutes=1),
            )
        )
        # 超出阈值 1 秒：过期。
        catalog.add_record(
            DirectoryRecord(
                record_id="stale",
                subject_id="u-stale",
                department="fin",
                on_duty=True,
                effective_at=decision_instant - THRESHOLD - timedelta(seconds=1),
                received_at=decision_instant - THRESHOLD,
            )
        )
        ok = service.decide(req("d-fresh", "u-fresh", "res-sensitive", decision_instant))
        stale = service.decide(req("d-stale", "u-stale", "res-sensitive", decision_instant))
        self.assertEqual(ok.outcome, Outcome.ALLOW)
        self.assertEqual(stale.outcome, Outcome.MANUAL_REVIEW)
        self.assertIn("stale_directory_record", stale.reasons)


class ConflictTests(unittest.TestCase):
    def _conflicting_records(self, subject_id):
        return [
            DirectoryRecord(
                record_id="c1",
                subject_id=subject_id,
                department="fin",
                on_duty=True,
                effective_at=t(2026, 10, 31, 10),
                received_at=t(2026, 10, 31, 10, 5),
            ),
            DirectoryRecord(
                record_id="c2",
                subject_id=subject_id,
                department="ops",
                on_duty=True,
                effective_at=t(2026, 10, 31, 10),
                received_at=t(2026, 10, 31, 10, 6),
            ),
        ]

    def test_conflicting_records_sensitive_manual_normal_deny(self):
        catalog, service = make_service()
        for record in self._conflicting_records("u1"):
            catalog.add_record(record)
        at = t(2026, 11, 1, 9)
        sensitive_decision = service.decide(req("d1", "u1", "res-sensitive", at))
        normal_decision = service.decide(
            req("d2", "u1", "res-normal", at, business_line="payment")
        )
        self.assertEqual(sensitive_decision.outcome, Outcome.MANUAL_REVIEW)
        self.assertIn("conflicting_directory_records", sensitive_decision.reasons)
        self.assertEqual(normal_decision.outcome, Outcome.DENY)
        self.assertIn("conflicting_directory_records", normal_decision.reasons)

        view = resolve_view(catalog.visible_records("u1", at), at)
        self.assertTrue(view.conflict)
        self.assertEqual(set(view.conflicting_record_ids), {"c1", "c2"})

    def test_supersedes_chain_resolves_conflict(self):
        catalog, service = make_service()
        for record in self._conflicting_records("u1"):
            catalog.add_record(record)
        # 历史更正显式取代 c2，且与 c1 状态一致：冲突消解。
        catalog.add_record(
            DirectoryRecord(
                record_id="c3",
                subject_id="u1",
                department="fin",
                on_duty=True,
                effective_at=t(2026, 10, 31, 10),
                received_at=t(2026, 10, 31, 12),
                supersedes="c2",
            )
        )
        decision = service.decide(req("d1", "u1", "res-sensitive", t(2026, 11, 1, 9)))
        self.assertEqual(decision.outcome, Outcome.ALLOW)
        self.assertEqual(decision.applicable_record_id, "c3")


class NormalRequestTests(unittest.TestCase):
    def test_business_scope_decisions(self):
        catalog, service = make_service()
        catalog.add_record(
            DirectoryRecord(
                record_id="r-hr",
                subject_id="u-hr",
                department="hr",
                on_duty=True,
                effective_at=t(2026, 10, 31, 10),
                received_at=t(2026, 10, 31, 10, 5),
            )
        )
        catalog.add_record(
            DirectoryRecord(
                record_id="r-off",
                subject_id="u-off",
                department="fin",
                on_duty=False,
                effective_at=t(2026, 10, 31, 10),
                received_at=t(2026, 10, 31, 10, 5),
            )
        )
        at = t(2026, 11, 1, 9)
        denied = service.decide(
            req("d-hr", "u-hr", "res-normal", at, business_line="payment")
        )
        off_duty = service.decide(
            req("d-off", "u-off", "res-normal", at, business_line="payment")
        )
        self.assertEqual(denied.outcome, Outcome.DENY)
        self.assertIn("department_outside_business_scope", denied.reasons)
        self.assertEqual(off_duty.outcome, Outcome.DENY)
        self.assertIn("subject_not_on_duty", off_duty.reasons)

        # reporting 业务不要求在岗：离岗仍可 ALLOW。
        allowed = service.decide(
            req("d-rep", "u-off", "res-normal", at, business_line="reporting")
        )
        self.assertEqual(allowed.outcome, Outcome.ALLOW)

    def test_normal_request_ignores_staleness_with_advisory(self):
        catalog, service = make_service()
        catalog.add_record(
            DirectoryRecord(
                record_id="old",
                subject_id="u1",
                department="fin",
                on_duty=True,
                effective_at=t(2026, 10, 28),  # 距判定 4 天，远超 24 小时阈值
                received_at=t(2026, 10, 28, 1),
            )
        )
        decision = service.decide(
            req("d1", "u1", "res-normal", t(2026, 11, 1), business_line="payment")
        )
        self.assertEqual(decision.outcome, Outcome.ALLOW)
        self.assertIn("stale_directory_record_advisory", decision.reasons)

    def test_no_visible_record_sensitive_manual_normal_deny(self):
        _, service = make_service()
        at = t(2026, 11, 1, 9)
        sensitive = service.decide(req("d1", "ghost", "res-sensitive", at))
        normal = service.decide(
            req("d2", "ghost", "res-normal", at, business_line="payment")
        )
        self.assertEqual(sensitive.outcome, Outcome.MANUAL_REVIEW)
        self.assertIn("no_visible_directory_record", sensitive.reasons)
        self.assertEqual(normal.outcome, Outcome.DENY)
        self.assertIn("no_visible_directory_record", normal.reasons)


class TimeBoundaryTests(unittest.TestCase):
    def test_effective_time_boundary(self):
        catalog = DirectoryCatalog()
        effective = t(2026, 11, 1, 9)
        catalog.add_record(
            DirectoryRecord(
                record_id="r1",
                subject_id="u1",
                department="fin",
                on_duty=True,
                effective_at=effective,
                received_at=t(2026, 10, 31),
            )
        )
        # 生效时刻当刻即可用。
        view_now = resolve_view(catalog.visible_records("u1", effective), effective)
        self.assertFalse(view_now.empty)
        # 生效前 1 微秒：已接收但未生效，视为无适用记录。
        view_before = resolve_view(
            catalog.visible_records("u1", effective - timedelta(microseconds=1)),
            effective - timedelta(microseconds=1),
        )
        self.assertTrue(view_before.empty)

    def test_received_time_boundary_and_future_effective(self):
        catalog, service = make_service()
        received_at = t(2026, 11, 1, 10)
        catalog.add_record(
            DirectoryRecord(
                record_id="late",
                subject_id="u1",
                department="fin",
                on_duty=True,
                effective_at=t(2026, 11, 1, 12),  # 接收时还未生效
                received_at=received_at,
            )
        )
        # 接收前 1 微秒：记录对判定不可见。
        before = service.decide(
            req("d-before", "u1", "res-sensitive", received_at - timedelta(microseconds=1))
        )
        self.assertEqual(before.outcome, Outcome.MANUAL_REVIEW)
        self.assertEqual(before.visible_record_ids, ())
        # 接收当刻可见，但生效时间在未来，仍无适用记录。
        at_receipt = service.decide(req("d-mid", "u1", "res-sensitive", received_at))
        self.assertEqual(at_receipt.outcome, Outcome.MANUAL_REVIEW)
        self.assertEqual(at_receipt.visible_record_ids, ("late",))
        self.assertIsNone(at_receipt.applicable_record_id)
        # 生效之后：同一记录转为适用，正常放行。
        after = service.decide(req("d-after", "u1", "res-sensitive", t(2026, 11, 1, 12)))
        self.assertEqual(after.outcome, Outcome.ALLOW)
        self.assertEqual(after.applicable_record_id, "late")

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValueError):
            DirectoryRecord(
                record_id="r1",
                subject_id="u1",
                department="fin",
                on_duty=True,
                effective_at=datetime(2026, 10, 31),
                received_at=t(2026, 10, 31),
            )


class ReviewAndCorrectionTests(unittest.TestCase):
    def _backdated_correction_scenario(self):
        """迟到的历史更正：决定作出后才被系统接收。"""
        catalog, service = make_service()
        catalog.add_record(
            DirectoryRecord(
                record_id="old",
                subject_id="u1",
                department="hr",  # 不在敏感资源允许部门内
                on_duty=True,
                effective_at=t(2026, 10, 30),
                received_at=t(2026, 10, 30, 1),
            )
        )
        decision_at = t(2026, 11, 1, 9)
        original = service.decide(req("d1", "u1", "res-sensitive", decision_at))
        self.assertEqual(original.outcome, Outcome.MANUAL_REVIEW)

        # 历史更正：生效时间回溯到更早，但接收时间晚于原决定。
        # 生效点距原决定时刻 23 小时，落在 24 小时新鲜度阈值内。
        catalog.add_record(
            DirectoryRecord(
                record_id="fix",
                subject_id="u1",
                department="fin",
                on_duty=True,
                effective_at=t(2026, 10, 31, 10),
                received_at=t(2026, 11, 2, 10),
                supersedes="old",
            )
        )
        return catalog, service, original

    def test_late_correction_invisible_then_reinforced_in_review(self):
        catalog, service, original = self._backdated_correction_scenario()

        # 原决定的可见输入快照与指纹不被事后更正改写。
        stored = service.get_decision("d1")
        self.assertEqual(stored.outcome, Outcome.MANUAL_REVIEW)
        self.assertEqual(stored.visible_record_ids, ("old",))
        self.assertEqual(stored.input_fingerprint, original.input_fingerprint)
        self.assertEqual(tuple(r.record_id for r in stored.visible_inputs), ("old",))

        report = service.review("d1", t(2026, 11, 3))
        self.assertEqual(report.original_outcome, Outcome.MANUAL_REVIEW)
        self.assertEqual(report.current_outcome, Outcome.ALLOW)
        self.assertEqual(report.review_outcome, ReviewOutcome.REINFORCED)
        self.assertTrue(report.visible_inputs_changed)
        self.assertTrue(report.applicable_data_changed)

        # 复核是追加行为：台账中的原决定仍然存在且内容不变。
        self.assertEqual(len(service.decisions), 1)
        self.assertEqual(service.get_decision("d1").outcome, Outcome.MANUAL_REVIEW)

    def test_review_upheld_when_nothing_changes(self):
        catalog, service = make_service()
        catalog.add_record(
            DirectoryRecord(
                record_id="r1",
                subject_id="u1",
                department="fin",
                on_duty=True,
                effective_at=t(2026, 10, 31, 10),
                received_at=t(2026, 10, 31, 10, 5),
            )
        )
        service.decide(req("d1", "u1", "res-sensitive", t(2026, 11, 1, 9)))
        # 同一天内复核，记录仍在 24 小时新鲜度窗口内。
        report = service.review("d1", t(2026, 11, 1, 15))
        self.assertEqual(report.review_outcome, ReviewOutcome.UPHELD)
        self.assertFalse(report.visible_inputs_changed)
        self.assertFalse(report.applicable_data_changed)

    def test_historical_correction_overturns_normal_denial(self):
        catalog, service = make_service()
        catalog.add_record(
            DirectoryRecord(
                record_id="old",
                subject_id="u1",
                department="hr",
                on_duty=True,
                effective_at=t(2026, 10, 30),
                received_at=t(2026, 10, 30, 1),
            )
        )
        original = service.decide(
            req("d1", "u1", "res-normal", t(2026, 11, 1, 9), business_line="payment")
        )
        self.assertEqual(original.outcome, Outcome.DENY)

        catalog.add_record(
            DirectoryRecord(
                record_id="fix",
                subject_id="u1",
                department="fin",
                on_duty=True,
                effective_at=t(2026, 10, 31, 10),
                received_at=t(2026, 11, 2, 10),
                supersedes="old",
            )
        )
        report = service.review("d1", t(2026, 11, 3))
        self.assertEqual(report.original_outcome, Outcome.DENY)
        self.assertEqual(report.current_outcome, Outcome.ALLOW)
        self.assertEqual(report.review_outcome, ReviewOutcome.OVERTURNED)
        # 实际作出的历史决定保留为 DENY。
        self.assertEqual(service.get_decision("d1").outcome, Outcome.DENY)

    def test_correction_taking_subject_off_duty_overturns_allow(self):
        catalog, service = make_service()
        catalog.add_record(
            DirectoryRecord(
                record_id="old",
                subject_id="u1",
                department="fin",
                on_duty=True,
                effective_at=t(2026, 10, 31, 10),
                received_at=t(2026, 10, 31, 10, 5),
            )
        )
        original = service.decide(req("d1", "u1", "res-sensitive", t(2026, 11, 1, 9)))
        self.assertEqual(original.outcome, Outcome.ALLOW)

        catalog.add_record(
            DirectoryRecord(
                record_id="fix",
                subject_id="u1",
                department="fin",
                on_duty=False,
                effective_at=t(2026, 10, 31, 10),
                received_at=t(2026, 11, 2, 8),
                supersedes="old",
            )
        )
        report = service.review("d1", t(2026, 11, 2, 9))
        self.assertEqual(report.current_outcome, Outcome.MANUAL_REVIEW)
        self.assertEqual(report.review_outcome, ReviewOutcome.OVERTURNED)

    def test_interleaved_correction_and_late_update_visibility(self):
        """交错场景：同一主体上迟到更正与迟到的普通更新同时存在。"""
        catalog, service = make_service()
        catalog.add_record(
            DirectoryRecord(
                record_id="base",
                subject_id="u1",
                department="fin",
                on_duty=True,
                effective_at=t(2026, 10, 31, 10),
                received_at=t(2026, 10, 31, 10, 1),
            )
        )
        decision_at = t(2026, 11, 1, 9)
        first = service.decide(req("d1", "u1", "res-sensitive", decision_at))
        self.assertEqual(first.visible_record_ids, ("base",))
        self.assertEqual(first.outcome, Outcome.ALLOW)

        # 更正 base（回溯生效），另一条更新生效时间在复核时刻之后。
        catalog.add_record(
            DirectoryRecord(
                record_id="fix",
                subject_id="u1",
                department="fin",
                on_duty=False,
                effective_at=t(2026, 10, 31, 10),
                received_at=t(2026, 11, 1, 15),
                supersedes="base",
            )
        )
        catalog.add_record(
            DirectoryRecord(
                record_id="future",
                subject_id="u1",
                department="fin",
                on_duty=True,
                effective_at=t(2026, 11, 5),
                received_at=t(2026, 11, 1, 16),
            )
        )
        review_at = t(2026, 11, 2, 9)
        report = service.review("d1", review_at)
        # future 已接收但未生效；fix 已生效且取代 base，复核以离岗为准。
        self.assertTrue(report.visible_inputs_changed)
        self.assertEqual(report.current_outcome, Outcome.MANUAL_REVIEW)
        self.assertEqual(report.review_outcome, ReviewOutcome.OVERTURNED)

        view = resolve_view(catalog.visible_records("u1", review_at), review_at)
        self.assertEqual(set(r.record_id for r in view.applicable), {"base", "fix"})
        self.assertEqual(view.representative.record_id, "fix")
        self.assertFalse(view.conflict)


class LedgerIntegrityTests(unittest.TestCase):
    def test_duplicate_request_and_record_ids_rejected(self):
        catalog, service = make_service()
        catalog.add_record(
            DirectoryRecord(
                record_id="r1",
                subject_id="u1",
                department="fin",
                on_duty=True,
                effective_at=t(2026, 10, 31, 10),
                received_at=t(2026, 10, 31, 10, 5),
            )
        )
        with self.assertRaises(ValueError):
            catalog.add_record(
                DirectoryRecord(
                    record_id="r1",
                    subject_id="u1",
                    department="fin",
                    on_duty=False,
                    effective_at=t(2026, 10, 31, 11),
                    received_at=t(2026, 10, 31, 11, 5),
                )
            )
        service.decide(req("d1", "u1", "res-sensitive", t(2026, 11, 1, 9)))
        with self.assertRaises(ValueError):
            service.decide(req("d1", "u1", "res-sensitive", t(2026, 11, 1, 10)))

    def test_fingerprint_stable_under_unchanged_inputs(self):
        catalog, service = make_service()
        catalog.add_record(
            DirectoryRecord(
                record_id="r1",
                subject_id="u1",
                department="fin",
                on_duty=True,
                effective_at=t(2026, 10, 31, 10),
                received_at=t(2026, 10, 31, 10, 5),
            )
        )
        service.decide(req("d1", "u1", "res-sensitive", t(2026, 11, 1, 9)))
        report = service.review("d1", t(2026, 11, 2, 9))
        self.assertFalse(report.visible_inputs_changed)

        catalog.add_record(
            DirectoryRecord(
                record_id="r2",
                subject_id="u1",
                department="fin",
                on_duty=True,
                effective_at=t(2026, 11, 2, 8),
                received_at=t(2026, 11, 2, 8, 5),
            )
        )
        report_after = service.review("d1", t(2026, 11, 2, 9))
        self.assertTrue(report_after.visible_inputs_changed)

    def test_unknown_resource_and_missing_business_line_rejected(self):
        _, service = make_service()
        with self.assertRaises(KeyError):
            service.decide(req("d1", "u1", "res-unknown", t(2026, 11, 1)))
        with self.assertRaises(ValueError):
            service.decide(req("d2", "u1", "res-normal", t(2026, 11, 1)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
