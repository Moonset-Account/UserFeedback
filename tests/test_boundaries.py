"""边界与负向判定：未锁定、所有权不匹配、非私有源、路径名复用等。"""

import unittest

from admission import (
    Branch,
    CreateFile,
    EstablishDirectory,
    InheritDirectory,
    LockDirectory,
    MoveObject,
    Sequence,
    Verdict,
    admit,
)
from admission.analyzer import (
    R_DIR_UNKNOWN,
    R_FILE_DIR_INHERITED,
    R_FILE_DIR_NOT_LOCKED,
    R_FILE_DIR_UNKNOWN,
    R_FILE_DUPLICATE,
    R_INHERIT_REVIEW,
    R_LOCK_ALREADY,
    R_LOCK_INHERIT_UNVERIFIED,
    R_LOCK_NOT_ESTABLISHED,
    R_LOCK_OWNER_MISMATCH,
    R_MOVE_NAME_CONFLICT,
    R_MOVE_OK,
    R_MOVE_SOURCE_NONPRIVATE,
    R_MOVE_SOURCE_UNKNOWN,
    R_MOVE_TARGET_INHERITED,
    R_MOVE_TARGET_NOT_LOCKED,
    R_MOVE_TARGET_UNKNOWN,
    R_STRUCTURAL,
)


def _locked_dir(did="d1", owner="alice", path="/tmp/a"):
    return [
        EstablishDirectory(id=did, path=path, owner=owner),
        LockDirectory(directory_id=did, owner=owner),
    ]


class DirectoryAuthorizationTest(unittest.TestCase):
    def test_create_before_lock_is_denied(self):
        report = admit(Sequence([
            EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
        ]))
        self.assertEqual(report.verdict, Verdict.DENY)
        entry = report.entries[-1]
        self.assertEqual(entry.verdict, Verdict.DENY)
        self.assertEqual(entry.reason, R_FILE_DIR_NOT_LOCKED)
        self.assertEqual(entry.basis_directory_id, "d1")
        self.assertEqual(entry.basis_identity, "alice")

    def test_lock_without_establish_is_denied(self):
        report = admit(LockDirectory(directory_id="ghost", owner="alice"))
        self.assertEqual(report.verdict, Verdict.DENY)
        self.assertEqual(report.entries[0].reason, R_LOCK_NOT_ESTABLISHED)

    def test_lock_owner_mismatch_rejects_directory_and_blocks_later_use(self):
        report = admit(Sequence([
            EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
            LockDirectory(directory_id="d1", owner="mallory"),
            LockDirectory(directory_id="d1", owner="alice"),
            CreateFile(id="f1", name="a.tmp", directory_id="d1",
                       creator="alice"),
        ]))
        self.assertEqual(report.verdict, Verdict.DENY)
        self.assertEqual(report.entries[1].reason, R_LOCK_OWNER_MISMATCH)
        # 被拒绝的目录即便后来用正确身份锁定也不恢复
        from admission.analyzer import R_LOCK_REJECTED, R_FILE_DIR_REJECTED
        self.assertEqual(report.entries[2].reason, R_LOCK_REJECTED)
        self.assertEqual(report.entries[3].reason, R_FILE_DIR_REJECTED)

    def test_double_lock_denied(self):
        report = admit(Sequence(
            _locked_dir()
            + [LockDirectory(directory_id="d1", owner="alice")]
        ))
        self.assertEqual(report.entries[-1].reason, R_LOCK_ALREADY)

    def test_duplicate_directory_id_denied(self):
        report = admit(Sequence([
            EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
            EstablishDirectory(id="d1", path="/tmp/b", owner="alice"),
        ]))
        from admission.analyzer import R_DIR_DUPLICATE
        self.assertEqual(report.entries[-1].reason, R_DIR_DUPLICATE)
        self.assertEqual(report.verdict, Verdict.DENY)


