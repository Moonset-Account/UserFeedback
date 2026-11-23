"""归档库恢复调度器的 unittest 测试。

覆盖:正常路径、边界(容量恰好/不足/空需求)、对象间交错消费、共享记录、
回绕、提前驱逐重读、中途更换暂存上限的故障恢复,以及
"多换一次盘反而比保住共享大记录便宜" 的取舍小例。
"""
import unittest

from tape_recovery import (
    Costs, ObjectSpec, Plan, Problem, Record,
    format_plan, replan, solve, validate_plan, _replay,
)


def make_problem(n_tapes, records, objects, costs):
    return Problem(n_tapes=n_tapes, records=tuple(records),
                   objects=tuple(objects), costs=costs)


class TestBasicPaths(unittest.TestCase):
    def test_single_record(self):
        # 正常路径:一盘一记录一对象。费用 = 换盘 + 前进 + 读记录。
        p = make_problem(
            1, [Record('R1', tape=0, pos=10, length=5)],
            [ObjectSpec('A', ('R1',))],
            Costs(swap=100, forward_per_unit=2, rewind=50, read_per_unit=3))
        plan = solve(p, capacity=5)
        self.assertIsNotNone(plan)
        # 100 (mount) + 10*2 (forward) + 5*3 (read) = 135
        self.assertEqual(plan.total_cost, 135)
        self.assertEqual(plan.peak_occupancy, 5)
        kinds = [s.action for s in plan.steps]
        self.assertEqual(kinds, ['mount', 'read', 'consume', 'release'])
        self.assertEqual(plan.steps[1].head, 15)  # 读完带头在 pos+len
        validate_plan(p, plan, capacity=5)

    def test_empty_demand_is_free(self):
        # 边界:所有对象都不需要任何切片,空计划零费用。
        p = make_problem(1, [Record('R1', 0, 0, 5)],
                         [ObjectSpec('A', ())],
                         Costs(100, 1, 10, 1))
        plan = solve(p, capacity=0)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.total_cost, 0)
        self.assertEqual(plan.steps, ())
        validate_plan(p, plan, capacity=0)

    def test_shared_record_read_once_for_two_objects(self):
        # 同一物理记录给两个对象各供一片:只读一次,两片耗尽后免费释放。
        p = make_problem(
            1, [Record('R1', 0, 0, 10)],
            [ObjectSpec('A', ('R1',)), ObjectSpec('B', ('R1',))],
            Costs(100, 1, 10, 1))
        plan = solve(p, capacity=10)
        self.assertEqual(plan.total_cost, 110)  # 100 mount + 10 read
        reads = [s for s in plan.steps if s.action == 'read']
        self.assertEqual(len(reads), 1)
        consumes = [s for s in plan.steps if s.action == 'consume']
        self.assertEqual(len(consumes), 2)
        # 最后一片消费后占用立即归零
        self.assertEqual(plan.steps[-1].occupancy, 0)
        validate_plan(p, plan, capacity=10)

    def test_capacity_exact_fit_serializes_reads(self):
        # 边界:容量恰好只装得下一条记录,读一条消费释放后再读下一条。
        p = make_problem(
            1, [Record('R1', 0, 0, 10), Record('R2', 0, 20, 10)],
            [ObjectSpec('A', ('R1', 'R2'))],
            Costs(100, 1, 10, 1))
        plan = solve(p, capacity=10)
        # 100 + (0+10) + (10 前进 + 10 读) = 130
        self.assertEqual(plan.total_cost, 130)
        self.assertEqual(plan.peak_occupancy, 10)
        validate_plan(p, plan, capacity=10)

    def test_infeasible_when_record_exceeds_capacity(self):
        # 边界:记录原长超过暂存上限,无解。
        p = make_problem(1, [Record('R1', 0, 0, 10)],
                         [ObjectSpec('A', ('R1',))],
                         Costs(100, 1, 10, 1))
        self.assertIsNone(solve(p, capacity=9))

    def test_invalid_input_rejected(self):
        p = make_problem(1, [Record('R1', 0, 0, 10)],
                         [ObjectSpec('A', ('R9',))],
                         Costs(100, 1, 10, 1))
        with self.assertRaises(ValueError):
            solve(p, capacity=10)


