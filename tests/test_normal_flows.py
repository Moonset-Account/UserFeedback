"""正常场景：建立 -> 锁定 -> 创建 -> 换名/移动 的完整准入路径。"""

import unittest

from admission import (
    Branch,
    CreateFile,
    EstablishDirectory,
    LockDirectory,
    MoveObject,
    Sequence,
    Verdict,
    admit,
)
from admission.analyzer import (
    R_DIR_ESTABLISHED,
    R_FILE_OK,
    R_LOCK_OK,
    R_MOVE_OK,
)


class NormalFlowTest(unittest.TestCase):
    def test_full_happy_path(self):
        program = Sequence([
            EstablishDirectory(id="d1", path="/tmp/work", owner="alice"),
            LockDirectory(directory_id="d1", owner="alice"),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
            MoveObject(object_id="f1", target_directory_id="d1",
                       target_name="b.tmp", mover="alice"),
        ])
        report = admit(program)
        # 建立动作本身为 REVIEW（未锁定前不授权），后续全部 ALLOW，
        # 因此整体结论是 REVIEW 而非 DENY。
        self.assertEqual(report.verdict, Verdict.REVIEW, report.entries)
        self.assertEqual(len(report.entries), 4)
        self.assertEqual(
            [e.verdict for e in report.entries],
            [Verdict.REVIEW, Verdict.ALLOW, Verdict.ALLOW, Verdict.ALLOW],
        )
        reasons = report.reasons()
        self.assertEqual(reasons[0], R_DIR_ESTABLISHED)
        self.assertEqual(reasons[1], R_LOCK_OK)
        self.assertEqual(reasons[2], R_FILE_OK)
        self.assertEqual(reasons[3], R_MOVE_OK)

    def test_cross_directory_move_keeps_creator_identity(self):
        program = Sequence([
            EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
            EstablishDirectory(id="d2", path="/tmp/b", owner="alice"),
            LockDirectory(directory_id="d1", owner="alice"),
            LockDirectory(directory_id="d2", owner="alice"),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
            MoveObject(object_id="f1", target_directory_id="d2",
                       mover="alice"),
        ])
        report = admit(program)
        self.assertEqual(report.verdict, Verdict.REVIEW)
        move_entry = report.entries[-1]
        # 审计按目标目录与锁定身份定位
        self.assertEqual(move_entry.basis_directory_id, "d2")
        self.assertEqual(move_entry.basis_identity, "alice")
        self.assertEqual(move_entry.object_id, "f1")
        self.assertIn("创建身份仍为 'alice'", move_entry.detail)

    def test_accepts_plain_list(self):
        report = admit([
            EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
            LockDirectory(directory_id="d1", owner="alice"),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
        ])
        self.assertEqual(report.verdict, Verdict.REVIEW)

    def test_identical_branches_both_allowed(self):
        body = Sequence([
            EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
            LockDirectory(directory_id="d1", owner="alice"),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
        ])
        report = admit(Branch(alternatives=[body, body]))
        self.assertEqual(report.verdict, Verdict.REVIEW)
        # 每个分支内的具体操作都各自有审计条目
        self.assertEqual(len(report.entries), 6)
        self.assertEqual(
            [e.branch_path for e in report.entries[:3]],
            [(0,), (0,), (0,)],
        )
        self.assertEqual(
            [e.branch_path for e in report.entries[3:]],
            [(1,), (1,), (1,)],
        )

    def test_sequence_index_is_preorder_per_concrete_operation(self):
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
        self.assertEqual(
            [e.sequence_index for e in report.entries],
            [1, 2, 3, 4],
        )


if __name__ == "__main__":
    unittest.main()
