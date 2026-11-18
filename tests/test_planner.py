"""取证采集规划模块验收测试.

覆盖:
  - 可接受结果: 可行计划满足时限、必取覆盖、先决条件与原始状态顺序;
  - 应被阻止的结果: 排序环、自破坏、时限不足、无采集方式;
  - 边界/交错: 时限恰好等于总耗时、替代方案规避冲突、
    单次采集覆盖多种材料、可选类别不影响可行性.
"""
import unittest

from forensic_planner import (
    AcquisitionMethod,
    EvidenceCategory,
    plan_acquisition,
)


def standard_scenario(time_limit=180):
    """内存需原始状态; triage_tool 会改变内存, 必须排在 mem_capture 之后."""
    cats = [
        EvidenceCategory("memory", required=True, needs_raw_state=True),
        EvidenceCategory("disk", required=True),
        EvidenceCategory("logs", required=True),
    ]
    methods = [
        AcquisitionMethod("mem_capture", 15, covers={"memory"}),
        AcquisitionMethod("disk_image", 90, covers={"disk"}),
        AcquisitionMethod("triage_tool", 20, covers={"logs"},
                          alters={"memory"}),
    ]
    return cats, methods, time_limit


def order_of(result, method_id):
    for i, step in enumerate(result.schedule):
        if step.method_id == method_id:
            return i
    raise AssertionError(f"{method_id} 不在计划中")


class TestAcceptablePlans(unittest.TestCase):
    """可接受结果: 约束全部满足时应给出可行计划."""

    def test_feasible_plan_covers_required_within_limit(self):
        cats, methods, limit = standard_scenario()
        result = plan_acquisition(cats, methods, limit)
        self.assertTrue(result.feasible, result.conflicts)
        self.assertLessEqual(result.total_duration, limit)
        self.assertEqual(result.covered, {"memory", "disk", "logs"})
        self.assertEqual(len(result.schedule), 3)

    def test_raw_state_preserved_before_altering_action(self):
        cats, methods, limit = standard_scenario()
        result = plan_acquisition(cats, methods, limit)
        self.assertTrue(result.feasible, result.conflicts)
        # 需要原始状态的 memory 必须在会改变它的 triage_tool 之前保存
        self.assertLess(order_of(result, "mem_capture"),
                        order_of(result, "triage_tool"))

    def test_schedule_times_are_sequential_and_consistent(self):
        cats, methods, limit = standard_scenario()
        result = plan_acquisition(cats, methods, limit)
        durations = {m.id: m.duration for m in methods}
        t = 0
        for step in result.schedule:
            self.assertEqual(step.start, t)
            self.assertEqual(step.end, t + durations[step.method_id])
            t = step.end
        self.assertEqual(t, result.total_duration)

    def test_single_acquisition_covers_multiple_categories(self):
        cats = [
            EvidenceCategory("logs", required=True),
            EvidenceCategory("netstat", required=True),
        ]
        methods = [
            # 一次 triage 同时拿到 logs 与 netstat 两类材料
            AcquisitionMethod("triage", 10, covers={"logs", "netstat"}),
        ]
        result = plan_acquisition(cats, methods, 60)
        self.assertTrue(result.feasible, result.conflicts)
        self.assertEqual(len(result.schedule), 1)
        self.assertEqual(result.covered, {"logs", "netstat"})

    def test_prerequisite_is_scheduled_before_dependent(self):
        cats = [EvidenceCategory("disk", required=True)]
        methods = [
            AcquisitionMethod("disk_image", 60, covers={"disk"},
                              prerequisites={"write_blocker"}),
            AcquisitionMethod("write_blocker", 5, covers=set()),
        ]
        result = plan_acquisition(cats, methods, 120)
        self.assertTrue(result.feasible, result.conflicts)
        self.assertLess(order_of(result, "write_blocker"),
                        order_of(result, "disk_image"))
        self.assertEqual(result.total_duration, 65)

    def test_optional_category_may_be_left_uncovered(self):
        cats = [
            EvidenceCategory("disk", required=True),
            EvidenceCategory("clipboard", required=False),
        ]
        methods = [AcquisitionMethod("disk_image", 60, covers={"disk"})]
        result = plan_acquisition(cats, methods, 90)
        self.assertTrue(result.feasible, result.conflicts)
        self.assertNotIn("clipboard", result.covered)


class TestAlternativeMethods(unittest.TestCase):
    """交错场景: 一个类别有替代方式时, 允许换方案避开冲突."""

    def test_alternative_method_avoids_ordering_conflict(self):
        cats = [
            EvidenceCategory("memory", required=True, needs_raw_state=True),
            EvidenceCategory("logs", required=True),
        ]
        methods = [
            # logs 的方案 A 会改变内存, 且是 mem_capture 的先决条件 -> 成环
            AcquisitionMethod("triage_live", 10, covers={"logs"},
                              alters={"memory"}),
            AcquisitionMethod("mem_capture", 15, covers={"memory"},
                              prerequisites={"triage_live"}),
            # logs 的替代方案 B: 从磁盘镜像离线提取, 不碰内存
            AcquisitionMethod("logs_offline", 40, covers={"logs"}),
        ]
        # 注意: mem_capture 依赖 triage_live, 因此 memory 的保存必然
        # 触发 triage_live; 只有 logs 改选 logs_offline 并不能解开
        # memory 侧的环 -> 本场景应不可行
        result = plan_acquisition(cats, methods, 300)
        self.assertFalse(result.feasible)
        self.assertTrue(any(c.kind == "ordering_cycle"
                            for c in result.conflicts))

    def test_switching_plan_for_conflicting_category_resolves(self):
        cats = [
            EvidenceCategory("memory", required=True, needs_raw_state=True),
            EvidenceCategory("logs", required=True),
        ]
        methods = [
            AcquisitionMethod("mem_capture", 15, covers={"memory"}),
            # 方案 A: 快但会改变内存 —— 只要排在 mem_capture 后也可行
            AcquisitionMethod("triage_live", 10, covers={"logs"},
                              alters={"memory"}),
            # 方案 B: 慢但无副作用
            AcquisitionMethod("logs_offline", 40, covers={"logs"}),
        ]
        result = plan_acquisition(cats, methods, 300)
        self.assertTrue(result.feasible, result.conflicts)
        # 无论选哪个方案, memory 都必须在任何会改变它的动作前保存
        sched = [s.method_id for s in result.schedule]
        if "triage_live" in sched:
            self.assertLess(sched.index("mem_capture"),
                            sched.index("triage_live"))
        # 求解器偏好更短总耗时, 应选 triage_live 并排在 mem_capture 后
        self.assertIn("triage_live", sched)
        self.assertEqual(result.total_duration, 25)


