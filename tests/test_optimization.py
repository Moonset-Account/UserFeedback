"""优化目标测试：暴力枚举对照、封套共享、确定性平局。"""

import random
import unittest

from mail_gateway import (
    InfeasibleDeliveryError,
    Key,
    Recipient,
    plan_delivery,
)

from tests.helpers import (
    assert_plan_invariants,
    brute_force_optimum,
    random_instance,
)

S = "S1"


def _open_recipient(rid: str, universe: set[str]) -> Recipient:
    return Recipient(
        recipient_id=rid,
        suites=frozenset([S]),
        can_see=frozenset(universe),
        decrypt_scope=frozenset(universe),
    )


class BruteForceComparisonTest(unittest.TestCase):
    def test_matches_brute_force_on_random_small_instances(self) -> None:
        rng = random.Random(20261126)
        checked = 0
        for n in range(1, 6):
            for _ in range(10):
                recipients, keys = random_instance(n, rng)
                expected = brute_force_optimum(recipients, keys)
                try:
                    plan = plan_delivery(recipients, keys)
                except InfeasibleDeliveryError:
                    self.assertIsNone(
                        expected,
                        msg="planner gave up but brute force found a plan",
                    )
                    continue
                self.assertIsNotNone(expected)
                self.assertEqual(
                    (plan.package_count, plan.envelope_count),
                    expected,
                    msg=(
                        f"n={n} recipients={sorted(recipients)} "
                        f"keys={[k.key_id for k in keys]}"
                    ),
                )
                assert_plan_invariants(self, plan, recipients, keys)
                checked += 1
        self.assertGreaterEqual(checked, 40)


class EnvelopeSharingTest(unittest.TestCase):
    def test_shared_group_key_minimizes_envelopes(self) -> None:
        ids = ["a", "b", "c"]
        recipients = {rid: _open_recipient(rid, set(ids)) for rid in ids}
        group = Key(
            key_id="g",
            suite=S,
            holders=frozenset(ids),
            identity="g",
            resolvers=frozenset(),
        )
        personals = [
            Key(
                key_id=f"p-{rid}",
                suite=S,
                holders=frozenset([rid]),
                identity=rid,
                resolvers=frozenset(),
            )
            for rid in ids
        ]
        plan = plan_delivery(recipients, personals + [group])
        self.assertEqual(plan.package_count, 1)
        # 一把组钥封套即可让三人各自可解密，优于三把个人封套
        self.assertEqual(plan.envelope_count, 1)
        self.assertEqual(plan.packages[0].envelope_key_ids, ("g",))

    def test_deterministic_tie_break_on_envelope_choice(self) -> None:
        """两把覆盖范围相同的钥，成本相同时选 key_id 字典序更小者。"""
        ids = ["a", "b"]
        recipients = {rid: _open_recipient(rid, set(ids)) for rid in ids}
        k_z = Key("k-z", S, frozenset(ids), "k-z", frozenset())
        k_a = Key("k-a", S, frozenset(ids), "k-a", frozenset())
        plan1 = plan_delivery(recipients, [k_z, k_a])
        plan2 = plan_delivery(recipients, [k_a, k_z])
        self.assertEqual(
            plan1.packages[0].envelope_key_ids,
            plan2.packages[0].envelope_key_ids,
        )
        self.assertEqual(plan1.packages[0].envelope_key_ids, ("k-a",))

    def test_deterministic_tie_break_on_partition(self) -> None:
        """两种拆法成本完全相同：划分签名字典序更小者胜出，且结果可复现。"""
        ids = ["a", "b", "c", "d"]
        # 套件差异使拆成两个两人包成为最优，且存在多种平局拆法。
        # scope 允许同套件两人包：a/b 互可见，c/d 互可见；跨套件仍不能合包。
        recipients = {
            "a": Recipient("a", frozenset(["X"]), frozenset(ids),
                           frozenset(["a", "b"])),
            "b": Recipient("b", frozenset(["X"]), frozenset(ids),
                           frozenset(["a", "b"])),
            "c": Recipient("c", frozenset(["Y"]), frozenset(ids),
                           frozenset(["c", "d"])),
            "d": Recipient("d", frozenset(["Y"]), frozenset(ids),
                           frozenset(["c", "d"])),
        }
        keys = [
            Key(f"p-{rid}", suite, frozenset([rid]), rid, frozenset())
            for rid, suite in [("a", "X"), ("b", "X"), ("c", "Y"), ("d", "Y")]
        ]
        plan1 = plan_delivery(recipients, keys)
        plan2 = plan_delivery(recipients, list(reversed(keys)))
        self.assertEqual(plan1.package_count, 2)
        self.assertEqual(plan1.envelope_count, 4)
        # 平局结果与输入顺序无关，可复现
        self.assertEqual(
            tuple(sorted(tuple(sorted(p.members)) for p in plan1.packages)),
            tuple(sorted(tuple(sorted(p.members)) for p in plan2.packages)),
        )
        # 字典序最小的两人划分：(['a','b'], ['c','d'])
        self.assertEqual(
            [sorted(p.members) for p in plan1.packages],
            [["a", "b"], ["c", "d"]],
        )


if __name__ == "__main__":
    unittest.main()
