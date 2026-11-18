"""离线授权审计库的验收测试。

覆盖:
- 可接受结果:时段重叠但存在合法顺序(含组变更对成员同时生效、边界时刻);
- 应被拒绝的结果:拼接两代授权字段、授权版本不被尊重;
- 交错/边界场景:最小冲突定位、回执缺失影响结论时转人工、
  回执缺失但不影响结论时照常判定。
"""

import unittest

from authz_audit import (
    AuditInput,
    Auditor,
    FieldGrading,
    PermissionChange,
    ReadRecord,
    Subject,
    Verdict,
    expand_level_grant,
)

GRADING = FieldGrading(
    levels={
        "doc": {"title": 0, "body": 1, "salary": 2, "ssn": 3},
    }
)

SUBJECTS = (
    Subject(id="alice", groups=frozenset({"staff"})),
    Subject(id="bob", groups=frozenset({"staff"})),
)

BASE_PERMS = {
    "alice": {"doc": expand_level_grant(GRADING, "doc", 1)},  # title, body
    "bob": {"doc": expand_level_grant(GRADING, "doc", 1)},
}


def make_input(changes=(), reads=()):
    return AuditInput(
        tenant="tenant-1",
        grading=GRADING,
        subjects=SUBJECTS,
        initial_permissions=BASE_PERMS,
        changes=tuple(changes),
        reads=tuple(reads),
    )


class TestAcceptableOrderings(unittest.TestCase):
    """时段重叠但存在合法顺序 -> CONSISTENT,并给出判定依据。"""

    def test_overlapping_windows_with_legal_order(self):
        change = PermissionChange(
            id="C1",
            version=1,
            group="staff",
            resource="doc",
            grant=frozenset({"salary"}),
            window=(10.0, 12.0),
            receipt=True,
        )
        reads = [
            # 变更前完成,看到旧状态,声明版本 0
            ReadRecord("R1", "alice", "doc", frozenset({"title", "body"}), 0.0, 9.0, authz_version=0),
            # 与变更窗口重叠,但在变更生效后看到新状态,声明版本 1
            ReadRecord("R2", "bob", "doc", frozenset({"title", "body", "salary"}), 11.0, 20.0, authz_version=1),
            # 与变更窗口重叠,仍可用变更前的状态解释(未观察到新字段)
            ReadRecord("R3", "alice", "doc", frozenset({"title", "body"}), 9.0, 11.0),
        ]
        result = Auditor(make_input([change], reads)).run()
        self.assertEqual(result.verdict, Verdict.CONSISTENT)
        # 判定依据:变更被安排在窗口内的某个时刻
        self.assertIn("C1", result.change_times)
        self.assertGreaterEqual(result.change_times["C1"], 10.0)
        self.assertLessEqual(result.change_times["C1"], 12.0)
        # 每条读取都有观察时刻与状态版本
        self.assertEqual(result.read_witnesses["R1"].state_version, 0)
        self.assertEqual(result.read_witnesses["R2"].state_version, 1)
        self.assertTrue(any("R2" in r for r in result.reasons))

    def test_group_change_atomic_for_all_members_at_boundary(self):
        """组变更对两名成员同一时刻生效;边界时刻两侧各观察到一代。"""
        change = PermissionChange(
            id="C1",
            version=1,
            group="staff",
            resource="doc",
            grant=frozenset({"salary"}),
            window=(10.0, 10.0),  # 回执确认的精确生效时刻
            receipt=True,
        )
        reads = [
            # alice 在边界时刻(含)之前观察:旧一代
            ReadRecord("R1", "alice", "doc", frozenset({"title", "body"}), 0.0, 10.0),
            # bob 在边界时刻(含)之后观察:新一代
            ReadRecord("R2", "bob", "doc", frozenset({"title", "body", "salary"}), 10.0, 20.0),
        ]
        result = Auditor(make_input([change], reads)).run()
        self.assertEqual(result.verdict, Verdict.CONSISTENT)
        self.assertEqual(result.change_times["C1"], 10.0)
        self.assertEqual(result.read_witnesses["R1"].observed_at <= 10.0, True)
        self.assertGreaterEqual(result.read_witnesses["R2"].observed_at, 10.0)