class InheritedDirectoryTest(unittest.TestCase):
    def test_inherit_itself_is_review_and_create_inside_denied(self):
        report = admit(Sequence([
            InheritDirectory(id="d9", path="/srv/shared",
                             expected_owner="alice"),
            CreateFile(id="f1", name="a.tmp", directory_id="d9",
                       creator="alice"),
        ]))
        self.assertEqual(report.entries[0].reason, R_INHERIT_REVIEW)
        self.assertEqual(report.entries[0].verdict, Verdict.REVIEW)
        self.assertEqual(report.entries[1].reason, R_FILE_DIR_INHERITED)
        self.assertEqual(report.verdict, Verdict.DENY)

    def test_inherited_lock_with_wrong_owner_denied(self):
        report = admit(Sequence([
            InheritDirectory(id="d9", path="/srv/shared",
                             expected_owner="alice"),
            LockDirectory(directory_id="d9", owner="mallory"),
        ]))
        self.assertEqual(report.entries[-1].reason,
                         R_LOCK_INHERIT_UNVERIFIED)
        self.assertEqual(report.verdict, Verdict.DENY)

    def test_inherited_lock_with_expected_owner_then_allowed(self):
        report = admit(Sequence([
            InheritDirectory(id="d9", path="/srv/shared",
                             expected_owner="alice"),
            LockDirectory(directory_id="d9", owner="alice"),
            CreateFile(id="f1", name="a.tmp", directory_id="d9",
                       creator="alice"),
        ]))
        # REVIEW（继承待确认）+ 两个 ALLOW，整体结论 REVIEW
        self.assertEqual(report.verdict, Verdict.REVIEW)
        self.assertEqual(report.entries[1].verdict, Verdict.ALLOW)
        self.assertEqual(report.entries[2].verdict, Verdict.ALLOW)

    def test_move_into_unconfirmed_inherited_directory_denied(self):
        report = admit(Sequence(
            _locked_dir("d1")
            + [
                InheritDirectory(id="d9", path="/srv/shared",
                                 expected_owner="alice"),
                CreateFile(id="f1", name="a.tmp", directory_id="d1",
                           creator="alice"),
                MoveObject(object_id="f1", target_directory_id="d9"),
            ]
        ))
        self.assertEqual(report.entries[-1].reason,
                         R_MOVE_TARGET_INHERITED)
        self.assertEqual(report.verdict, Verdict.DENY)


class MoveSourceTest(unittest.TestCase):
    def test_move_unknown_source_denied(self):
        report = admit(Sequence(
            _locked_dir()
            + [MoveObject(object_id="ghost", target_directory_id="d1")]
        ))
        self.assertEqual(report.entries[-1].reason, R_MOVE_SOURCE_UNKNOWN)

    def test_move_nonprivate_source_denied(self):
        report = admit(Sequence(
            _locked_dir("d1")
            + _locked_dir("d2", path="/tmp/b")
            + [
                CreateFile(id="f1", name="a.tmp", directory_id="d1",
                           creator="alice", private=False),
                MoveObject(object_id="f1", target_directory_id="d2"),
            ]
        ))
        self.assertEqual(report.entries[-1].reason,
                         R_MOVE_SOURCE_NONPRIVATE)
        self.assertEqual(report.verdict, Verdict.DENY)

    def test_move_to_unknown_or_unlocked_target_denied(self):
        report = admit(Sequence(
            _locked_dir("d1")
            + [
                CreateFile(id="f1", name="a.tmp", directory_id="d1",
                           creator="alice"),
                MoveObject(object_id="f1", target_directory_id="ghost"),
            ]
        ))
        self.assertEqual(report.entries[-1].reason, R_MOVE_TARGET_UNKNOWN)

        report2 = admit(Sequence(
            _locked_dir("d1")
            + [
                EstablishDirectory(id="d2", path="/tmp/b", owner="alice"),
                CreateFile(id="f1", name="a.tmp", directory_id="d1",
                           creator="alice"),
                MoveObject(object_id="f1", target_directory_id="d2"),
            ]
        ))
        self.assertEqual(report2.entries[-1].reason,
                         R_MOVE_TARGET_NOT_LOCKED)

    def test_move_name_conflict_denied_and_state_unchanged(self):
        report = admit(Sequence(
            _locked_dir("d1")
            + [
                CreateFile(id="f1", name="a.tmp", directory_id="d1",
                           creator="alice"),
                CreateFile(id="f2", name="b.tmp", directory_id="d1",
                           creator="alice"),
                MoveObject(object_id="f1", target_directory_id="d1",
                           target_name="b.tmp"),
            ]
        ))
        conflict = report.entries[-1]
        self.assertEqual(conflict.reason, R_MOVE_NAME_CONFLICT)
        # 被拒绝的移动不改变绑定：f1 仍可按原名再次移动成功
        report2 = admit(Sequence(
            _locked_dir("d1")
            + [
                CreateFile(id="f1", name="a.tmp", directory_id="d1",
                           creator="alice"),
                CreateFile(id="f2", name="b.tmp", directory_id="d1",
                           creator="alice"),
                MoveObject(object_id="f1", target_directory_id="d1",
                           target_name="b.tmp"),
                MoveObject(object_id="f1", target_directory_id="d1",
                           target_name="c.tmp"),
            ]
        ))
        self.assertEqual(report2.entries[-1].reason, R_MOVE_OK)