class TestTapeMechanics(unittest.TestCase):
    def test_rewind_when_target_behind_head(self):
        # 读完带尾记录后需读带首附近的记录:必须回绕,费用账目含 rewind。
        p = make_problem(
            1, [Record('R1', 0, 100, 10), Record('R2', 0, 50, 10)],
            [ObjectSpec('A', ('R1', 'R2'))],
            Costs(1000, 1, 50, 1))
        plan = solve(p, capacity=10)  # 一次只能暂存一条
        # 最优:读 R1(前进100+读10=110),消费释放,回绕+前进50+读10=110
        # 合计 1000 + 110 + 110 = 1220
        self.assertEqual(plan.total_cost, 1220)
        # 至少一次读记录发生了回绕(目标 pos 落后于带头)
        head = 0
        saw_rewind = False
        for s in plan.steps:
            if s.action == 'read':
                rec = {r.rid: r for r in p.records}[s.record]
                if rec.pos < head:
                    saw_rewind = True
                head = s.head
        self.assertTrue(saw_rewind)
        validate_plan(p, plan, capacity=10)

    def test_remount_starts_at_head(self):
        # 每次卸载后重装从带首开始:换盘步之后带头必须为 0。
        p = make_problem(
            2,
            [Record('R1', 0, 30, 10), Record('R2', 1, 40, 10)],
            [ObjectSpec('A', ('R1', 'R2'))],
            Costs(100, 1, 10, 1))
        plan = solve(p, capacity=20)
        for s in plan.steps:
            if s.action == 'mount':
                self.assertEqual(s.head, 0)
        validate_plan(p, plan, capacity=20)


class TestInterleaving(unittest.TestCase):
    def test_objects_interleave_consumption(self):
        # 对象间可穿插:A 的两片之间夹入 B 的消费。
        p = make_problem(
            1,
            [Record('R1', 0, 0, 10), Record('R2', 0, 10, 10), Record('R3', 0, 20, 10)],
            [ObjectSpec('A', ('R1', 'R3')), ObjectSpec('B', ('R2',))],
            Costs(100, 1, 10, 1))
        plan = solve(p, capacity=30)
        self.assertEqual(plan.total_cost, 130)  # 100 + 3*10
        consume_objs = [s.obj for s in plan.steps if s.action == 'consume']
        self.assertEqual(consume_objs, ['A', 'B', 'A'])
        # 每条记录只有一片,消费后立即免费释放,峰值即单条记录长度
        self.assertEqual(plan.peak_occupancy, 10)
        validate_plan(p, plan, capacity=30)


class TestEvictionAndSharedRecordTradeoff(unittest.TestCase):
    """多换一次盘反而比保住共享大记录便宜的小例。

    磁带0: R  (pos 0,     len 60) —— 被 X、Y 共享,三片
    磁带1: S1 (pos 5000,  len 40)、S2 (pos 10000, len 40) —— 各两片
    X = [R, S1, S2];  Y = [R, S2, S1, R];  容量 100。

    保住共享大记录 R(占用 60)时,S1/S2 只能逐一暂存,读完 S1 后须驱逐
    才能读 S2,之后为 Y 的 S1 片重读 S1:回绕+长途前进 5000,代价 5140。
    更优选择:消费完 R 的前两片后驱逐 R,一次性暂存 S1+S2(共 80 ≤ 100),
    最后多换一次盘回磁带0 重读 R(位于带首,重读仅 60+换盘 500)。
    """

    def build(self):
        p = make_problem(
            2,
            [Record('R', 0, 0, 60),
             Record('S1', 1, 5000, 40),
             Record('S2', 1, 10000, 40)],
            [ObjectSpec('X', ('R', 'S1', 'S2')),
             ObjectSpec('Y', ('R', 'S2', 'S1', 'R'))],
            Costs(swap=500, forward_per_unit=1, rewind=100, read_per_unit=1))
        return p

    def test_extra_swap_beats_holding_shared_record(self):
        p = self.build()
        plan = solve(p, capacity=100)
        self.assertIsNotNone(plan)
        # 驱逐重读方案:mount A 500 + read R 60 + mount B 500
        #   + read S1 (5000 前进 + 40) + read S2 (4960 前进 + 40)
        #   + mount A 500 + read R 60 = 11660
        self.assertEqual(plan.total_cost, 11660)
        # 保住 R 的最优方案费用为 16240(重读 S1 需回绕+5000 前进),严格更贵
        self.assertLess(plan.total_cost, 16240)
        mounts = [s for s in plan.steps if s.action == 'mount']
        self.assertEqual(len(mounts), 3)  # 多换一次盘
        r_reads = [s for s in plan.steps if s.action == 'read' and s.record == 'R']
        self.assertEqual(len(r_reads), 2)  # 共享大记录被驱逐后重读
        evicts = [s for s in plan.steps if s.action == 'evict']
        self.assertEqual([s.record for s in evicts], ['R'])
        self.assertEqual(plan.peak_occupancy, 80)  # S1+S2 同时暂存
        validate_plan(p, plan, capacity=100)

    def test_forced_early_eviction_and_reread(self):
        # 容量只容一条记录,共享记录的两片被另一记录隔开:必须驱逐并重读。
        p = make_problem(
            1,
            [Record('R1', 0, 0, 10), Record('R2', 0, 20, 10)],
            [ObjectSpec('A', ('R1', 'R2')), ObjectSpec('B', ('R2', 'R1'))],
            Costs(100, 1, 10, 1))
        plan = solve(p, capacity=10)
        # mount 100 + read R1 10 + read R2 (前进10+读10=20)
        #   + 重读 R1 (回绕10+读10=20) = 150
        self.assertEqual(plan.total_cost, 150)
        r1_reads = [s for s in plan.steps if s.action == 'read' and s.record == 'R1']
        self.assertEqual(len(r1_reads, ), 2)
        self.assertEqual(plan.peak_occupancy, 10)
        validate_plan(p, plan, capacity=10)


