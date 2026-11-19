"""drill 演练库的 unittest 验收用例。

覆盖:
  - 可接受结果: 撤掉故障域后仍能恢复应急账号, 且解锁顺序真实可执行
  - 应被阻止/拒绝: 互相保管解锁材料的阻塞环; 永不可满足的非法配方
  - 边界/交错: 别名只算一种材料、阈值恰好等于持有数、跨故障域的别名冗余
"""

import unittest

from drill import Credential, Recipe, run_drill


def simulate_plan(initial, plan):
    """真实执行解锁顺序: 每一步的材料必须在执行时已就位。"""
    held = set(initial)
    for step in plan:
        for material in step.used:
            assert material in held, f"解锁 {step.output} 时材料 {material} 尚未就位"
        held.add(step.output)
    return held


class TestAcceptableRecovery(unittest.TestCase):
    """可接受结果: 撤掉单个故障域后仍可恢复应急账号。"""

    def setUp(self):
        self.creds = {
            "badge-primary": "room-a",
            "token-admin": "room-b",
            "seal-physical": "room-a",
        }
        self.recipes = [
            Recipe("vault-key", ["badge-primary", "token-admin"], need=2),
            Recipe("account-emergency", ["vault-key", "seal-physical"], need=2),
        ]

    def test_recovery_survives_room_b_loss(self):
        (report,) = run_drill(self.creds, self.recipes, ["room-b"])
        self.assertEqual(report.removed, ["token-admin"])
        # token-admin 丢失后 vault-key 凑不齐两种材料, 应急账号不可恢复
        self.assertIn("account-emergency", report.blocked)
        self.assertIn("vault-key", report.blocked)

    def test_recovery_survives_room_a_loss_with_spare(self):
        # 在 room-b 部署 badge 与 seal 的备用别名设备后, 撤掉 room-a 仍可恢复
        creds = dict(self.creds, **{"badge-spare": "room-b", "seal-spare": "room-b"})
        (report,) = run_drill(
            creds, self.recipes, ["room-a"],
            aliases={"badge-spare": "badge-primary", "seal-spare": "seal-physical"},
        )
        self.assertIn("account-emergency", report.recovered)
        self.assertEqual(report.blocked, [])
        self.assertEqual(report.cycles, [])
        # 解锁顺序真实可依次执行, 且执行完确实拿到应急账号
        held = simulate_plan(report.available_initial, report.plan)
        self.assertIn("account-emergency", held)
        # 顺序必须是拓扑序: vault-key 先于 account-emergency
        outputs = [s.output for s in report.plan]
        self.assertLess(outputs.index("vault-key"), outputs.index("account-emergency"))


class TestBlockingCycle(unittest.TestCase):
    """应被阻止: 两把钥匙互相保管解锁材料且无外部起点。"""

    def test_mutual_guard_reported_as_cycle(self):
        recipes = [
            Recipe("key-a", ["key-b"], need=1),
            Recipe("key-b", ["key-a"], need=1),
            Recipe("account-emergency", ["key-a", "key-b"], need=2),
        ]
        (report,) = run_drill({"token-outside": "room-a"}, recipes, ["room-a"])
        self.assertEqual(report.recovered, [])
        self.assertIn(["key-a", "key-b"], report.cycles)
        self.assertIn("key-a", report.blocked)
        self.assertIn("key-b", report.blocked)
        self.assertIn("account-emergency", report.blocked)
        self.assertEqual(report.plan, [])

    def test_cycle_broken_by_external_start(self):
        # 交错场景: 同样的互保结构, 但外部起点解锁其中一把后环被打破
        recipes = [
            Recipe("key-a", ["key-b"], need=1),
            Recipe("key-b", ["key-a"], need=1),
            Recipe("key-a", ["token-outside"], need=1),  # 外部起点
        ]
        (report,) = run_drill({"token-outside": "room-a"}, recipes, ["room-b"])
        self.assertEqual(report.cycles, [])
        self.assertIn("key-a", report.recovered)
        self.assertIn("key-b", report.recovered)
        held = simulate_plan(report.available_initial, report.plan)
        self.assertIn("key-b", held)


class TestAliasAndThresholdBoundary(unittest.TestCase):
    """边界: 同一材料的两个别名只算一个; 阈值恰好等于持有数。"""

    def test_two_aliases_count_as_one_material(self):
        creds = {"badge-x": "room-a", "badge-y": "room-a"}  # 同一材料的两个别名
        recipes = [Recipe("door", ["badge-x", "badge-y"], need=2)]
        (report,) = run_drill(
            creds, recipes, ["room-b"], aliases={"badge-y": "badge-x"}
        )
        self.assertIn("door", report.blocked)
        self.assertEqual(report.plan, [])

    def test_distinct_material_at_exact_threshold_unlocks(self):
        # 交错边界: 两个别名 + 一个真正不同的材料, need=2 恰好凑齐
        creds = {"badge-x": "room-a", "badge-y": "room-a", "token-admin": "room-a"}
        recipes = [Recipe("door", ["badge-x", "badge-y", "token-admin"], need=2)]
        (report,) = run_drill(
            creds, recipes, ["room-b"], aliases={"badge-y": "badge-x"}
        )
        self.assertIn("door", report.recovered)
        held = simulate_plan(report.available_initial, report.plan)
        self.assertIn("door", held)

    def test_threshold_one_below_and_at_count(self):
        # need=2 持有 2 种可解锁; 撤掉故障域后只剩 1 种则不可解锁
        creds = {"mat-1": "room-a", "mat-2": "room-b"}
        recipes = [Recipe("door", ["mat-1", "mat-2"], need=2)]
        kept, lost = run_drill(creds, recipes, ["room-c", "room-b"])
        self.assertIn("door", kept.recovered)   # 撤掉无关域, 两种材料都在
        self.assertIn("door", lost.blocked)     # 撤掉 room-b, 只剩 1 种
        self.assertEqual(lost.removed, ["mat-2"])


class TestInvalidInputRejected(unittest.TestCase):
    """应被拒绝: 永不可满足或非法的配方。"""

    def test_need_exceeds_distinct_materials(self):
        with self.assertRaises(ValueError):
            run_drill({}, [Recipe("door", ["a", "b"], need=3)], ["room-a"])

    def test_need_must_be_positive(self):
        with self.assertRaises(ValueError):
            run_drill({}, [Recipe("door", ["a"], need=0)], ["room-a"])

    def test_empty_credential_name_rejected(self):
        with self.assertRaises(ValueError):
            run_drill([Credential("", "room-a")], [], ["room-a"])

    def test_empty_domain_rejected(self):
        with self.assertRaises(ValueError):
            run_drill([Credential("badge-x", "")], [], ["room-a"])


if __name__ == "__main__":
    unittest.main()
