"""规划器验收测试（unittest，真实断言）。

覆盖：
* 题面基准：2 微批 / 3 段 / 前后向均 1 / 两端容量 2，中段 1 -> 2，
  最短时间 10 -> 8（一个被容量约束阻止的结果 vs 一个可接受结果）。
* 边界 / 交错：瞬时移交（同刻先完成再开工）、单微批、单段、
  4 微批上限、混合时长最优性。
* 独立校验器：容量越界、设备重叠、缺操作、时长不符、输入非法均被拒绝。
"""

import os
import sys
import time
import unittest

# 兼容 python -I（隔离模式不自动注入 cwd）：把工作区根目录加入导入路径。
_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE not in sys.path:
    sys.path.insert(0, _WORKSPACE)

from pipeline_planner import (
    Operation,
    Schedule,
    StageConfig,
    plan,
    verify_schedule,
)


def uniform_stages(cap_middle: int) -> list[StageConfig]:
    return [
        StageConfig(1, 1, 2),
        StageConfig(1, 1, cap_middle),
        StageConfig(1, 1, 2),
    ]


class PlannerReferenceTests(unittest.TestCase):
    def test_middle_capacity_one_makespan_ten(self):
        """中段容量 1：槽位迫使串行，最短整步时间必须为 10。"""
        sched = plan(uniform_stages(1), num_microbatches=2)
        report = verify_schedule(uniform_stages(1), 2, sched)
        self.assertEqual(sched.makespan, 10)
        self.assertEqual(report["makespan"], 10)
        # 中段峰值正好顶到容量 1，说明瓶颈确实是激活槽。
        self.assertEqual(sched.activation_peak, (2, 1, 1))

    def test_middle_capacity_two_makespan_eight(self):
        """中段容量升到 2：交错流水恢复，最短整步时间必须降为 8。"""
        sched = plan(uniform_stages(2), num_microbatches=2)
        report = verify_schedule(uniform_stages(2), 2, sched)
        self.assertEqual(sched.makespan, 8)
        self.assertEqual(report["makespan"], 8)
        # 中段允许 2 份激活并存，峰值顶到容量；末段同样出现两微批交错。
        self.assertEqual(sched.activation_peak, (2, 2, 2))
        self.assertLessEqual(sched.activation_peak[1], 2)

    def test_capacity_blocks_overlap(self):
        """交叉约束：容量 1 的段上两个微批的激活占用区间不得交错。"""
        stages = uniform_stages(1)
        sched = plan(stages, num_microbatches=2)
        iv = {
            (o.kind, o.microbatch): (o.start, o.end)
            for o in sched.operations
            if o.stage == 1
        }
        # 微批 0 的反向结束（释放槽）<= 微批 1 的前向开始（占用槽）。
        b0_end = iv[("backward", 0)][1]
        f1_start = iv[("forward", 1)][0]
        self.assertLessEqual(b0_end, f1_start)
        # 且该阻塞真实地把工期从 8 拖到 10。
        self.assertEqual(sched.makespan, 10)

    def test_instant_handoff_boundary(self):
        """边界：同刻先完成再开工合法，调度中必须存在首尾相接的操作。"""
        sched = plan(uniform_stages(1), num_microbatches=2)
        verify_schedule(uniform_stages(1), 2, sched)
        # 收集所有端点，至少存在一对“某操作 end == 另一操作 start”。
        ends = {o.end for o in sched.operations}
        starts = {o.start for o in sched.operations}
        self.assertTrue(
            ends & starts,
            "瞬时移交要求调度里出现同一时刻的完成/开工衔接",
        )
        # 微批内：后一段前向不得早于前一段前向结束（等号即瞬时移交）。
        for m in range(2):
            for s in range(2):
                fa = next(o for o in sched.operations
                          if o.kind == "forward" and o.stage == s and o.microbatch == m)
                fb = next(o for o in sched.operations
                          if o.kind == "forward" and o.stage == s + 1 and o.microbatch == m)
                self.assertGreaterEqual(fb.start, fa.end)
        # cap=2 的紧凑调度中至少有一对微批做到真正零等待移交。
        tight = plan(uniform_stages(2), num_microbatches=2)
        verify_schedule(uniform_stages(2), 2, tight)
        self.assertTrue(
            any(
                fb.start == fa.end
                for m in range(2)
                for s in range(2)
                for fa in [next(o for o in tight.operations
                                if o.kind == "forward" and o.stage == s and o.microbatch == m)]
                for fb in [next(o for o in tight.operations
                                if o.kind == "forward" and o.stage == s + 1 and o.microbatch == m)]
            )
        )


