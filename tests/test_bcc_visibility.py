"""密送身份只能对部分人隐藏、匿名标识反查等可见性场景。"""

import unittest

from mail_gateway import Key, Recipient, evaluate_package, plan_delivery

S = "S1"


def _recipient(rid: str, can_see: set[str], scope: set[str]) -> Recipient:
    return Recipient(
        recipient_id=rid,
        suites=frozenset([S]),
        can_see=frozenset(can_see),
        decrypt_scope=frozenset(scope),
    )


def _personal_key(rid: str, scope_people: set[str]) -> Key:
    return Key(
        key_id=f"key-{rid}",
        suite=S,
        holders=frozenset([rid]),
        identity=rid,
        resolvers=frozenset(),  # 个人钥标识不暴露身份
    )


class BccVisibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ids = ["alice", "bob", "carol"]
        # alice 允许 carol 知道自己，但不允许 bob（对 bob 密送隐藏）
        self.recipients = {
            "alice": _recipient("alice", {"alice", "carol"}, set(self.ids)),
            "bob": _recipient("bob", set(self.ids), set(self.ids)),
            "carol": _recipient("carol", set(self.ids), set(self.ids)),
        }
        # 组钥：包头标识反查到 alice 的身份，只有 carol 能反查，bob 不能
        self.group_key = Key(
            key_id="g-alice-bob",
            suite=S,
            holders=frozenset(["alice", "bob"]),
            identity="alice",
            resolvers=frozenset(["carol"]),
        )

    def test_identity_hidden_from_bob_but_visible_to_carol(self) -> None:
        """同一把钥在不同包里：bob 看不到，carol 看得到且在允许名单内。"""
        # 包 {alice, bob}：carol 不在包内，包内无人能反查 -> 身份成功隐藏
        option_ab = evaluate_package(
            frozenset(["alice", "bob"]),
            self.recipients,
            [self.group_key, _personal_key("alice", set(self.ids)),
             _personal_key("bob", set(self.ids))],
        )
        self.assertTrue(option_ab.feasible, msg=option_ab.violations)
        self.assertEqual(option_ab.envelope_key_ids, ("g-alice-bob",))

        # 包 {alice, bob, carol}：carol 能反查 alice，且 alice 允许 carol 看到
        personal = [_personal_key(r, set(self.ids)) for r in self.ids]
        option_all = evaluate_package(
            frozenset(self.ids), self.recipients, [self.group_key] + personal
        )
        self.assertTrue(option_all.feasible, msg=option_all.violations)
        # carol 不在组钥持有人里，需要她自己的封套；组钥仍可安全出现在包头
        self.assertIn("g-alice-bob", option_all.envelope_key_ids)
        self.assertIn("key-carol", option_all.envelope_key_ids)

    def test_identity_revealed_to_unauthorized_member_blocks_key(self) -> None:
        """bob 能反查 alice 身份、但 alice 不允许 bob 知道 -> 该钥被拒绝。"""
        leaky = Key(
            key_id="g-leak",
            suite=S,
            holders=frozenset(["alice", "bob"]),
            identity="alice",
            resolvers=frozenset(["bob"]),  # bob 能反查
        )
        option = evaluate_package(
            frozenset(["alice", "bob"]), self.recipients, [leaky]
        )
        self.assertFalse(option.feasible)
        self.assertTrue(
            any("reveals identity 'alice' to 'bob'" in v for v in option.violations),
            msg=option.violations,
        )

    def test_anonymous_identity_with_resolver_is_blocked(self) -> None:
        """匿名标识一旦某人可反查，而身份不在收件人表（无允许名单）-> 拒绝。"""
        anon = Key(
            key_id="anon-key",
            suite=S,
            holders=frozenset(["bob"]),
            identity="anon-9f32",
            resolvers=frozenset(["carol"]),
        )
        option = evaluate_package(
            frozenset(["bob", "carol"]), self.recipients, [anon]
        )
        self.assertFalse(option.feasible)
        self.assertTrue(
            any("anon-9f32" in v and "carol" in v for v in option.violations),
            msg=option.violations,
        )

    def test_anonymous_identity_without_resolver_is_safe(self) -> None:
        """无人能反查的匿名标识不会触发可见性违规。"""
        anon = Key(
            key_id="anon-key",
            suite=S,
            holders=frozenset(["bob"]),
            identity="anon-9f32",
            resolvers=frozenset(),
        )
        option = evaluate_package(
            frozenset(["bob", "carol"]),
            self.recipients,
            [anon, _personal_key("carol", set(self.ids))],
        )
        self.assertTrue(option.feasible, msg=option.violations)

    def test_plan_respects_partial_hiding(self) -> None:
        """端到端：泄露钥被排除后，规划器仍能用安全钥完成拆投。"""
        leaky = Key(
            key_id="g-leak",
            suite=S,
            holders=frozenset(["alice", "bob"]),
            identity="alice",
            resolvers=frozenset(["bob"]),
        )
        personal = [_personal_key(r, set(self.ids)) for r in self.ids]
        plan = plan_delivery(self.recipients, [leaky] + personal)
        for pkg in plan.packages:
            self.assertNotIn("g-leak", pkg.envelope_key_ids)
        self.assertEqual(plan.package_count, 1)
        self.assertEqual(plan.envelope_count, 3)


if __name__ == "__main__":
    unittest.main()
