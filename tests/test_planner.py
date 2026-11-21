"""plan_recovery 的正常、边界与交错场景测试。

所有期望数值均手工推导；同时用测试内独立实现的重算函数
（不调用被测代码的目标函数）交叉校验字节总量、损失与依赖可行性。
"""

import unittest
from itertools import product

from dr_planner import (
    REASON_BUDGET,
    REASON_EMPTY_SERVICE,
    REASON_INFEASIBLE_DEPS,
    Backup,
    RecoveryResult,
    Service,
    plan_recovery,
)


def b(bid, position, size, reqs=None):
    """构造备份的简写。"""
    return Backup(
        backup_id=bid,
        position=position,
        bytes_required=size,
        requirements=frozenset((reqs or {}).items()),
    )


def recompute(services, chosen):
    """独立重算 (总字节, 加权损失)，避免复用被测实现的代码。"""
    latest = {s.service_id: max(x.position for x in s.backups) for s in services}
    total = sum(x.bytes_required for x in chosen)
    loss = sum(
        (latest[s.service_id] - x.position) * s.weight
        for s, x in zip(services, chosen)
    )
    return total, loss


def check_deps(services, chosen):
    """独立依赖校验：自引用 / 循环引用均按最终位置比较。"""
    pos = {s.service_id: x.position for s, x in zip(services, chosen)}
    for x in chosen:
        for ref, minimum in x.requirements:
            if pos.get(ref, -1) < minimum:
                return False
    return True


def all_feasible_combos(services):
    """枚举全部依赖可行组合（测试用参考实现）。"""
    return [
        combo
        for combo in product(*[list(s.backups) for s in services])
        if check_deps(services, combo)
    ]


class TwoIndependentServicesTest(unittest.TestCase):
    """场景 1：无依赖的两个服务，纯预算 + 损失选择。"""

    def setUp(self):
        self.s1 = Service("svc_a", weight=2, backups=(b("a1", 10, 100), b("a2", 20, 300)))
        self.s2 = Service("svc_b", weight=1, backups=(b("b1", 5, 200), b("b2", 15, 400)))
        self.services = [self.s1, self.s2]

    def test_budget_covers_latest_picks_latest_with_zero_loss(self):
        result = plan_recovery(self.services, 1000)
        self.assertTrue(result.feasible)
        self.assertEqual([x.backup_id for x in result.selected], ["a2", "b2"])
        self.assertEqual(result.total_bytes, 700)
        self.assertEqual(result.loss, 0)
        self.assertEqual(result.reason, None)
        total, loss = recompute(self.services, result.selected)
        self.assertEqual((total, loss), (700, 0))

    def test_budget_exact_boundary_is_accepted(self):
        # 最新组合恰好 700，等于上限时必须可行（闭区间）。
        result = plan_recovery(self.services, 700)
        self.assertTrue(result.feasible)
        self.assertEqual([x.backup_id for x in result.selected], ["a2", "b2"])

    def test_tight_budget_forces_older_backups(self):
        # 预算 300：可行组合只有 (a1, b1)=300。
        result = plan_recovery(self.services, 300)
        self.assertTrue(result.feasible)
        self.assertEqual([x.backup_id for x in result.selected], ["a1", "b1"])
        # 损失 = (20-10)*2 + (15-5)*1 = 20 + 10
        self.assertEqual(result.loss, 30)
        self.assertEqual(result.total_bytes, 300)
        total, loss = recompute(self.services, result.selected)
        self.assertEqual((total, loss), (300, 30))

    def test_budget_299_infeasible_but_increase_helps(self):
        result = plan_recovery(self.services, 299)
        self.assertFalse(result.feasible)
        self.assertEqual(result.reason, REASON_BUDGET)
        self.assertTrue(result.budget_can_fix)
        # 依赖可行的最小字节组合就是 (a1, b1)=300。
        self.assertEqual(result.required_budget, 300)
        self.assertEqual([x.backup_id for x in result.selected], ["a1", "b1"])
        # 放宽到所需预算后重跑，应当可解且选择一致。
        rerun = plan_recovery(self.services, result.required_budget)
        self.assertTrue(rerun.feasible)
        self.assertEqual(rerun.selected, result.selected)


