"""交错与控制流：分支合流、未知所有权、部分分支建立等 REVIEW 场景。"""

import unittest

from admission import (
    Branch,
    CreateFile,
    EstablishDirectory,
    LockDirectory,
    MoveObject,
    Sequence,
    UnknownBranch,
    Verdict,
    admit,
)
from admission.analyzer import (
    R_BRANCH_DIR_STATE_DIVERGED,
    R_BRANCH_LOCK_OWNER_DIVERGED,
    R_BRANCH_OBJECT_DIVERGED,
    R_BRANCH_OWNER_UNKNOWN,
    R_FILE_OK,
    R_LOCK_OK,
    R_MOVE_OK,
)


def _locked(did, owner="alice", path="/tmp/a"):
    return Sequence([
        EstablishDirectory(id=did, path=path, owner=owner),
        LockDirectory(directory_id=did, owner=owner),
    ])


class UnknownBranchTest(unittest.TestCase):
    def test_unknown_branch_ownership_forces_review_on_create(self):
        program = Sequence([
            EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
            LockDirectory(directory_id="d1", owner="alice"),
            UnknownBranch(affected_directory_ids=["d1"]),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
        ])
        report = admit(program)
        self.assertEqual(report.verdict, Verdict.REVIEW)
        entry = report.entries[-1]
        self.assertEqual(entry.verdict, Verdict.REVIEW)
        self.assertEqual(entry.reason, R_BRANCH_OWNER_UNKNOWN)
        self.assertEqual(entry.basis_directory_id, "d1")

    def test_unknown_branch_ownership_forces_review_on_move_target(self):
        program = Sequence([
            _locked("d1"),
            _locked("d2", path="/tmp/b"),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
            UnknownBranch(affected_directory_ids=["d2"]),
            MoveObject(object_id="f1", target_directory_id="d2"),
        ])
        report = admit(program)
        self.assertEqual(report.entries[-1].reason,
                         R_BRANCH_OWNER_UNKNOWN)
        self.assertEqual(report.verdict, Verdict.REVIEW)

    def test_unknown_branch_affected_object_forces_review_on_move(self):
        program = Sequence([
            _locked("d1"),
            _locked("d2", path="/tmp/b"),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
            UnknownBranch(affected_object_ids=["f1"]),
            MoveObject(object_id="f1", target_directory_id="d2"),
        ])
        report = admit(program)
        self.assertEqual(report.entries[-1].reason,
                         R_BRANCH_OBJECT_DIVERGED)
        self.assertEqual(report.verdict, Verdict.REVIEW)

    def test_unknown_branch_declaring_unknown_directory_id(self):
        # 未知分支提到一个从未建立的目录：之后按未知所有权对待，访问即 REVIEW
        report = admit(Sequence([
            UnknownBranch(affected_directory_ids=["dX"]),
            CreateFile(id="f1", name="a.tmp", directory_id="dX",
                       creator="alice"),
        ]))
        self.assertEqual(report.entries[-1].verdict, Verdict.REVIEW)
        self.assertEqual(report.entries[-1].reason,
                         R_BRANCH_OWNER_UNKNOWN)