class TestMidExecutionCapacityChange(unittest.TestCase):
    """故障恢复:执行到一半更换暂存上限,保留已消费前缀,按新上限重求余程。"""

    def test_replan_with_tighter_capacity(self):
        p = TestEvictionAndSharedRecordTradeoff().build()
        plan = solve(p, capacity=100)
        self.assertEqual(plan.total_cost, 11660)

        cut = 6  # 已执行:mount A, read R, consume X#1, consume Y#1, evict R, mount B
        prefix = plan.steps[:cut]
        state = _replay(p, prefix)
        self.assertEqual(state.consumed, (1, 1))   # 已消费前缀保留
        self.assertEqual(state.mounted, 1)

        new_plan = replan(p, plan, executed_steps=cut, new_capacity=60)
        self.assertIsNotNone(new_plan)
        # 前缀原样保留
        self.assertEqual(new_plan.steps[:cut], prefix)
        # 余程满足新上限(峰值 60:末尾重读 R 时占满)
        suffix = new_plan.steps[cut:]
        self.assertLessEqual(max(s.occupancy for s in suffix), 60)
        # 余程费用:read S1 5040 + evict S1 + read S2 5000
        #   + 重读 S1 (回绕100+前进5000+读40=5140) + mount A 500 + read R 60
        # 前缀 1060 + 余程 15740 = 16800,严于原上限故更贵
        self.assertEqual(new_plan.total_cost, 16800)
        # 前缀按旧上限、余程按新上限分别校验;整体消费完整
        validate_plan(p, prefix, capacity=100, require_complete=False)
        validate_plan(p, suffix, capacity=60, start=state)
        validate_plan(p, new_plan, capacity=100)  # 新上限更严,旧上限下必成立

    def test_replan_infeasible_when_record_too_big_for_new_capacity(self):
        p = TestEvictionAndSharedRecordTradeoff().build()
        plan = solve(p, capacity=100)
        # R 原长 60,新上限 50 时余程不可能完成
        self.assertIsNone(replan(p, plan, executed_steps=6, new_capacity=50))

    def test_replan_from_scratch_equivalent_to_solve(self):
        p = TestEvictionAndSharedRecordTradeoff().build()
        plan = solve(p, capacity=100)
        fresh = replan(p, plan, executed_steps=0, new_capacity=100)
        self.assertEqual(fresh.total_cost, plan.total_cost)


class TestReporting(unittest.TestCase):
    def test_format_plan_reports_head_consumed_and_peak(self):
        p = TestEvictionAndSharedRecordTradeoff().build()
        plan = solve(p, capacity=100)
        text = format_plan(p, plan)
        self.assertIn('total_cost=11660', text)
        self.assertIn('peak_occupancy=80', text)
        self.assertIn('head', text)
        # 逐动作已消费片计数单调不减
        last = (0, 0)
        for s in plan.steps:
            self.assertGreaterEqual(s.consumed, last)
            last = s.consumed
        self.assertEqual(plan.steps[-1].consumed, (3, 4))


if __name__ == '__main__':
    unittest.main()