class PathReuseTest(unittest.TestCase):
    def test_reused_path_name_does_not_inherit_authorization(self):
        # f1 是授权目录中的授权对象；移动腾出 a.tmp 后，新建的 f3 复用同名，
        # 必须按全新对象重新走创建准入，不能继承 f1 的任何状态。
        report = admit(Sequence(
            _locked_dir("d1")
            + [
                CreateFile(id="f1", name="a.tmp", directory_id="d1",
                           creator="alice"),
                MoveObject(object_id="f1", target_directory_id="d1",
                           target_name="moved.tmp"),
                CreateFile(id="f3", name="a.tmp", directory_id="d1",
                           creator="bob"),
            ]
        ))
        # 建立动作 REVIEW + 其余 ALLOW；关键是新对象按自身身份独立准入
        self.assertEqual(report.verdict, Verdict.REVIEW)
        new_file = report.entries[-1]
        self.assertEqual(new_file.object_id, "f3")
        self.assertEqual(new_file.basis_identity, "bob")
        # f3 是新对象：对 f1 的审计不会包含 f3
        self.assertEqual(
            tuple(e.object_id for e in report.entries_for("f1")),
            ("f1", "f1"),
        )

    def test_create_with_reused_name_in_unlocked_dir_still_denied(self):
        # 旧名字曾在授权目录中存在，不代表新目录里的同名对象获得授权
        report = admit(Sequence(
            _locked_dir("d1")
            + [
                CreateFile(id="f1", name="a.tmp", directory_id="d1",
                           creator="alice"),
                EstablishDirectory(id="d2", path="/tmp/b", owner="alice"),
                CreateFile(id="f2", name="a.tmp", directory_id="d2",
                           creator="alice"),
            ]
        ))
        self.assertEqual(report.entries[-1].reason,
                         R_FILE_DIR_NOT_LOCKED)

    def test_duplicate_object_id_denied_even_with_different_name(self):
        report = admit(Sequence(
            _locked_dir("d1")
            + [
                CreateFile(id="f1", name="a.tmp", directory_id="d1",
                           creator="alice"),
                CreateFile(id="f1", name="b.tmp", directory_id="d1",
                           creator="alice"),
            ]
        ))
        self.assertEqual(report.entries[-1].reason, R_FILE_DUPLICATE)


class StructuralValidationTest(unittest.TestCase):
    def test_bad_inputs_reported_as_structural_errors(self):
        report = admit(EstablishDirectory(id="", path="/tmp/a", owner="alice"))
        self.assertEqual(report.verdict, Verdict.DENY)
        self.assertTrue(report.structural_errors)

        report = admit(Sequence([
            CreateFile(id="f1", name="", directory_id="d1",
                       creator="alice"),
        ]))
        self.assertTrue(report.structural_errors)

        report = admit("not-an-operation")
        self.assertTrue(report.structural_errors)

        report = admit(Branch(alternatives=[]))
        self.assertTrue(report.structural_errors)

    def test_unknown_directory_constant_is_only_for_missing_dir_state(self):
        # 常量存在且语义稳定（供外部对照 reason 使用）
        self.assertEqual(R_DIR_UNKNOWN, "DIRECTORY_UNKNOWN")
        self.assertEqual(R_STRUCTURAL, "STRUCTURAL_ERROR")


if __name__ == "__main__":
    unittest.main()