class ForcingTradeoffTest(unittest.TestCase):
    """场景 2：某服务的新备份会迫使另一服务选大得多的副本。"""

    def setUp(self):
        self.x = Service(
            "x",
            weight=1,
            backups=(
                b("x_old", 10, 100),
                b("x_new", 20, 150, {"y": 30}),
            ),
        )
        self.y = Service(
            "y",
            weight=1,
            backups=(
                b("y_small", 20, 50),
                b("y_big", 30, 900),
            ),
        )
        self.services = [self.x, self.y]

    def test_new_backup_forces_large_peer_so_old_is_chosen(self):
        # 可行组合：
        #   (x_old, y_small)=150, 损失 (20-10)*1+(30-20)*1=20
        #   (x_old, y_big) =1000, 损失 10
        #   (x_new, y_big) =1050, 损失 0（x_new 要求 y>=30）
        # 预算 700 只能选第一个：单看 x 的最新备份反而无解。
        result = plan_recovery(self.services, 700)
        self.assertTrue(result.feasible)
        self.assertEqual([x.backup_id for x in result.selected], ["x_old", "y_small"])
        self.assertEqual(result.total_bytes, 150)
        self.assertEqual(result.loss, 20)

    def test_large_budget_prefers_zero_loss_combination(self):
        result = plan_recovery(self.services, 1050)
        self.assertTrue(result.feasible)
        self.assertEqual([x.backup_id for x in result.selected], ["x_new", "y_big"])
        self.assertEqual(result.total_bytes, 1050)
        self.assertEqual(result.loss, 0)

    def test_budget_between_feasible_and_forced_reports_required_budget(self):
        # 200 容得下旧组合 (150)，其实可解；这里测无解区间的边界 149。
        result = plan_recovery(self.services, 149)
        self.assertFalse(result.feasible)
        self.assertTrue(result.budget_can_fix)
        self.assertEqual(result.reason, REASON_BUDGET)
        self.assertEqual(result.required_budget, 150)
        self.assertEqual([x.backup_id for x in result.selected], ["x_old", "y_small"])

    def test_required_budget_is_global_minimum_over_feasible_combos(self):
        result = plan_recovery(self.services, 0)
        combos = all_feasible_combos(self.services)
        min_bytes = min(recompute(self.services, c)[0] for c in combos)
        self.assertEqual(result.required_budget, min_bytes)
        self.assertEqual(result.total_bytes, min_bytes)

    def test_relaxed_path_matches_feasible_path_at_required_budget(self):
        # 最小字节组合不唯一时，放宽路径与把预算提到 required_budget
        # 后的有解路径必须给出同一个选择（次级键保持一致的确定性）。
        svc = Service(
            "s",
            1,
            (b("a-lossy", 1, 100), b("b-fresh", 5, 100)),
        )
        blocked = plan_recovery([svc], 0)
        self.assertFalse(blocked.feasible)
        self.assertEqual(blocked.required_budget, 100)
        unblocked = plan_recovery([svc], blocked.required_budget)
        self.assertTrue(unblocked.feasible)
        self.assertEqual(unblocked.selected, blocked.selected)
        self.assertEqual(unblocked.loss, blocked.loss)
        self.assertEqual(blocked.selected[0].backup_id, "b-fresh")