class TestRejectedResults(unittest.TestCase):
    """所有顺序均矛盾 -> CONFLICT,并定位冲突记录。"""

    def test_stitched_generations_rejected(self):
        """返回字段横跨两代授权:没有任何单一时刻的状态能解释。"""
        change = PermissionChange(
            id="C1",
            version=1,
            group="staff",
            resource="doc",
            grant=frozenset({"salary"}),
            revoke=frozenset({"body"}),
            window=(5.0, 6.0),
            receipt=True,
        )
        # 旧一代允许 body 不允许 salary;新一代允许 salary 不允许 body。
        # 返回 {body, salary} 只能是把两代拼起来的结果。
        read = ReadRecord("R1", "alice", "doc", frozenset({"body", "salary"}), 0.0, 20.0)
        result = Auditor(make_input([change], [read])).run()
        self.assertEqual(result.verdict, Verdict.CONFLICT)
        self.assertEqual(result.conflicting_reads, ("R1",))
        self.assertEqual(result.involved_changes, ("C1",))

    def test_declared_version_not_respected(self):
        """读取声明版本 1,但其时段完全在版本 1 变更窗口之前。"""
        change = PermissionChange(
            id="C1",
            version=1,
            group="staff",
            resource="doc",
            grant=frozenset({"salary"}),
            window=(10.0, 12.0),
            receipt=True,
        )
        read = ReadRecord(
            "R1", "alice", "doc", frozenset({"title", "body", "salary"}),
            0.0, 4.0, authz_version=1,
        )
        result = Auditor(make_input([change], [read])).run()
        self.assertEqual(result.verdict, Verdict.CONFLICT)
        self.assertEqual(result.conflicting_reads, ("R1",))

    def test_never_granted_field_rejected(self):
        """返回了任何一代都未授权的字段。"""
        read = ReadRecord("R1", "alice", "doc", frozenset({"title", "ssn"}), 0.0, 5.0)
        result = Auditor(make_input([], [read])).run()
        self.assertEqual(result.verdict, Verdict.CONFLICT)
        self.assertEqual(result.conflicting_reads, ("R1",))
        self.assertEqual(result.involved_changes, ())

    def test_minimal_conflict_localization(self):
        """好记录与坏记录交错时,冲突集合只包含坏记录。"""
        change = PermissionChange(
            id="C1",
            version=1,
            group="staff",
            resource="doc",
            grant=frozenset({"salary"}),
            revoke=frozenset({"body"}),
            window=(5.0, 6.0),
            receipt=True,
        )
        reads = [
            ReadRecord("R-good", "alice", "doc", frozenset({"title", "body"}), 0.0, 4.0),
            ReadRecord("R-bad", "alice", "doc", frozenset({"title", "body", "salary"}), 0.0, 20.0),
            ReadRecord("R-good-2", "bob", "doc", frozenset({"title", "salary"}), 7.0, 9.0),
        ]
        result = Auditor(make_input([change], reads)).run()
        self.assertEqual(result.verdict, Verdict.CONFLICT)
        self.assertEqual(result.conflicting_reads, ("R-bad",))