class SmallShapeTests(unittest.TestCase):
    def test_single_microbatch_three_stages(self):
        """1 微批 3 段、各时长 1：纯串行 6 拍，峰值恒为 1。"""
        stages = [StageConfig(1, 1, 1) for _ in range(3)]
        sched = plan(stages, num_microbatches=1)
        verify_schedule(stages, 1, sched)
        self.assertEqual(sched.makespan, 6)
        self.assertEqual(sched.activation_peak, (1, 1, 1))

    def test_single_stage_two_microbatches(self):
        """单段 2 微批、时长 1：共 4 拍，容量 1 也可行，且每个微批 F 先于 B。"""
        stages = [StageConfig(1, 1, 1)]
        sched = plan(stages, num_microbatches=2)
        verify_schedule(stages, 2, sched)
        self.assertEqual(sched.makespan, 4)
        self.assertEqual(sched.activation_peak, (1,))
        by_mb = {m: sorted((o.kind, o.start) for o in sched.operations
                           if o.microbatch == m) for m in range(2)}
        for m in range(2):
            kinds = [k for k, _ in by_mb[m]]
            self.assertEqual(kinds, ["backward", "forward"])
            f_start = next(st for k, st in by_mb[m] if k == "forward")
            b_start = next(st for k, st in by_mb[m] if k == "backward")
            self.assertLess(f_start, b_start)

    def test_mixed_durations_optimal(self):
        """2 段混合时长 (F,B)=(1,3)/(3,1)，2 微批：最优 12 拍。

        下界复核：段0工作量 2*1+2*3=8，段1=2*3+2*1=8，关键链 8；
        但段0两个 3 拍反向必须串行（共 6 拍），且最早 5 点才能开工，
        两条可行反向序都收敛到 12，故 12 即全局最优。
        """
        stages = [StageConfig(1, 3, 2), StageConfig(3, 1, 2)]
        sched = plan(stages, num_microbatches=2)
        report = verify_schedule(stages, 2, sched)
        self.assertEqual(sched.makespan, 12)
        # 段0 两微批前向紧挨 -> 峰值2；段1 两微批激活首尾相接 -> 峰值1。
        self.assertEqual(report["activation_peak"], (2, 1))
        for s, cap in enumerate((2, 2)):
            self.assertLessEqual(sched.activation_peak[s], cap)
        # 段0 的两个反向时长各 3 且串行，合计占用 6 个时间单位。
        b0 = sorted(
            (o.start, o.end) for o in sched.operations
            if o.stage == 0 and o.kind == "backward"
        )
        self.assertEqual(sum(e - s for s, e in b0), 6)
        self.assertLessEqual(b0[0][1], b0[1][0])

    def test_four_microbatches_upper_shape(self):
        """规模上限 4 微批 3 段：快速求解且全部约束可独立复算通过。"""
        stages = [StageConfig(2, 1, 2), StageConfig(1, 2, 2),
                  StageConfig(1, 1, 3)]
        t0 = time.perf_counter()
        sched = plan(stages, num_microbatches=4)
        elapsed = time.perf_counter() - t0
        report = verify_schedule(stages, 4, sched)
        self.assertLess(elapsed, 10.0, "DFS 搜索超时，存在组合爆炸")
        self.assertEqual(len(sched.operations), 24)
        # 设备工作量下界：段0 = 4*(2+1)=12，段1 = 4*(1+2)=12，段2 = 4*2=8。
        self.assertGreaterEqual(sched.makespan, 12)
        for s, cap in enumerate((2, 2, 3)):
            self.assertLessEqual(sched.activation_peak[s], cap)
        self.assertEqual(report["microbatches"], 4)