class CyclicInterlockTest(unittest.TestCase):
    """场景 3：服务间循环引用，依赖只看最终位置，环本身不造成死锁。"""

    def setUp(self):
        self.p = Service(
            "p",
            weight=1,
            backups=(b("p1", 10, 100), b("p2", 20, 400, {"q": 20})),
        )
        self.q = Service(
            "q",
            weight=1,
            backups=(b("q1", 10, 100), b("q2", 20, 400, {"p": 20})),
        )
        self.services = [self.p, self.q]
        # 依赖可行组合：(p1,q1)=200 与 (p2,q2)=800；单边升级互相不满足。

    def test_cycle_allows_two_joint_levels(self):
        combos = all_feasible_combos(self.services)
        self.assertEqual(len(combos), 2)
        self.assertEqual(
            sorted(tuple(x.backup_id for x in c) for c in combos),
            [("p1", "q1"), ("p2", "q2")],
        )

    def test_low_budget_picks_joint_old_level(self):
        result = plan_recovery(self.services, 500)
        self.assertTrue(result.feasible)
        self.assertEqual([x.backup_id for x in result.selected], ["p1", "q1"])
        # 损失 = (20-10)*1 + (20-10)*1 = 20
        self.assertEqual(result.loss, 20)
        self.assertEqual(result.total_bytes, 200)

    def test_high_budget_picks_joint_new_level_zero_loss(self):
        result = plan_recovery(self.services, 800)
        self.assertTrue(result.feasible)
        self.assertEqual([x.backup_id for x in result.selected], ["p2", "q2"])
        self.assertEqual(result.loss, 0)
        self.assertEqual(result.total_bytes, 800)

    def test_budget_below_old_level_reports_minimum_feasible_budget(self):
        # 旧档联合选择需要 200；预算 199 时预算内无可行组合，
        # 放宽到 200 即可解决（最小字节的可行组合才是答案，而非 800）。
        result = plan_recovery(self.services, 199)
        self.assertFalse(result.feasible)
        self.assertEqual(result.reason, REASON_BUDGET)
        self.assertTrue(result.budget_can_fix)
        self.assertEqual(result.required_budget, 200)
        self.assertEqual([x.backup_id for x in result.selected], ["p1", "q1"])

    def test_budget_between_old_and_new_level_still_returns_old_level(self):
        # 预算 799 容得下旧档 (200) 但够不到新档 (800)：有解，返回旧档。
        result = plan_recovery(self.services, 799)
        self.assertTrue(result.feasible)
        self.assertEqual([x.backup_id for x in result.selected], ["p1", "q1"])
        self.assertEqual(result.total_bytes, 200)
        self.assertEqual(result.loss, 20)


class StructurallyInfeasibleTest(unittest.TestCase):
    """场景 4：依赖指向不存在的位置，增加字节永远无法解决。"""

    def setUp(self):
        self.m = Service(
            "m",
            weight=1,
            backups=(b("m1", 10, 100, {"n": 999}),),
        )
        self.n = Service("n", weight=1, backups=(b("n1", 10, 100),))
        self.services = [self.m, self.n]

    def test_no_feasible_combination_at_any_budget(self):
        result = plan_recovery(self.services, 10**9)
        self.assertFalse(result.feasible)
        self.assertFalse(result.budget_can_fix)
        self.assertEqual(result.reason, REASON_INFEASIBLE_DEPS)
        self.assertIsNone(result.required_budget)
        self.assertIsNone(result.selected)
        self.assertEqual(all_feasible_combos(self.services), [])

    def test_dependency_on_unknown_service_is_structural(self):
        lonely = Service(
            "lonely",
            weight=1,
            backups=(b("l1", 1, 10, {"ghost": 1}),),
        )
        result = plan_recovery([lonely], 10**9)
        self.assertFalse(result.feasible)
        self.assertFalse(result.budget_can_fix)
        self.assertEqual(result.reason, REASON_INFEASIBLE_DEPS)


class EmptyBackupSetTest(unittest.TestCase):
    """场景 5：某服务没有候选备份（边界）。"""

    def test_empty_service_is_structural_not_budget(self):
        services = [
            Service("filled", weight=1, backups=(b("f1", 1, 10),)),
            Service("empty", weight=1, backups=()),
        ]
        result = plan_recovery(services, 10**9)
        self.assertFalse(result.feasible)
        self.assertFalse(result.budget_can_fix)
        self.assertEqual(result.reason, REASON_EMPTY_SERVICE)
        self.assertIsNone(result.required_budget)


