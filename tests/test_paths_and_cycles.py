"""密级约束、多路径资格合并、别名解析与环（自引用 / 互引用）场景。"""

import unittest

from key_coverage import (
    MemberKind,
    Status,
    check_key_coverage,
)

from tests.builders import (
    T0,
    alias,
    build_snapshot,
    build_store,
    group,
    key,
    person,
    ref,
)


class ClassificationPathTests(unittest.TestCase):
    def test_path_blocked_when_notification_below_min_level(self):
        # 进入 secret 组要求 secret，发 internal 通知 -> 成员无资格但仍被“到达”。
        secret_group = group(
            "g-secret",
            [ref("p-s", MemberKind.PERSON)],
            name="机密组",
        )
        root = group(
            "g-root",
            [ref("g-secret", MemberKind.GROUP, min_level="secret")],
            name="根组",
        )
        snap = build_snapshot(
            "v1",
            groups=(root, secret_group),
            persons=(person("p-s", (key("k-s"),)),),
        )
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="internal",
            scheduled_at=T0,
        )
        self.assertEqual(result.status, Status.READY)
        self.assertEqual(result.recipients, ())
        self.assertIn("p-s", result.restricted)
        blocked_path = result.restricted["p-s"][0]
        self.assertEqual(blocked_path[1].min_level, "secret")
        # 受限者仍被遍历到，避免“漏掉人”。
        self.assertEqual(result.reached_person_ids, ("p-s",))

    def test_eligibility_merged_across_paths_union_semantics(self):
        # bob 一条路径要求 secret（被阻断），另一条无约束（合格）=> 有资格。
        restricted_group = group(
            "g-restricted",
            [ref("p-bob", MemberKind.PERSON)],
            name="受限组",
        )
        open_group = group(
            "g-open",
            [ref("p-bob", MemberKind.PERSON)],
            name="开放组",
        )
        root = group(
            "g-root",
            [
                ref("g-restricted", MemberKind.GROUP, min_level="secret"),
                ref("g-open", MemberKind.GROUP),
            ],
            name="根组",
        )
        snap = build_snapshot(
            "v1",
            groups=(root, restricted_group, open_group),
            persons=(person("p-bob", (key("k-bob"),), name="Bob"),),
        )
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="internal",
            scheduled_at=T0,
        )
        self.assertEqual(result.status, Status.READY)
        self.assertEqual([r.person_id for r in result.recipients], ["p-bob"])
        recipient = result.recipients[0]
        # 只保留合格路径；受限路径不进入收件人明细。
        self.assertEqual(len(recipient.eligible_paths), 1)
        self.assertEqual(
            [node.ref for node in recipient.eligible_paths[0]],
            ["g-root", "g-open", "p-bob"],
        )
        self.assertNotIn("p-bob", result.restricted)

    def test_classification_ordering_boundary_levels(self):
        # 边要求 confidential：同级别刚好满足，低一级 internal 不满足。
        leaf = group("g-leaf", [ref("p-a", MemberKind.PERSON)])
        root = group(
            "g-root",
            [ref("g-leaf", MemberKind.GROUP, min_level="confidential")],
        )
        snap = build_snapshot(
            "v1",
            groups=(root, leaf),
            persons=(person("p-a", (key("k-a"),)),),
        )
        store = build_store(snap)
        ok = check_key_coverage(
            store,
            directory_version="v1",
            root_group_id="g-root",
            notification_level="confidential",
            scheduled_at=T0,
        )
        self.assertEqual(ok.status, Status.READY)
        self.assertEqual(len(ok.recipients), 1)

        blocked = check_key_coverage(
            store,
            directory_version="v1",
            root_group_id="g-root",
            notification_level="internal",
            scheduled_at=T0,
        )
        self.assertEqual(blocked.status, Status.READY)
        self.assertEqual(blocked.recipients, ())
        self.assertIn("p-a", blocked.restricted)

    def test_alias_chain_resolves_to_person_and_group(self):
        # 别名 a-person -> 人；别名 a-team -> 组；别名 a2 -> a-person（别名套别名）。
        team = group(
            "g-team",
            [ref("p-bob", MemberKind.PERSON)],
            name="技术组",
        )
        root = group(
            "g-root",
            [
                ref("a-person", MemberKind.ALIAS),
                ref("a-team", MemberKind.ALIAS),
                ref("a2", MemberKind.ALIAS),
            ],
            name="根组",
        )
        aliases = (
            alias("a-person", "p-alice", MemberKind.PERSON),
            alias("a-team", "g-team", MemberKind.GROUP),
            alias("a2", "a-person", MemberKind.ALIAS),
        )
        persons = (
            person("p-alice", (key("k-alice"),), name="Alice"),
            person("p-bob", (key("k-bob"),), name="Bob"),
        )
        snap = build_snapshot(
            "v1", groups=(root, team), aliases=aliases, persons=persons
        )
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=T0,
        )
        self.assertEqual(result.status, Status.READY)
        self.assertEqual(
            sorted(r.person_id for r in result.recipients),
            ["p-alice", "p-bob"],
        )
        alice = next(r for r in result.recipients if r.person_id == "p-alice")
        # 两条别名路径（a-person 与 a2 -> a-person）都解析到同一个人。
        self.assertEqual(len(alice.eligible_paths), 2)
        nested = next(
            path for path in alice.eligible_paths if path[1].ref == "a2"
        )
        self.assertEqual(
            [node.kind.value for node in nested],
            ["group", "alias", "alias", "person"],
        )