class TestMissingReceiptEscalation(unittest.TestCase):
    """影响结论的回执缺失 -> MANUAL_REVIEW;不影响则照常判定。"""

    def test_consistent_but_depends_on_unreceipted_change(self):
        change = PermissionChange(
            id="C1",
            version=1,
            group="staff",
            resource="doc",
            grant=frozenset({"salary"}),
            window=(10.0, 12.0),
            receipt=False,  # 缺失生效回执
        )
        # 只有变更生效后才能解释这次返回,而生效时刻无回执确认
        read = ReadRecord("R1", "alice", "doc", frozenset({"title", "body", "salary"}), 11.0, 20.0)
        result = Auditor(make_input([change], [read])).run()
        self.assertEqual(result.verdict, Verdict.MANUAL_REVIEW)
        self.assertEqual(result.pending_receipts, ("C1",))

    def test_conflict_depending_on_unreceipted_change(self):
        change = PermissionChange(
            id="C1",
            version=1,
            group="staff",
            resource="doc",
            grant=frozenset({"salary"}),
            revoke=frozenset({"body"}),
            window=(5.0, 6.0),
            receipt=False,
        )
        read = ReadRecord("R1", "alice", "doc", frozenset({"body", "salary"}), 0.0, 20.0)
        result = Auditor(make_input([change], [read])).run()
        self.assertEqual(result.verdict, Verdict.MANUAL_REVIEW)
        self.assertEqual(result.conflicting_reads, ("R1",))
        self.assertEqual(result.pending_receipts, ("C1",))

    def test_unreceipted_change_not_affecting_conclusion(self):
        """回执缺失但读取时段与变更窗口不重叠,结论不受影响。"""
        change = PermissionChange(
            id="C1",
            version=1,
            group="staff",
            resource="doc",
            grant=frozenset({"salary"}),
            window=(10.0, 12.0),
            receipt=False,
        )
        read = ReadRecord("R1", "alice", "doc", frozenset({"title", "body"}), 0.0, 5.0)
        result = Auditor(make_input([change], [read])).run()
        self.assertEqual(result.verdict, Verdict.CONSISTENT)
        self.assertEqual(result.pending_receipts, ())


class TestVersionOrdering(unittest.TestCase):
    """多次变更必须按版本顺序生效。"""

    def test_version_order_respected(self):
        changes = [
            PermissionChange(
                id="C1", version=1, group="staff", resource="doc",
                grant=frozenset({"salary"}), window=(10.0, 11.0), receipt=True,
            ),
            PermissionChange(
                id="C2", version=2, group="staff", resource="doc",
                revoke=frozenset({"salary"}), window=(20.0, 21.0), receipt=True,
            ),
        ]
        reads = [
            # 版本 1 状态:salary 可见
            ReadRecord("R1", "alice", "doc", frozenset({"title", "body", "salary"}), 12.0, 19.0, authz_version=1),
            # 版本 2 状态:salary 已撤回
            ReadRecord("R2", "alice", "doc", frozenset({"title", "body"}), 22.0, 30.0, authz_version=2),
        ]
        result = Auditor(make_input(changes, reads)).run()
        self.assertEqual(result.verdict, Verdict.CONSISTENT)
        self.assertLess(result.change_times["C1"], result.change_times["C2"])
        self.assertEqual(result.read_witnesses["R1"].state_version, 1)
        self.assertEqual(result.read_witnesses["R2"].state_version, 2)

    def test_impossible_version_sequence_rejected(self):
        """读取声称在版本 2 之后观察到版本 1 才允许的字段。"""
        changes = [
            PermissionChange(
                id="C1", version=1, group="staff", resource="doc",
                grant=frozenset({"salary"}), window=(10.0, 11.0), receipt=True,
            ),
            PermissionChange(
                id="C2", version=2, group="staff", resource="doc",
                revoke=frozenset({"salary"}), window=(12.0, 13.0), receipt=True,
            ),
        ]
        # 时段在 C2 窗口之后,却返回了版本 2 已撤回的 salary
        read = ReadRecord("R1", "alice", "doc", frozenset({"title", "body", "salary"}), 14.0, 20.0)
        result = Auditor(make_input(changes, [read])).run()
        self.assertEqual(result.verdict, Verdict.CONFLICT)
        self.assertEqual(result.conflicting_reads, ("R1",))


if __name__ == "__main__":
    unittest.main()