class SelfReferenceTest(unittest.TestCase):
    """场景 6：备份依赖本服务的最低位置（自引用）。"""

    def test_self_requirement_below_own_position_passes(self):
        svc = Service(
            "solo",
            weight=2,
            backups=(
                b("s1", 10, 100, {"solo": 10}),
                b("s2", 20, 300, {"solo": 20}),
            ),
        )
        result = plan_recovery([svc], 400)
        self.assertTrue(result.feasible)
        self.assertEqual(result.selected[0].backup_id, "s2")
        self.assertEqual(result.loss, 0)

    def test_self_requirement_above_own_position_fails(self):
        svc = Service(
            "solo",
            weight=2,
            backups=(b("s1", 10, 100, {"solo": 11}),),
        )
        result = plan_recovery([svc], 400)
        self.assertFalse(result.feasible)
        self.assertFalse(result.budget_can_fix)
        self.assertEqual(result.reason, REASON_INFEASIBLE_DEPS)


class WeightFlipsChoiceTest(unittest.TestCase):
    """场景 7：同样的可行集合，权重变化让最优解在两个服务间翻转。"""

    def setUp(self):
        # 无依赖；新备份更贵，预算 300 无法同时选两个新备份 (需 500)：
        #   (u_old,v_old)=100, 损失 10*wu + 10*wv
        #   (u_old,v_new)=250, 损失 10*wu
        #   (u_new,v_old)=300, 损失 10*wv
        #   (u_new,v_new)=500 —— 预算内不可行
        self.u_old = b("u_old", 10, 50)
        self.u_new = b("u_new", 20, 250)
        self.v_old = b("v_old", 10, 50)
        self.v_new = b("v_new", 20, 200)

    def _services(self, wu, wv):
        return [
            Service("u", weight=wu, backups=(self.u_old, self.u_new)),
            Service("v", weight=wv, backups=(self.v_old, self.v_new)),
        ]

    def test_higher_weight_on_u_protects_u(self):
        result = plan_recovery(self._services(3, 1), 300)
        self.assertTrue(result.feasible)
        # u 权重高，宁愿 v 落后：选 (u_new, v_old)，损失 10*1=10。
        self.assertEqual([x.backup_id for x in result.selected], ["u_new", "v_old"])
        self.assertEqual(result.total_bytes, 300)
        self.assertEqual(result.loss, 10)

    def test_higher_weight_on_v_protects_v(self):
        result = plan_recovery(self._services(1, 3), 300)
        self.assertEqual([x.backup_id for x in result.selected], ["u_old", "v_new"])
        self.assertEqual(result.total_bytes, 250)
        self.assertEqual(result.loss, 10)

    def test_equal_weights_tie_broken_deterministically(self):
        result = plan_recovery(self._services(1, 1), 300)
        # 两个候选损失相同 (10)，先按字节：(u_old,v_new)=250 < (u_new,v_old)=300。
        self.assertEqual([x.backup_id for x in result.selected], ["u_old", "v_new"])
        result_again = plan_recovery(self._services(1, 1), 300)
        self.assertEqual(result_again.selected, result.selected)

    def test_equal_weights_and_bytes_tie_broken_by_backup_id(self):
        # 构造损失与字节都相同的平局，验证字典序兜底规则。
        # 新备份加价：零损失的 (u_b,v_b)=120 超出预算 110；
        # 两个“一新一旧”组合均为 110 字节、损失均为 10。
        u = Service("u", 1, (b("u_a", 10, 50), b("u_b", 20, 60)))
        v = Service("v", 1, (b("v_a", 10, 50), b("v_b", 20, 60)))
        result = plan_recovery([u, v], 110)
        self.assertTrue(result.feasible)
        # 按备份标识字典序：u_a < u_b，故 (u_a,v_b) 胜出。
        self.assertEqual([x.backup_id for x in result.selected], ["u_a", "v_b"])


class TieBreakByBytesTest(unittest.TestCase):
    """场景 8：损失相同时优先字节更少的选择。"""

    def test_smaller_backup_wins_on_equal_loss(self):
        # 同一位置、不同字节：损失恒等，字节小者胜。
        svc = Service(
            "z",
            weight=1,
            backups=(b("z_big", 5, 500), b("z_small", 5, 100)),
        )
        result = plan_recovery([svc], 1000)
        self.assertTrue(result.feasible)
        self.assertEqual(result.selected[0].backup_id, "z_small")
        self.assertEqual(result.total_bytes, 100)


