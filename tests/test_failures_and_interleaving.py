"""失败路径：无有效密钥停发、原因链回溯、悬空引用、选钥与交错场景。"""

import unittest
from datetime import datetime

from key_coverage import (
    MemberKind,
    Status,
    check_key_coverage,
)

from tests.builders import (
    T0,
    build_snapshot,
    build_store,
    group,
    key,
    person,
    ref,
)


class MissingKeyTests(unittest.TestCase):
    def test_eligible_person_without_keys_halts_email(self):
        snap = build_snapshot(
            "v1",
            groups=(
                group(
                    "g-root",
                    [
                        ref("p-ok", MemberKind.PERSON),
                        ref("p-nokey", MemberKind.PERSON),
                    ],
                    name="根组",
                ),
            ),
            persons=(
                person("p-ok", (key("k-ok"),), name="有钥的人"),
                person("p-nokey", (), name="没钥的人"),
            ),
        )
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=T0,
        )
        self.assertEqual(result.status, Status.PREPARING)
        self.assertEqual(len(result.missing_key_failures), 1)
        failure = result.missing_key_failures[0]
        self.assertEqual(failure.person_id, "p-nokey")
        self.assertEqual(failure.key_diagnostics, ())

    def test_reason_chain_walks_back_to_original_group_name(self):
        # 根组 -> 嵌套组（带密级边）-> 没钥的人；原因链必须含根组展示名与整条链。
        inner = group(
            "g-inner",
            [ref("p-nokey", MemberKind.PERSON)],
            name="内层组",
        )
        root = group(
            "g-root",
            [ref("g-inner", MemberKind.GROUP, min_level="secret")],
            name="原始通知组",
        )
        snap = build_snapshot(
            "v1",
            groups=(root, inner),
            persons=(person("p-nokey", (), name="没钥的人"),),
        )
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="secret",
            scheduled_at=T0,
        )
        self.assertEqual(result.status, Status.PREPARING)
        reason = result.missing_key_failures[0].reason_chain
        self.assertIn("原始通知组", reason)
        self.assertIn("g-root", reason)
        self.assertIn("内层组", reason)
        self.assertIn("没钥的人", reason)
        self.assertIn("secret", reason)
        self.assertIn("停在准备状态", reason)
        # 结构化路径同样可回溯到根。
        path_ids = [
            node.ref
            for node in result.missing_key_failures[0].eligible_paths[0]
        ]
        self.assertEqual(path_ids, ["g-root", "g-inner", "p-nokey"])

    def test_all_keys_expired_is_reported_per_key(self):
        snap = build_snapshot(
            "v1",
            groups=(group("g-root", [ref("p-a", MemberKind.PERSON)], name="根组"),),
            persons=(
                person(
                    "p-a",
                    (
                        key("k-old", valid_to=datetime(2026, 11, 27, tzinfo=T0.tzinfo)),
                        key("k-future", valid_from=datetime(2026, 11, 29, tzinfo=T0.tzinfo)),
                    ),
                    name="Alice",
                ),
            ),
        )
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=T0,
        )
        self.assertEqual(result.status, Status.PREPARING)
        failure = result.missing_key_failures[0]
        self.assertEqual(len(failure.key_diagnostics), 2)
        self.assertFalse(any(diag.valid for diag in failure.key_diagnostics))
        reasons = " ".join(diag.reason for diag in failure.key_diagnostics)
        self.assertIn("已过有效期", reasons)
        self.assertIn("尚未生效", reasons)

    def test_restricted_person_without_key_does_not_block(self):
        # 全部路径都被密级阻断的人没有资格，即使没密钥也不应停发。
        leaf = group("g-leaf", [ref("p-blocked", MemberKind.PERSON)])
        root = group(
            "g-root",
            [ref("g-leaf", MemberKind.GROUP, min_level="secret")],
            name="根组",
        )
        snap = build_snapshot(
            "v1",
            groups=(root, leaf),
            persons=(person("p-blocked", ()),),
        )
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=T0,
        )
        self.assertEqual(result.status, Status.READY)
        self.assertEqual(result.missing_key_failures, ())
        self.assertIn("p-blocked", result.restricted)

    def test_one_missing_key_among_many_blocks_whole_email(self):
        snap = build_snapshot(
            "v1",
            groups=(
                group(
                    "g-root",
                    [
                        ref("p-a", MemberKind.PERSON),
                        ref("p-b", MemberKind.PERSON),
                        ref("p-c", MemberKind.PERSON),
                    ],
                    name="根组",
                ),
            ),
            persons=(
                person("p-a", (key("k-a"),)),
                person("p-b", ()),  # 只有他没钥
                person("p-c", (key("k-c"),)),
            ),
        )
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=T0,
        )
        self.assertEqual(result.status, Status.PREPARING)
        self.assertEqual(
            [f.person_id for f in result.missing_key_failures],
            ["p-b"],
        )
        # 停发时仍把其他人的选钥结果保留下来，方便调用方展示差在哪。
        self.assertEqual(
            [r.person_id for r in result.recipients],
            ["p-a", "p-c"],
        )


class KeySelectionAndDedupTests(unittest.TestCase):
    def test_deterministic_key_selection_prefers_latest_expiry(self):
        snap = build_snapshot(
            "v1",
            groups=(group("g-root", [ref("p-a", MemberKind.PERSON)]),),
            persons=(
                person(
                    "p-a",
                    (
                        key("k-short", valid_to=datetime(2026, 11, 28, 18, tzinfo=T0.tzinfo)),
                        key("k-long", valid_to=datetime(2026, 11, 30, 18, tzinfo=T0.tzinfo)),
                    ),
                ),
            ),
        )
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=T0,
        )
        self.assertEqual(result.recipients[0].key_id, "k-long")

    def test_tie_break_by_smallest_key_id(self):
        snap = build_snapshot(
            "v1",
            groups=(group("g-root", [ref("p-a", MemberKind.PERSON)]),),
            persons=(
                person(
                    "p-a",
                    (
                        key("k-z", valid_to=datetime(2026, 11, 30, tzinfo=T0.tzinfo)),
                        key("k-a", valid_to=datetime(2026, 11, 30, tzinfo=T0.tzinfo)),
                    ),
                ),
            ),
        )
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=T0,
        )
        self.assertEqual(result.recipients[0].key_id, "k-a")