class TestBlockedPlans(unittest.TestCase):
    """应被阻止/拒绝的结果及其阻塞约束报告."""

    def test_ordering_cycle_is_reported(self):
        cats = [
            EvidenceCategory("memory", required=True, needs_raw_state=True),
            EvidenceCategory("logs", required=True),
        ]
        methods = [
            # 唯一能保存 memory 的方式以 triage_live 为先决条件,
            # 而 triage_live 会改变 memory -> 互相阻塞
            AcquisitionMethod("mem_capture", 15, covers={"memory"},
                              prerequisites={"triage_live"}),
            AcquisitionMethod("triage_live", 10, covers={"logs"},
                              alters={"memory"}),
        ]
        result = plan_acquisition(cats, methods, 300)
        self.assertFalse(result.feasible)
        cycles = [c for c in result.conflicts if c.kind == "ordering_cycle"]
        self.assertTrue(cycles, result.conflicts)
        self.assertIn("mem_capture", cycles[0].methods)
        self.assertIn("triage_live", cycles[0].methods)
        self.assertIn("memory", cycles[0].categories)

    def test_self_altering_only_option_is_rejected(self):
        cats = [
            EvidenceCategory("firmware", required=True, needs_raw_state=True),
        ]
        methods = [
            # 唯一的读取方式本身会改写固件原始状态
            AcquisitionMethod("read_fw", 30, covers={"firmware"},
                              alters={"firmware"}),
        ]
        result = plan_acquisition(cats, methods, 300)
        self.assertFalse(result.feasible)
        kinds = {c.kind for c in result.conflicts}
        self.assertIn("self_alter", kinds)

    def test_time_limit_exceeded_is_reported(self):
        cats, methods, _ = standard_scenario()
        result = plan_acquisition(cats, methods, time_limit=100)
        self.assertFalse(result.feasible)
        kinds = {c.kind for c in result.conflicts}
        self.assertIn("time_limit_exceeded", kinds)

    def test_uncoverable_required_category_is_reported(self):
        cats = [EvidenceCategory("gps", required=True)]
        result = plan_acquisition(cats, [], 300)
        self.assertFalse(result.feasible)
        self.assertEqual(result.conflicts[0].kind, "uncoverable_category")
        self.assertIn("gps", result.conflicts[0].categories)


class TestBoundaries(unittest.TestCase):
    """边界: 时限恰好等于/小于最小总耗时."""

    def test_time_limit_exactly_equal_is_accepted(self):
        cats, methods, _ = standard_scenario()
        # 15 + 90 + 20 = 125, 恰好等于时限
        result = plan_acquisition(cats, methods, time_limit=125)
        self.assertTrue(result.feasible, result.conflicts)
        self.assertEqual(result.total_duration, 125)

    def test_time_limit_one_below_is_rejected(self):
        cats, methods, _ = standard_scenario()
        result = plan_acquisition(cats, methods, time_limit=124)
        self.assertFalse(result.feasible)
        kinds = {c.kind for c in result.conflicts}
        self.assertIn("time_limit_exceeded", kinds)

    def test_zero_duration_method_at_zero_limit(self):
        cats = [EvidenceCategory("logs", required=True)]
        methods = [AcquisitionMethod("snapshot", 0, covers={"logs"})]
        result = plan_acquisition(cats, methods, 0)
        self.assertTrue(result.feasible, result.conflicts)
        self.assertEqual(result.total_duration, 0)


class TestInputValidation(unittest.TestCase):
    def test_negative_time_limit_rejected(self):
        with self.assertRaises(ValueError):
            plan_acquisition([], [], -1)

    def test_negative_duration_rejected(self):
        cats = [EvidenceCategory("logs")]
        methods = [AcquisitionMethod("m", -5, covers={"logs"})]
        with self.assertRaises(ValueError):
            plan_acquisition(cats, methods, 10)

    def test_unknown_prerequisite_rejected(self):
        cats = [EvidenceCategory("logs")]
        methods = [AcquisitionMethod("m", 5, covers={"logs"},
                                     prerequisites={"ghost"})]
        with self.assertRaises(ValueError):
            plan_acquisition(cats, methods, 10)

    def test_unknown_covered_category_rejected(self):
        methods = [AcquisitionMethod("m", 5, covers={"ghost"})]
        with self.assertRaises(ValueError):
            plan_acquisition([], methods, 10)

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError):
            plan_acquisition(
                [EvidenceCategory("a"), EvidenceCategory("a")], [], 10)


if __name__ == "__main__":
    unittest.main()