class MaxScaleSixBySixTest(unittest.TestCase):
    """场景 9：上限规模 6 服务 × 6 备份（46656 组合），交叉验证全局最优。"""

    def setUp(self):
        self.services = []
        for i in range(6):
            sid = f"s{i}"
            backups = []
            for k in range(6):
                reqs = {}
                if k >= 3 and i > 0:
                    # 交错依赖：升级到后半段要求前一个服务也达到同档位置。
                    reqs = {f"s{i - 1}": k * 10}
                backups.append(b(f"{sid}_b{k}", k * 10, 10 + k * 5, reqs))
            self.services.append(
                Service(sid, weight=(i % 3) + 1, backups=tuple(backups))
            )

    def _reference_optimum(self, budget):
        """独立参考实现：全枚举后按题面优先级取最小。"""
        best = None
        for combo in product(*[list(s.backups) for s in self.services]):
            if not check_deps(self.services, combo):
                continue
            total, loss = recompute(self.services, combo)
            if total > budget:
                continue
            key = (loss, total, tuple(x.backup_id for x in combo))
            if best is None or key < best[0]:
                best = (key, combo, total, loss)
        return best

    def test_planner_matches_independent_reference(self):
        for budget in (0, 50, 120, 300, 10**6):
            with self.subTest(budget=budget):
                result = plan_recovery(self.services, budget)
                ref = self._reference_optimum(budget)
                if ref is None:
                    self.assertFalse(result.feasible)
                    # 参考实现同时验证 required_budget 的最小值语义。
                    feasible = all_feasible_combos(self.services)
                    min_bytes = min(
                        recompute(self.services, c)[0] for c in feasible
                    )
                    self.assertEqual(result.required_budget, min_bytes)
                else:
                    _, combo, total, loss = ref
                    self.assertTrue(result.feasible)
                    self.assertEqual(result.selected, combo)
                    self.assertEqual(result.total_bytes, total)
                    self.assertEqual(result.loss, loss)

    def test_full_size_runs_and_returns_complete_selection(self):
        result = plan_recovery(self.services, 10**6)
        self.assertTrue(result.feasible)
        self.assertEqual(len(result.selected), 6)
        self.assertEqual(
            [s.service_id for s in self.services],
            [f"s{i}" for i in range(6)],
        )
        # 每个服务都有位置 50 的备份且链式依赖可同时满足，损失为 0。
        self.assertEqual(result.loss, 0)
        self.assertTrue(all(x.position == 50 for x in result.selected))