class StructuralAndInputTests(unittest.TestCase):
    def test_dangling_group_reference_is_structural_error(self):
        root = group(
            "g-root",
            [ref("g-ghost", MemberKind.GROUP)],
            name="根组",
        )
        snap = build_snapshot(
            "v1",
            groups=(root,),
            persons=(person("p-a", (key("k-a"),)),),
        )
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=T0,
        )
        self.assertEqual(result.status, Status.PREPARING)
        self.assertEqual(len(result.structural_errors), 1)
        self.assertIn("g-ghost", result.structural_errors[0])

    def test_unknown_version_and_root_raise(self):
        store = build_store(
            build_snapshot("v1", groups=(group("g-root", ()),))
        )
        with self.assertRaises(KeyError):
            check_key_coverage(
                store,
                directory_version="missing",
                root_group_id="g-root",
                notification_level="public",
                scheduled_at=T0,
            )
        with self.assertRaises(KeyError):
            check_key_coverage(
                store,
                directory_version="v1",
                root_group_id="g-missing",
                notification_level="public",
                scheduled_at=T0,
            )

    def test_naive_datetime_rejected(self):
        store = build_store(
            build_snapshot("v1", groups=(group("g-root", ()),))
        )
        with self.assertRaises(ValueError):
            check_key_coverage(
                store,
                directory_version="v1",
                root_group_id="g-root",
                notification_level="public",
                scheduled_at=datetime(2026, 11, 28, 12, 0, 0),
            )

    def test_unknown_classification_rejected(self):
        store = build_store(
            build_snapshot("v1", groups=(group("g-root", ()),))
        )
        with self.assertRaises(ValueError):
            check_key_coverage(
                store,
                directory_version="v1",
                root_group_id="g-root",
                notification_level="cosmic",
                scheduled_at=T0,
            )


class InterleavingTests(unittest.TestCase):
    """环 + 密级 + 多路径 + 密钥失效同时出现的交错场景。"""

    def test_cycle_with_level_split_and_expired_key(self):
        # g-loop 自引用；alice 经无约束边（合格）和 secret 边（受限）两条路到达；
        # bob 只有经环内真实成员一条合格路但密钥过期。
        loop = group(
            "g-loop",
            [
                ref("p-alice", MemberKind.PERSON),
                ref("p-bob", MemberKind.PERSON),
                ref("g-loop", MemberKind.GROUP),
            ],
            name="环组",
        )
        root = group(
            "g-root",
            [
                ref("g-loop", MemberKind.GROUP),
                ref("p-alice", MemberKind.PERSON, min_level="secret"),
            ],
            name="根组",
        )
        snap = build_snapshot(
            "v1",
            groups=(root, loop),
            persons=(
                person("p-alice", (key("k-alice"),), name="Alice"),
                person(
                    "p-bob",
                    (key("k-bob-expired", valid_to=datetime(2026, 11, 27, tzinfo=T0.tzinfo)),),
                    name="Bob",
                ),
            ),
        )
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="internal",  # secret 边被阻断，但无约束路仍合格
            scheduled_at=T0,
        )
        # bob 合格却无有效密钥 -> 整封停发。
        self.assertEqual(result.status, Status.PREPARING)
        self.assertEqual([f.person_id for f in result.missing_key_failures], ["p-bob"])
        # alice 沿无约束路径合格，环没有让她丢失。
        self.assertEqual([r.person_id for r in result.recipients], ["p-alice"])
        self.assertEqual(len(result.cycle_cuts), 1)
        self.assertEqual(sorted(result.reached_person_ids), ["p-alice", "p-bob"])
        # alice 同时拥有受限路径和合格路径，但不出现在 restricted 里（并集语义）。
        self.assertNotIn("p-alice", result.restricted)

    def test_same_person_two_paths_one_blocked_but_key_expired_anyway(self):
        # 两条路都到达 carol：一条受限、一条合格；合格路下密钥过期 -> 停发，
        # 且原因链只回溯合格路径。
        g_closed = group(
            "g-closed",
            [ref("p-carol", MemberKind.PERSON)],
            name="封闭组",
        )
        g_open = group(
            "g-open",
            [ref("p-carol", MemberKind.PERSON)],
            name="开放组",
        )
        root = group(
            "g-root",
            [
                ref("g-closed", MemberKind.GROUP, min_level="top_secret"),
                ref("g-open", MemberKind.GROUP),
            ],
            name="根组",
        )
        snap = build_snapshot(
            "v1",
            groups=(root, g_closed, g_open),
            persons=(
                person(
                    "p-carol",
                    (key("k-old", valid_to=datetime(2026, 11, 27, tzinfo=T0.tzinfo)),),
                    name="Carol",
                ),
            ),
        )
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="internal",
            scheduled_at=T0,
        )
        self.assertEqual(result.status, Status.PREPARING)
        failure = result.missing_key_failures[0]
        self.assertEqual(failure.person_id, "p-carol")
        self.assertEqual(len(failure.eligible_paths), 1)
        self.assertIn("开放组", failure.reason_chain)
        self.assertNotIn("封闭组", failure.reason_chain)


if __name__ == "__main__":
    unittest.main()