class CycleTests(unittest.TestCase):
    def test_self_referencing_group_keeps_real_members(self):
        # g-loop 含真实成员 alice，又把自己列为成员（自引用环）。
        loop = group(
            "g-loop",
            [
                ref("p-alice", MemberKind.PERSON),
                ref("g-loop", MemberKind.GROUP),
            ],
            name="自引用组",
        )
        root = group(
            "g-root",
            [ref("g-loop", MemberKind.GROUP)],
            name="根组",
        )
        snap = build_snapshot(
            "v1",
            groups=(root, loop),
            persons=(person("p-alice", (key("k-alice"),), name="Alice"),),
        )
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=T0,
        )
        self.assertEqual(result.status, Status.READY)
        self.assertEqual([r.person_id for r in result.recipients], ["p-alice"])
        # 自引用边被记录为一次环截断。
        self.assertEqual(len(result.cycle_cuts), 1)
        self.assertEqual(result.cycle_cuts[0][-1].ref, "g-loop")

    def test_mutual_group_reference_terminates_and_covers_everyone(self):
        # g-a -> g-b，g-b -> g-a，各自带真实成员。
        g_a = group(
            "g-a",
            [
                ref("p-ann", MemberKind.PERSON),
                ref("g-b", MemberKind.GROUP),
            ],
            name="A组",
        )
        g_b = group(
            "g-b",
            [
                ref("p-ben", MemberKind.PERSON),
                ref("g-a", MemberKind.GROUP),
            ],
            name="B组",
        )
        root = group(
            "g-root",
            [ref("g-a", MemberKind.GROUP)],
            name="根组",
        )
        persons = (
            person("p-ann", (key("k-ann"),), name="Ann"),
            person("p-ben", (key("k-ben"),), name="Ben"),
        )
        snap = build_snapshot(
            "v1", groups=(root, g_a, g_b), persons=persons
        )
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=T0,
        )
        self.assertEqual(result.status, Status.READY)
        self.assertEqual(
            sorted(r.person_id for r in result.recipients),
            ["p-ann", "p-ben"],
        )
        self.assertTrue(result.cycle_cuts)

    def test_cycle_branch_does_not_swallow_sibling_members(self):
        # root 下两条分支：一条进入环，一条是普通成员，两边的人都要覆盖到。
        loop = group(
            "g-loop",
            [
                ref("p-loop-member", MemberKind.PERSON),
                ref("g-loop", MemberKind.GROUP),
            ],
            name="环组",
        )
        root = group(
            "g-root",
            [
                ref("g-loop", MemberKind.GROUP),
                ref("p-side", MemberKind.PERSON),
            ],
            name="根组",
        )
        persons = (
            person("p-loop-member", (key("k-loop"),)),
            person("p-side", (key("k-side"),)),
        )
        snap = build_snapshot("v1", groups=(root, loop), persons=persons)
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=T0,
        )
        self.assertEqual(
            sorted(r.person_id for r in result.recipients),
            ["p-loop-member", "p-side"],
        )

    def test_alias_cycle_is_cut(self):
        # a1 -> a2 -> a1，不能无限递归。
        root = group(
            "g-root",
            [ref("a1", MemberKind.ALIAS)],
            name="根组",
        )
        aliases = (
            alias("a1", "a2", MemberKind.ALIAS),
            alias("a2", "a1", MemberKind.ALIAS),
        )
        snap = build_snapshot("v1", groups=(root,), aliases=aliases, persons=())
        result = check_key_coverage(
            build_store(snap),
            directory_version="v1",
            root_group_id="g-root",
            notification_level="public",
            scheduled_at=T0,
        )
        self.assertEqual(result.recipients, ())
        self.assertEqual(len(result.cycle_cuts), 1)
        self.assertEqual(result.reached_person_ids, ())


if __name__ == "__main__":
    unittest.main()
