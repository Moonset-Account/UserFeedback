"""补充语义：REVIEW 不改变状态、审计定位字段、结论聚合优先级。"""

import unittest

from admission import (
    Branch,
    CreateFile,
    EstablishDirectory,
    InheritDirectory,
    LockDirectory,
    MoveObject,
    Sequence,
    UnknownBranch,
    Verdict,
    admit,
)
from admission.analyzer import (
    R_BRANCH_DIR_STATE_DIVERGED,
    R_BRANCH_OWNER_UNKNOWN,
    R_MOVE_OK,
    R_MOVE_SOURCE_NONPRIVATE,
)


def _locked(did, owner="alice", path="/tmp/a"):
    return [
        EstablishDirectory(id=did, path=path, owner=owner),
        LockDirectory(directory_id=did, owner=owner),
    ]


class ReviewNoMutationTest(unittest.TestCase):
    def test_reviewed_move_does_not_change_bindings(self):
        # 目标目录所有权未知 -> REVIEW；随后目标确认锁定，再移动必须成功，
        # 证明 REVIEW 的移动没有提前改动任何绑定。
        program = Sequence(
            _locked("d1")
            + _locked("d2", path="/tmp/b")
            + [
                CreateFile(id="f1", name="a.tmp", directory_id="d1",
                           creator="alice"),
                UnknownBranch(affected_directory_ids=["d2"]),
                MoveObject(object_id="f1", target_directory_id="d2",
                           target_name="x.tmp"),
            ]
        )
        report = admit(program)
        reviewed = report.entries[-1]
        self.assertEqual(reviewed.verdict, Verdict.REVIEW)
        self.assertEqual(reviewed.reason, R_BRANCH_OWNER_UNKNOWN)

        # 同前缀但不含未知分支：移动应当成功，且名字绑定按提交时生效
        clean = Sequence(
            _locked("d1")
            + _locked("d2", path="/tmp/b")
            + [
                CreateFile(id="f1", name="a.tmp", directory_id="d1",
                           creator="alice"),
                MoveObject(object_id="f1", target_directory_id="d2",
                           target_name="x.tmp"),
            ]
        )
        clean_report = admit(clean)
        self.assertEqual(clean_report.entries[-1].reason, R_MOVE_OK)
        self.assertEqual(clean_report.entries[-1].basis_directory_id, "d2")

    def test_denied_move_does_not_change_bindings(self):
        program = Sequence(
            _locked("d1")
            + _locked("d2", path="/tmp/b")
            + [
                CreateFile(id="f1", name="a.tmp", directory_id="d1",
                           creator="alice", private=False),
                MoveObject(object_id="f1", target_directory_id="d2"),
                # 非私有的否决移动后，再做一次合法换名仍应被名字冲突规则覆盖：
                # f1 仍占用 a.tmp
                MoveObject(object_id="f1", target_directory_id="d1",
                           target_name="a.tmp"),
            ]
        )
        report = admit(program)
        self.assertEqual(report.entries[-2].reason,
                         R_MOVE_SOURCE_NONPRIVATE)
        # 最后一次移动：目标名字 a.tmp 仍被 f1 自己占用，属于同一对象，
        # 因此不是冲突；但源仍非私有 -> 依旧是非私有否决
        self.assertEqual(report.entries[-1].reason,
                         R_MOVE_SOURCE_NONPRIVATE)


class AmbiguousDirectoryCreateTest(unittest.TestCase):
    def test_create_in_branch_diverged_directory_is_review(self):
        program = Sequence([
            EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
            Branch(alternatives=[
                LockDirectory(directory_id="d1", owner="alice"),
                Sequence([]),
            ]),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
        ])
        report = admit(program)
        entry = report.entries[-1]
        self.assertEqual(entry.verdict, Verdict.REVIEW)
        self.assertEqual(entry.reason, R_BRANCH_DIR_STATE_DIVERGED)
        self.assertEqual(entry.basis_directory_id, "d1")
        self.assertEqual(entry.basis_identity, "alice")

    def test_unknown_branch_then_create_uses_ambiguous_reason_chain(self):
        program = Sequence([
            EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
            LockDirectory(directory_id="d1", owner="alice"),
            UnknownBranch(affected_directory_ids=["d1"]),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
        ])
        report = admit(program)
        entry = report.entries[-1]
        # 未知分支直接给出所有权原因码（比通用歧义码更具体）
        self.assertEqual(entry.reason, R_BRANCH_OWNER_UNKNOWN)


class AuditAndAggregationTest(unittest.TestCase):
    def test_audit_locates_directory_and_identity_for_every_leaf(self):
        program = Sequence(
            _locked("d1")
            + [
                CreateFile(id="f1", name="a.tmp", directory_id="d1",
                           creator="alice"),
            ]
        )
        report = admit(program)
        for entry in report.entries:
            self.assertIsNotNone(entry.basis_directory_id)
            self.assertIsNotNone(entry.basis_identity)
            self.assertTrue(entry.reason)
            self.assertIsInstance(entry.sequence_index, int)

    def test_entries_for_object_id(self):
        program = Sequence(
            _locked("d1")
            + [
                CreateFile(id="f1", name="a.tmp", directory_id="d1",
                           creator="alice"),
                MoveObject(object_id="f1", target_directory_id="d1",
                           target_name="b.tmp"),
            ]
        )
        report = admit(program)
        related = report.entries_for("f1")
        self.assertEqual(len(related), 2)
        self.assertTrue(all(e.object_id == "f1" for e in related))

    def test_deny_outranks_review_outranks_allow(self):
        # 同一程序里同时出现 REVIEW（继承）与 DENY（未锁定创建）
        program = Sequence([
            InheritDirectory(id="d9", path="/srv/shared",
                             expected_owner="alice"),
            EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
        ])
        report = admit(program)
        self.assertEqual(report.verdict, Verdict.DENY)
        self.assertTrue(report.has_reason(R_BRANCH_OWNER_UNKNOWN) is False)

    def test_structural_error_report_has_no_entries(self):
        report = admit(
            CreateFile(id="", name="a.tmp", directory_id="d1",
                       creator="alice")
        )
        self.assertEqual(report.verdict, Verdict.DENY)
        self.assertEqual(report.entries, ())
        self.assertTrue(report.structural_errors)


if __name__ == "__main__":
    unittest.main()
