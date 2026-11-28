"""正常场景：嵌套组展开、有效时段边界、版本固定、按密钥去重。"""

import unittest

from key_coverage import (
    DirectoryStore,
    MemberKind,
    Status,
    check_key_coverage,
)

from tests.builders import (
    T0,
    alias,
    at,
    build_snapshot,
    build_store,
    group,
    key,
    person,
    ref,
)


def _basic_store() -> DirectoryStore:
    # root -> [alice(人), team(组)]，team -> [bob, carol]
    persons = (
        person("p-alice", (key("k-alice", at(8), at(18)),), name="Alice"),
        person("p-bob", (key("k-bob", at(8), at(18)),), name="Bob"),
        person("p-carol", (key("k-carol", at(8), at(18)),), name="Carol"),
    )
    team = group(
        "g-team",
        [
            ref("p-bob", MemberKind.PERSON),
            ref("p-carol", MemberKind.PERSON),
        ],
        name="技术组",
    )
    root = group(
        "g-root",
        [
            ref("p-alice", MemberKind.PERSON),
            ref("g-team", MemberKind.GROUP),
        ],
        name="全员组",
    )
    return build_store(build_snapshot("v1", groups=(root, team), persons=persons))


class NormalCoverageTests(unittest.TestCase):
    def test_nested_groups_expand_to_all_persons(self):
        store = _basic_store()
        result = check_key_coverage(
            store,
            directory_version="v1",
            root_group_id="g-root",
            notification_level="internal",
            scheduled_at=at(12),
        )
        self.assertEqual(result.status, Status.READY)
        self.assertEqual(
            [r.person_id for r in result.recipients],
            ["p-alice", "p-bob", "p-carol"],
        )
        self.assertEqual(
            [r.key_id for r in result.recipients],
            ["k-alice", "k-bob", "k-carol"],
        )
        # 到达路径包含根组 -> 子组 -> 成员。
        bob = next(r for r in result.recipients if r.person_id == "p-bob")
        chain_ids = [node.ref for node in bob.eligible_paths[0]]
        self.assertEqual(chain_ids, ["g-root", "g-team", "p-bob"])
        self.assertEqual(
            [node.name for node in bob.eligible_paths[0]],
            ["全员组", "技术组", "Bob"],
        )

    def test_validity_window_is_half_open(self):
        # 密钥窗口 [09:00, 12:00)。
        snap = build_snapshot(
            "v1",
            groups=(group("g-root", [ref("p-a", MemberKind.PERSON)]),),
            persons=(person("p-a", (key("k-a", at(9), at(12)),)),),
        )
        store = build_store(snap)

        inside = check_key_coverage(
            store,
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=at(11, 59),
        )
        self.assertEqual(inside.status, Status.READY)
        self.assertEqual(inside.recipients[0].key_id, "k-a")

        on_right_edge = check_key_coverage(
            store,
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=at(12),
        )
        self.assertEqual(on_right_edge.status, Status.PREPARING)
        self.assertEqual(len(on_right_edge.missing_key_failures), 1)
        self.assertIn("半开区间", on_right_edge.missing_key_failures[0].reason_chain)

        before_left_edge = check_key_coverage(
            store,
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=at(8, 59),
        )
        self.assertEqual(before_left_edge.status, Status.PREPARING)
        self.assertIn("尚未生效", before_left_edge.missing_key_failures[0].reason_chain)

    def test_open_ended_windows(self):
        # 只有起始 / 只有结束 / 双向开放的窗口。
        persons = (
            person("p-from", (key("k-from", valid_from=at(9)),)),
            person("p-to", (key("k-to", valid_to=at(18)),)),
            person("p-open", (key("k-open"),)),
        )
        snap = build_snapshot(
            "v1",
            groups=(
                group(
                    "g-root",
                    [
                        ref("p-from", MemberKind.PERSON),
                        ref("p-to", MemberKind.PERSON),
                        ref("p-open", MemberKind.PERSON),
                    ],
                ),
            ),
            persons=persons,
        )
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=at(12),
        )
        self.assertEqual(result.status, Status.READY)
        self.assertEqual(len(result.recipients), 3)

    def test_version_pin_uses_only_pinned_snapshot(self):
        # v1 里 carol 还不在组里；v2 里才加进来。检查固定 v1 时必须看不到 v2。
        persons = (
            person("p-alice", (key("k-alice"),)),
            person("p-carol", (key("k-carol"),)),
        )
        v1_root = group("g-root", [ref("p-alice", MemberKind.PERSON)])
        v2_root = group(
            "g-root",
            [
                ref("p-alice", MemberKind.PERSON),
                ref("p-carol", MemberKind.PERSON),
            ],
        )
        store = build_store(
            build_snapshot("v1", groups=(v1_root,), persons=persons),
            build_snapshot("v2", groups=(v2_root,), persons=persons),
        )
        pinned = check_key_coverage(
            store,
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=T0,
        )
        self.assertEqual([r.person_id for r in pinned.recipients], ["p-alice"])
        self.assertEqual(pinned.directory_version, "v1")

        latest = check_key_coverage(
            store,
            directory_version="v2",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=T0,
        )
        self.assertEqual(
            [r.person_id for r in latest.recipients],
            ["p-alice", "p-carol"],
        )

    def test_recipients_deduplicated_by_actual_key_id(self):
        # 同一人员沿两条路径到达，且只有一把有效密钥：最终只出现一次。
        shared = group(
            "g-shared",
            [ref("p-a", MemberKind.PERSON)],
            name="共享组",
        )
        root = group(
            "g-root",
            [
                ref("p-a", MemberKind.PERSON),
                ref("g-shared", MemberKind.GROUP),
            ],
            name="根组",
        )
        snap = build_snapshot(
            "v1",
            groups=(root, shared),
            persons=(person("p-a", (key("k-a"),), name="Alice"),),
        )
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=T0,
        )
        self.assertEqual(result.status, Status.READY)
        self.assertEqual(len(result.recipients), 1)
        self.assertEqual(result.recipients[0].key_id, "k-a")
        # 两条合格路径都保留在明细里，去重只发生在最终收件人层面。
        self.assertEqual(len(result.recipients[0].eligible_paths), 2)

    def test_empty_group_is_ready_with_no_recipients(self):
        snap = build_snapshot("v1", groups=(group("g-empty", ()),))
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-empty",
            notification_level="public",
            scheduled_at=T0,
        )
        self.assertEqual(result.status, Status.READY)
        self.assertEqual(result.recipients, ())
        self.assertEqual(result.missing_key_failures, ())


if __name__ == "__main__":
    unittest.main()
