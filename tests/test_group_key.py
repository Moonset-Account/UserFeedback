"""两人共用组密钥、第三人也持钥、组外未授权持钥人的排除场景。"""

import unittest

from mail_gateway import (
    InfeasibleDeliveryError,
    Key,
    Recipient,
    evaluate_package,
    plan_delivery,
)

S = "S1"
TRIO = ["alice", "bob", "carol"]


def _recipient(rid: str, scope: set[str]) -> Recipient:
    return Recipient(
        recipient_id=rid,
        suites=frozenset([S]),
        can_see=frozenset(TRIO),
        decrypt_scope=frozenset(scope),
    )


def _personal(rid: str) -> Key:
    return Key(
        key_id=f"pk-{rid}",
        suite=S,
        holders=frozenset([rid]),
        identity=rid,
        resolvers=frozenset(),
    )


def _group_key(holders: set[str]) -> Key:
    return Key(
        key_id="group-g1",
        suite=S,
        holders=frozenset(holders),
        identity="group-g1",
        resolvers=frozenset(),  # 本组场景只考察解密范围，不考察包头反查
    )


class GroupKeyOutsiderTest(unittest.TestCase):
    def test_outsider_holder_blocks_group_key_everywhere(self) -> None:
        """mallory 不在任何收件人的授权范围内：组钥在任何包里都不可用。"""
        recipients = {rid: _recipient(rid, set(TRIO)) for rid in TRIO}
        group = _group_key({"alice", "bob", "carol", "mallory"})

        for size in (1, 2, 3):
            members = frozenset(TRIO[:size])
            option = evaluate_package(members, recipients, [group])
            self.assertFalse(option.feasible)
            # 每名成员的范围违规都要报出来，且明确点名组外持钥人
            for rid in members:
                self.assertTrue(
                    any(
                        "group-g1" in v
                        and "decrypt scope" in v
                        and f"'{rid}'" in v
                        and "mallory" in v
                        for v in option.violations
                    ),
                    msg=(rid, option.violations),
                )

    def test_planner_excludes_package_outsider_can_open(self) -> None:
        """有个人钥兜底时，规划结果不得使用任何 mallory 能打开的封套。"""
        recipients = {rid: _recipient(rid, set(TRIO)) for rid in TRIO}
        group = _group_key({"alice", "bob", "carol", "mallory"})
        personals = [_personal(rid) for rid in TRIO]

        plan = plan_delivery(recipients, [group] + personals)
        self.assertEqual(plan.package_count, 1)
        self.assertEqual(plan.envelope_count, 3)
        for pkg in plan.packages:
            self.assertNotIn("group-g1", pkg.envelope_key_ids)
            for kid in pkg.envelope_key_ids:
                self.assertTrue(kid.startswith("pk-"))

    def test_group_key_allowed_when_scopes_cover_all_holders(self) -> None:
        """所有人的授权范围都包含 mallory 时，组钥一把封套即可覆盖全员。"""
        everyone = set(TRIO) | {"mallory"}
        recipients = {rid: _recipient(rid, everyone) for rid in TRIO}
        group = _group_key(everyone)

        option = evaluate_package(frozenset(TRIO), recipients, [group])
        self.assertTrue(option.feasible, msg=option.violations)
        self.assertEqual(option.envelope_key_ids, ("group-g1",))

        plan = plan_delivery(recipients, [group])
        self.assertEqual(plan.package_count, 1)
        self.assertEqual(plan.envelope_count, 1)

    def test_outsider_as_recipient_forces_split(self) -> None:
        """mallory 也是收件人，但 carol 未授权她：carol 所在包必须排除组钥。"""
        four = ["alice", "bob", "carol", "mallory"]
        broad = set(four)
        recipients = {
            "alice": _recipient("alice", broad),
            "bob": _recipient("bob", broad),
            "mallory": _recipient("mallory", broad),
            # carol 的范围不含 mallory：任何含 carol 的包都不能使用组钥
            "carol": _recipient("carol", {"alice", "bob", "carol"}),
        }
        group = _group_key(set(four))
        # alice/bob/mallory 各有个人钥，保证各自可以独立成包
        keys = [
            group,
            _personal("alice"),
            _personal("bob"),
            _personal("mallory"),
            _personal("carol"),
        ]

        # carol + 任一组钥持有人同包：组钥因 carol 的范围违规被排除，
        # 而又没有 carol 可用的同套件其他钥，因此该包不可行
        blocked = evaluate_package(
            frozenset(["alice", "carol"]), recipients, [group]
        )
        self.assertFalse(blocked.feasible)
        self.assertTrue(
            any("mallory" in v and "'carol'" in v for v in blocked.violations)
        )

        plan = plan_delivery(recipients, keys)
        # 最优：{alice, bob, mallory} 一把组钥 + {carol} 一把个人钥
        self.assertEqual(plan.package_count, 2)
        self.assertEqual(plan.envelope_count, 2)
        carol_pkg = plan.recipient_to_package()["carol"]
        self.assertEqual(carol_pkg.members, frozenset(["carol"]))
        self.assertEqual(carol_pkg.envelope_key_ids, ("pk-carol",))
        for pkg in plan.packages:
            if "group-g1" in pkg.envelope_key_ids:
                self.assertNotIn("carol", pkg.members)

    def test_no_secure_plan_when_only_key_is_leaking(self) -> None:
        """唯一的钥被组外人持有且无人授权 -> 整体无解。"""
        recipients = {rid: _recipient(rid, set(TRIO)) for rid in TRIO}
        group = _group_key({"alice", "bob", "carol", "mallory"})
        with self.assertRaises(InfeasibleDeliveryError) as ctx:
            plan_delivery(recipients, [group])
        self.assertIn("no secure delivery partition", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