class VerifyRejectTests(unittest.TestCase):
    def _good_schedule(self) -> tuple[list[StageConfig], Schedule]:
        stages = uniform_stages(1)
        return stages, plan(stages, num_microbatches=2)

    def test_reject_capacity_violation(self):
        """应被阻止：中段容量 1 却塞入两个同时占用激活槽的微批。"""
        stages, good = self._good_schedule()
        # 把微批 1 的中段前向提前到微批 0 中段反向结束之前 -> 占用重叠。
        hacked_ops = []
        for op in good.operations:
            if op.kind == "forward" and op.stage == 1 and op.microbatch == 1:
                op = Operation(op.stage, op.microbatch, op.kind,
                               start=1, end=2)
            hacked_ops.append(op)
        hacked = Schedule(good.makespan, tuple(hacked_ops), good.activation_peak)
        with self.assertRaises(ValueError):
            verify_schedule(stages, 2, hacked)

    def test_reject_device_overlap(self):
        """应被阻止：同段两个操作时间重叠违反单设备互斥。"""
        stages, good = self._good_schedule()
        # 段0 微批1 的前向合法区间是 [1,2)；把微批0 的段0反向改成 [1,4)
        # （时长仍为配置值 1 不成立，故同时构造一个时长正确但区间相撞的版本：
        # 直接把微批0段0反向移动到 [1,2)，与微批1段0前向严格重叠）。
        hacked_ops = []
        for op in good.operations:
            if op.kind == "backward" and op.stage == 0 and op.microbatch == 0:
                op = Operation(op.stage, op.microbatch, op.kind,
                               start=1, end=2)
            hacked_ops.append(op)
        hacked = Schedule(good.makespan, tuple(hacked_ops), good.activation_peak)
        with self.assertRaises(ValueError):
            verify_schedule(stages, 2, hacked)

    def test_reject_wrong_duration_and_missing_op(self):
        """应被阻止：时长与配置不符、缺少操作。"""
        stages, good = self._good_schedule()
        wrong = [op for op in good.operations
                 if not (op.kind == "forward" and op.stage == 2 and op.microbatch == 1)]
        hacked = Schedule(good.makespan, tuple(wrong), good.activation_peak)
        with self.assertRaises(ValueError):
            verify_schedule(stages, 2, hacked)

        bad_dur = Operation(0, 0, "forward", 0, 2)  # 配置时长为 1
        replaced = tuple(bad_dur if (o.stage, o.microbatch, o.kind) == (0, 0, "forward")
                         else o for o in good.operations)
        hacked2 = Schedule(good.makespan, replaced, good.activation_peak)
        with self.assertRaises(ValueError):
            verify_schedule(stages, 2, hacked2)

    def test_reject_invalid_inputs(self):
        """输入边界：段数 / 微批数 / 正整数约束。"""
        with self.assertRaises(ValueError):
            plan([StageConfig(1, 1, 1)] * 4, 2)
        with self.assertRaises(ValueError):
            plan([StageConfig(1, 1, 1)], 5)
        with self.assertRaises(ValueError):
            plan([StageConfig(0, 1, 1)], 2)
        with self.assertRaises(ValueError):
            plan([StageConfig(1, -1, 1)], 2)
        with self.assertRaises(ValueError):
            plan([], 2)
        with self.assertRaises(TypeError):
            plan([StageConfig(1, 1, 1)], True)  # type: ignore[arg-type]

    def test_planner_output_self_consistent(self):
        """所有基准配置产出的调度都必须通过独立校验器。"""
        for cap in (1, 2):
            stages = uniform_stages(cap)
            sched = plan(stages, num_microbatches=2)
            report = verify_schedule(stages, 2, sched)
            self.assertEqual(report["operations"], 12)
            self.assertEqual(report["stages"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
