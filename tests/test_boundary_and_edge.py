"""边界与交错约束：12 人上限、空输入、套件冲突、多约束交织。"""

import random
import time
import unittest

from mail_gateway import (
    InfeasibleDeliveryError,
    Key,
    MAX_RECIPIENTS,
    Recipient,
    evaluate_package,
    plan_delivery,
)

from tests.helpers import assert_plan_invariants, random_instance


class BoundaryTest(unittest.TestCase):
    def test_empty_input_returns_empty_plan(self) -> None:
        plan = plan_delivery({}, [])
        self.assertEqual(plan.package_count, 0)
        self.assertEqual(plan.envelope_count, 0)

    def test_more_than_twelve_recipients_rejected(self) -> None:
        recipients = {
            f"r{i}": Recipient(
                f"r{i}", frozenset(["S"]), frozenset(), frozenset([f"r{i}"])
            )
            for i in range(MAX_RECIPIENTS + 1)
        }
        with self.assertRaises(ValueError):
            plan_delivery(recipients, [])

    def test_twelve_recipients_single_shared_key(self) -> None:
        ids = [f"r{i:02d}" for i in range(12)]
        recipients = {
            rid: Recipient(rid, frozenset(["S"]), frozenset(ids), frozenset(ids))
            for rid in ids
        }
        group = Key("g", "S", frozenset(ids), "g", frozenset())
        start = time.perf_counter()
        plan = plan_delivery(recipients, [group])
        elapsed = time.perf_counter() - start
        self.assertEqual(plan.package_count, 1)
        self.assertEqual(plan.envelope_count, 1)
        self.assertLess(elapsed, 5.0)

    def test_twelve_recipients_forced_into_solo_packages(self) -> None:
        # 每人独占一种套件：任何两人都没有共同套件，只能 12 个包。
        ids = [f"r{i:02d}" for i in range(12)]
        recipients = {
            rid: Recipient(
                rid,
                frozenset([f"S{i}"]),
                frozenset(ids),
                frozenset([rid]),  # 只授权自己，个人钥持有人恰为自己
            )
            for i, rid in enumerate(ids)
        }
        keys = [
            Key(f"k{i}", f"S{i}", frozenset([rid]), rid, frozenset())
            for i, rid in enumerate(ids)
        ]
        start = time.perf_counter()
        plan = plan_delivery(recipients, keys)
        elapsed = time.perf_counter() - start
        self.assertEqual(plan.package_count, 12)
        self.assertEqual(plan.envelope_count, 12)
        for pkg in plan.packages:
            self.assertEqual(len(pkg.members), 1)
        assert_plan_invariants(self, plan, recipients, keys)
        self.assertLess(elapsed, 5.0)

    def test_random_twelve_person_instance_invariants(self) -> None:
        for seed in (424242, 7, 99):
            rng = random.Random(seed)
            recipients, keys = random_instance(12, rng)
            try:
                plan = plan_delivery(recipients, keys)
            except InfeasibleDeliveryError:
                # 随机构造可能无解；无解也是合法输出，这里仅确认异常类型。
                continue
            assert_plan_invariants(self, plan, recipients, keys)


class SuiteSplitTest(unittest.TestCase):
    def test_no_common_suite_forces_split(self) -> None:
        recipients = {
            "a": Recipient("a", frozenset(["X"]), frozenset(["a", "b"]),
                           frozenset(["a"])),
            "b": Recipient("b", frozenset(["Y"]), frozenset(["a", "b"]),
                           frozenset(["b"])),
        }
        keys = [
            Key("ka", "X", frozenset(["a"]), "a", frozenset()),
            Key("kb", "Y", frozenset(["b"]), "b", frozenset()),
        ]
        option = evaluate_package(frozenset(["a", "b"]), recipients, keys)
        self.assertFalse(option.feasible)
        self.assertTrue(
            any("no common cipher suite" in v for v in option.violations)
        )
        plan = plan_delivery(recipients, keys)
        self.assertEqual(plan.package_count, 2)
        self.assertEqual(plan.envelope_count, 2)


class InterleavedConstraintsTest(unittest.TestCase):
    """套件、密送可见性、解密范围三类约束同时生效的手工场景。"""

    def setUp(self) -> None:
        self.recipients = {
            # a 支持两种套件，但不允许 b 知道自己的身份
            "a": Recipient("a", frozenset(["S1", "S2"]),
                           frozenset(["a", "c"]), frozenset(["a", "b", "c"])),
            # b 只支持 S1，且授权范围不含 c
            "b": Recipient("b", frozenset(["S1"]),
                           frozenset(["a", "b", "c"]), frozenset(["a", "b"])),
            # c 只支持 S2
            "c": Recipient("c", frozenset(["S2"]),
                           frozenset(["a", "b", "c"]), frozenset(["a", "b", "c"])),
        }
        self.keys = [
            # S1 组钥：b 能反查到 a 的身份，但 a 不允许 b 看到 -> 必被排除
            Key("k-ab", "S1", frozenset(["a", "b"]), "a", frozenset(["b"])),
            # b 的个人钥（S1）
            Key("k-b", "S1", frozenset(["b"]), "b", frozenset()),
            # S2 组钥覆盖 a、c，标识不暴露身份
            Key("k-ac", "S2", frozenset(["a", "c"]), "a", frozenset()),
        ]

    def test_visibility_blocked_key_reported(self) -> None:
        option = evaluate_package(
            frozenset(["a", "b"]), self.recipients, self.keys
        )
        self.assertFalse(option.feasible)
        self.assertTrue(
            any("k-ab" in v and "'b'" in v for v in option.violations),
            msg=option.violations,
        )

    def test_scope_and_suite_and_visibility_together(self) -> None:
        # 三人整包：无共同套件
        self.assertFalse(
            evaluate_package(
                frozenset(["a", "b", "c"]), self.recipients, self.keys
            ).feasible
        )
        # {a,b}：套件 S1 可行，但唯一覆盖 a 的 k-ab 因可见性被排除
        self.assertFalse(
            evaluate_package(
                frozenset(["a", "b"]), self.recipients, self.keys
            ).feasible
        )
        # {a,c}：套件 S2，k-ac 一把封套即可
        option_ac = evaluate_package(
            frozenset(["a", "c"]), self.recipients, self.keys
        )
        self.assertTrue(option_ac.feasible, msg=option_ac.violations)
        self.assertEqual(option_ac.envelope_key_ids, ("k-ac",))

        plan = plan_delivery(self.recipients, self.keys)
        # 最优：{a,c} 一把组钥封套 + {b} 一把个人钥 = 2 包 2 封套
        self.assertEqual(plan.package_count, 2)
        self.assertEqual(plan.envelope_count, 2)
        assert_plan_invariants(self, plan, self.recipients, self.keys)
        mapping = plan.recipient_to_package()
        self.assertEqual(mapping["a"].members, mapping["c"].members)
        self.assertEqual(mapping["b"].members, frozenset(["b"]))
        for pkg in plan.packages:
            self.assertNotIn("k-ab", pkg.envelope_key_ids)


if __name__ == "__main__":
    unittest.main()