class BranchMergeTest(unittest.TestCase):
    def test_one_branch_locks_other_does_not_merges_to_review(self):
        program = Sequence([
            EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
            Branch(alternatives=[
                LockDirectory(directory_id="d1", owner="alice"),
                Sequence([]),  # 该分支不锁定
            ]),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
        ])
        report = admit(program)
        self.assertEqual(report.verdict, Verdict.REVIEW)
        create_entry = report.entries[-1]
        self.assertEqual(create_entry.reason, R_BRANCH_DIR_STATE_DIVERGED)

    def test_diverging_lock_owners_force_review(self):
        program = Sequence([
            EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
            Branch(alternatives=[
                LockDirectory(directory_id="d1", owner="alice"),
                Sequence([
                    # 另一分支以不同身份锁定（静态上视为不同锁定结论）
                    LockDirectory(directory_id="d1", owner="bob"),
                ]),
            ]),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
        ])
        report = admit(program)
        # 分支 1 的锁定实际上会被判定为所有者不匹配（DENY），
        # 汇合后状态发散，创建保留待复核；整体结论为 DENY（含确定性违规）。
        self.assertEqual(report.verdict, Verdict.DENY)
        self.assertEqual(report.entries[-1].verdict, Verdict.REVIEW)
        self.assertIn(
            report.entries[-1].reason,
            (R_BRANCH_LOCK_OWNER_DIVERGED, R_BRANCH_DIR_STATE_DIVERGED),
        )

    def test_partial_branch_establishment_is_review(self):
        program = Sequence([
            Branch(alternatives=[
                Sequence([
                    EstablishDirectory(id="d1", path="/tmp/a",
                                       owner="alice"),
                    LockDirectory(directory_id="d1", owner="alice"),
                ]),
                Sequence([]),  # 另一分支根本没有该目录
            ]),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
        ])
        report = admit(program)
        self.assertEqual(report.entries[-1].verdict, Verdict.REVIEW)
        self.assertEqual(report.entries[-1].reason,
                         R_BRANCH_DIR_STATE_DIVERGED)

    def test_branches_move_object_to_different_dirs_then_move_review(self):
        program = Sequence([
            _locked("d1"),
            _locked("d2", path="/tmp/b"),
            _locked("d3", path="/tmp/c"),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
            Branch(alternatives=[
                MoveObject(object_id="f1", target_directory_id="d2"),
                MoveObject(object_id="f1", target_directory_id="d3"),
            ]),
            # 汇合后 f1 位置跨分支不一致，再移动必须保留待复核
            MoveObject(object_id="f1", target_directory_id="d1"),
        ])
        report = admit(program)
        last = report.entries[-1]
        self.assertEqual(last.verdict, Verdict.REVIEW)

    def test_deny_inside_one_branch_dominates_report(self):
        program = Branch(alternatives=[
            Sequence([
                EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
                LockDirectory(directory_id="d1", owner="alice"),
                CreateFile(id="f1", name="a.tmp", directory_id="d1",
                           creator="alice"),
            ]),
            Sequence([
                EstablishDirectory(id="d2", path="/tmp/b", owner="alice"),
                # 未锁定直接创建 -> DENY
                CreateFile(id="f2", name="b.tmp", directory_id="d2",
                           creator="alice"),
            ]),
        ])
        report = admit(program)
        self.assertEqual(report.verdict, Verdict.DENY)

    def test_both_branches_lock_consistently_then_allow_after_merge(self):
        program = Sequence([
            EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
            Branch(alternatives=[
                LockDirectory(directory_id="d1", owner="alice"),
                LockDirectory(directory_id="d1", owner="alice"),
            ]),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
        ])
        report = admit(program)
        # 建立动作 REVIEW + 其余 ALLOW，整体 REVIEW；汇合后的创建确实放行
        self.assertEqual(report.verdict, Verdict.REVIEW)
        self.assertEqual(report.entries[1].reason, R_LOCK_OK)
        self.assertEqual(report.entries[2].reason, R_LOCK_OK)
        self.assertEqual(report.entries[-1].reason, R_FILE_OK)

    def test_nested_branch_paths_in_audit(self):
        program = Branch(alternatives=[
            Branch(alternatives=[
                Sequence([
                    EstablishDirectory(id="d1", path="/tmp/a",
                                       owner="alice"),
                    LockDirectory(directory_id="d1", owner="alice"),
                ]),
                Sequence([
                    EstablishDirectory(id="d2", path="/tmp/b",
                                       owner="alice"),
                    LockDirectory(directory_id="d2", owner="alice"),
                ]),
            ]),
            Sequence([
                EstablishDirectory(id="d3", path="/tmp/c", owner="alice"),
                LockDirectory(directory_id="d3", owner="alice"),
            ]),
        ])
        report = admit(program)
        paths = [e.branch_path for e in report.entries]
        self.assertIn((0, 0), paths)
        self.assertIn((0, 1), paths)
        self.assertIn((1,), paths)

    def test_move_allowed_after_consistent_branch_then_merged(self):
        program = Sequence([
            _locked("d1"),
            _locked("d2", path="/tmp/b"),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
            Branch(alternatives=[
                Sequence([]),
                Sequence([]),
            ]),
            MoveObject(object_id="f1", target_directory_id="d2"),
        ])
        report = admit(program)
        self.assertEqual(report.entries[-1].reason, R_MOVE_OK)
        self.assertEqual(report.verdict, Verdict.REVIEW)


if __name__ == "__main__":
    unittest.main()