class ZeroBudgetAndZeroWeightTest(unittest.TestCase):
    """场景 10：零字节预算与零权重的边界语义。"""

    def test_zero_budget_needs_zero_byte_backups(self):
        svc = Service(
            "free",
            weight=1,
            backups=(b("paid", 5, 10), b("free", 1, 0)),
        )
        ok = plan_recovery([svc], 0)
        self.assertTrue(ok.feasible)
        self.assertEqual(ok.selected[0].backup_id, "free")
        self.assertEqual(ok.total_bytes, 0)

        blocked = Service(
            "blocked",
            weight=1,
            backups=(b("only", 5, 1),),
        )
        result = plan_recovery([blocked], 0)
        self.assertFalse(result.feasible)
        self.assertTrue(result.budget_can_fix)
        self.assertEqual(result.required_budget, 1)

    def test_zero_weight_service_does_not_affect_loss_but_still_chosen(self):
        # ghost 权重为 0：它的位置差从不计入损失，因此字节最优的旧备份
        # 在任何预算下都会被选中（损失与新备份相同，二级规则偏向字节更小）。
        services = [
            Service("heavy", weight=5, backups=(b("h1", 10, 100), b("h2", 20, 100))),
            Service("ghost", weight=0, backups=(b("g1", 1, 10), b("g2", 9, 50))),
        ]

        # 预算 149：h2(100)+g2(50)=150 超限，ghost 只能配 g1。
        tight = plan_recovery(services, 149)
        self.assertTrue(tight.feasible)
        self.assertEqual(
            [x.backup_id for x in tight.selected], ["h2", "g1"]
        )
        total, loss = recompute(services, tight.selected)
        self.assertEqual(tight.total_bytes, 110)
        self.assertEqual(total, 110)
        # 独立重算与被测结果一致，ghost 落后 8 个位置但加权损失仍为 0。
        self.assertEqual(tight.loss, loss)
        self.assertEqual(tight.loss, 0)

        # 预算放宽后 ghost 仍选 g1：它的升级不减少任何加权损失，
        # 却增加字节，故目标函数永远偏好便宜备份——这是零权重的正确语义。
        generous = plan_recovery(services, 10**6)
        self.assertEqual(
            [x.backup_id for x in generous.selected], ["h2", "g1"]
        )
        self.assertEqual(generous.loss, 0)
        self.assertEqual(generous.total_bytes, 110)

    def test_zero_weight_peer_never_drives_the_objective(self):
        # 对照：把 ghost 权重调到 1，预算放宽后升级 g2 能把损失从 8 降到 0，
        # 规划器就会选择更贵的 g2——与权重为 0 时永远选 g1 形成对照，
        # 证明权重项真实参与目标计算。
        weighted = [
            Service("heavy", weight=5, backups=(b("h1", 10, 100), b("h2", 20, 100))),
            Service("ghost", weight=1, backups=(b("g1", 1, 10), b("g2", 9, 50))),
        ]
        result = plan_recovery(weighted, 10**6)
        self.assertEqual(
            [x.backup_id for x in result.selected], ["h2", "g2"]
        )
        self.assertEqual(result.loss, 0)

        tight = plan_recovery(weighted, 149)
        self.assertEqual(
            [x.backup_id for x in tight.selected], ["h2", "g1"]
        )
        # ghost 位置差 (9-1)=8 且权重为 1，损失非零。
        self.assertEqual(tight.loss, 8)


class InputValidationTest(unittest.TestCase):
    """场景 11：非法入参的防御性校验。"""

    def test_empty_service_list_rejected(self):
        with self.assertRaises(ValueError):
            plan_recovery([], 100)

    def test_duplicate_service_id_rejected(self):
        services = [
            Service("dup", 1, (b("a", 1, 1),)),
            Service("dup", 1, (b("b", 1, 1),)),
        ]
        with self.assertRaises(ValueError):
            plan_recovery(services, 100)

    def test_service_count_limit_enforced(self):
        seven = [
            Service(f"s{i}", 1, (b("x", 1, 1),)) for i in range(7)
        ]
        with self.assertRaises(ValueError):
            plan_recovery(seven, 100)

    def test_backup_count_limit_enforced(self):
        seven_backups = tuple(b(f"b{i}", i, 1) for i in range(7))
        with self.assertRaises(ValueError):
            plan_recovery([Service("s", 1, seven_backups)], 100)

    def test_duplicate_backup_id_rejected(self):
        with self.assertRaises(ValueError):
            Service("s", 1, (b("same", 1, 1), b("same", 2, 2)))

    def test_negative_values_rejected(self):
        with self.assertRaises(ValueError):
            Backup("x", -1, 10)
        with self.assertRaises(ValueError):
            Backup("x", 1, -10)
        with self.assertRaises(ValueError):
            Service("s", -1, (b("x", 1, 10),))
        with self.assertRaises(ValueError):
            plan_recovery([Service("s", 1, (b("x", 1, 10),))], -1)

    def test_duplicate_requirement_keeps_strictest(self):
        backup = Backup(
            "x",
            5,
            10,
            frozenset((("y", 3), ("y", 8))),
        )
        self.assertEqual(dict(backup.requirements), {"y": 8})

    def test_result_is_immutable_value_object(self):
        result = plan_recovery([Service("s", 1, (b("x", 1, 10),))], 100)
        self.assertIsInstance(result, RecoveryResult)
        with self.assertRaises(Exception):
            result.feasible = False  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main(verbosity=2)
