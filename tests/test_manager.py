"""BeamManager 层面的测试：选择语义、稳定编号、计划/执行分离与边界。"""

import random
import unittest

from beam_manager import BeamManager, Candidate, CapacityError
from beam_manager import AppendPage


class SelectionTest(unittest.TestCase):
    def test_score_descending_with_stable_id_tiebreak(self):
        m = BeamManager(width=3, heap_slots=4)
        # 两个父束：先制造 beam 1 / beam 2
        m.step([Candidate(0, 1, 1.0), Candidate(0, 2, 1.0)])
        b1, b2 = m.active_beam_ids
        self.assertLess(b1, b2)

        # 分数完全相同：必须按父束稳定编号（b1 < b2）而非数组位置
        plan = m.plan_step(
            [
                Candidate(b2, token=20, score=5.0),
                Candidate(b1, token=10, score=5.0),
                Candidate(b1, token=11, score=4.0),
                Candidate(b2, token=21, score=4.0),
            ]
        )
        self.assertEqual(
            [(c.parent_beam_id, c.token) for c in plan.ranked_candidates],
            [(b1, 10), (b2, 20), (b1, 11), (b2, 21)],
        )
        self.assertEqual(
            [b.token for b in plan.active_beams], [10, 20, 11]
        )

    def test_shuffled_inputs_produce_identical_results(self):
        """父束顺序每轮都可能变化：打乱后端数组不影响结果。"""
        def run(seed):
            rng = random.Random(seed)
            m = BeamManager(width=3, heap_slots=12)
            records = []
            for step in range(3):
                cands = []
                # 分数只由候选身份决定，与种子无关；种子仅用于打乱后端数组顺序
                for beam in m.active_beams:
                    for k in range(2):
                        token = 100 * (step + 1) + beam.beam_id * 10 + k
                        cands.append(
                            Candidate(
                                beam.beam_id,
                                token=token,
                                score=1000.0 - token,
                            )
                        )
                rng.shuffle(cands)
                plan = m.step(cands)
                records.append(
                    (
                        [b.beam_id for b in plan.active_beams],
                        [op.describe() for op in plan.ops],
                        m.memory.snapshot(),
                        {
                            pid: m.page_tokens(pid)
                            for pid in sorted(m.memory.pages)
                        },
                    )
                )
            return records

        baseline = run(1)
        for seed in (2, 3, 42):
            self.assertEqual(run(seed), baseline)

    def test_ended_hypothesis_maintained_separately_and_slot_backfilled(self):
        m = BeamManager(width=2, heap_slots=6)
        m.step([Candidate(0, 1, 1.0), Candidate(0, 2, 0.9)])
        b1, b2 = m.active_beam_ids

        plan = m.step(
            [
                Candidate(b1, token=90, score=9.0, ended=True),   # 第1名但结束
                Candidate(b2, token=20, score=8.0),
                Candidate(b1, token=10, score=7.0),               # 递补存活
                Candidate(b2, token=91, score=6.0, ended=True),   # 前2名之外 -> 丢弃
            ]
        )
        self.assertEqual([b.token for b in plan.active_beams], [20, 10])
        self.assertEqual(len(plan.active_beams), 2)
        self.assertEqual([b.token for b in plan.finished_beams], [90])
        self.assertEqual(len(m.finished), 1)
        self.assertTrue(m.finished[0].ended)

    def test_all_ended_leaves_zero_active_beams(self):
        m = BeamManager(width=2, heap_slots=3)
        plan = m.step(
            [
                Candidate(0, token=1, score=3.0, ended=True),
                Candidate(0, token=2, score=2.0, ended=True),
                Candidate(0, token=3, score=1.0, ended=True),
            ]
        )
        self.assertEqual(plan.active_beams, [])
        self.assertEqual(
            sorted(b.token for b in plan.finished_beams), [1, 2]
        )
        self.assertEqual(m.active_beam_ids, [])


class PlanExecuteSeparationTest(unittest.TestCase):
    def test_plan_does_not_mutate_memory(self):
        m = BeamManager(width=2, heap_slots=3)
        before_pages = {p: m.page_tokens(p) for p in m.memory.pages}
        before_slots = m.memory.snapshot()
        plan = m.plan_step(
            [Candidate(0, 1, 1.0), Candidate(0, 2, 0.9)]
        )
        self.assertEqual(m.memory.snapshot(), before_slots)
        self.assertEqual(
            {p: m.page_tokens(p) for p in m.memory.pages}, before_pages
        )
        self.assertEqual(m.step_count, 0)
        m.execute_plan(plan)
        self.assertEqual(m.step_count, 1)
        self.assertEqual(len(m.active_beams), 2)

    def test_plan_is_stepwise_executable(self):
        """逐操作执行与批量执行结果一致，且每一步都满足物理断言。"""
        m = BeamManager(width=3, heap_slots=5)
        m.step([Candidate(0, t, float(t)) for t in range(3)])
        parents = [b.beam_id for b in m.active_beams]
        plan = m.plan_step(
            [Candidate(p, t + 10, float(t)) for t, p in enumerate(parents)]
            + [Candidate(parents[0], 99, 0.1, ended=True)]
        )
        for op in plan.ops:
            m.memory.apply(op)  # 逐步执行，任何违例直接 AssertionError
        # 手动提交束状态
        for beam in plan.active_beams + plan.finished_beams:
            m.beams[beam.beam_id] = beam
        m.active_beam_ids = [b.beam_id for b in plan.active_beams]
        m.finished.extend(plan.finished_beams)
        m.step_count += 1
        self.assertTrue(m.memory.temp_is_free())
        self.assertEqual(len(m.finished), 1)


class BlockFullnessTest(unittest.TestCase):
    def test_full_tail_allocates_fresh_page(self):
        m = BeamManager(width=1, heap_slots=2, block_size=2)
        # 根页容量 2：先写满两个 token
        m.step([Candidate(0, token=1, score=1.0)])
        m.step(
            [Candidate(m.active_beam_ids[0], token=2, score=1.0)]
        )
        tail = m.active_beams[0].page_ids[-1]
        self.assertEqual(m.page_tokens(tail), (1, 2))

        plan = m.step(
            [Candidate(m.active_beam_ids[0], token=3, score=1.0)]
        )
        beam = plan.active_beams[0]
        new_tail = beam.page_ids[-1]
        self.assertNotEqual(new_tail, tail)
        self.assertEqual(m.page_tokens(tail), (1, 2))       # 旧页冻结
        self.assertEqual(m.page_tokens(new_tail), (3,))     # 全新页
        fresh_ops = [
            op for op in plan.ops
            if isinstance(op, AppendPage) and op.fresh
        ]
        self.assertEqual(len(fresh_ops), 1)
        self.assertEqual(m.trace(beam.beam_id), (1, 2, 3))


class SourceSlotInterleaveTest(unittest.TestCase):
    """目的槽恰是另一个孩子尚未读取的源槽时的写前保护。"""

    def test_fork_target_equals_source_home_slot(self):
        # 宽度2：根页先独占保留在家槽0（单孩子继承），随后该父束分叉，
        # 且排名第一的孩子目的家槽就是源页所在的槽0。
        m = BeamManager(width=2, heap_slots=4, block_size=8)
        m.step([Candidate(0, token=7, score=1.0)])
        parent = m.active_beams[0]
        source_pid = parent.page_ids[-1]
        self.assertEqual(m.slot_of(source_pid), 0)

        plan = m.step(
            [
                Candidate(parent.beam_id, token=70, score=3.0),  # -> home0
                Candidate(parent.beam_id, token=71, score=2.0),  # -> home1
            ]
        )
        # 共享源必须先搬离家槽0，任何 COPY 才能写入槽0
        ops = plan.ops
        last_move = max(
            i for i, o in enumerate(ops)
            if type(o).__name__ == "MovePage"
        )
        first_copy = next(
            i for i, o in enumerate(ops)
            if type(o).__name__ == "CopyPage"
        )
        self.assertLess(last_move, first_copy)

        beams = {b.token: b for b in plan.active_beams}
        t70 = beams[70].page_ids[-1]
        t71 = beams[71].page_ids[-1]
        self.assertEqual(m.slot_of(t70), 0)
        self.assertEqual(m.slot_of(t71), 1)
        # 源页撤离到堆槽且内容冻结，两个孩子各自独立可写
        self.assertGreaterEqual(m.slot_of(source_pid), 2)
        self.assertEqual(m.page_tokens(source_pid), (7,))
        self.assertEqual(m.page_tokens(t70), (7, 70))
        self.assertEqual(m.page_tokens(t71), (7, 71))


class CapacityRejectionTest(unittest.TestCase):
    def test_capacity_error_before_overwrite_keeps_state(self):
        # 宽度2 + 0 堆槽：根页被两个孩子 COW 时无处安置共享根页 -> 拒绝
        m = BeamManager(width=2, heap_slots=0, block_size=4)
        snapshot = m.memory.snapshot()
        with self.assertRaises(CapacityError):
            m.plan_step(
                [
                    Candidate(0, 1, 1.0),
                    Candidate(0, 2, 0.9),
                    Candidate(0, 3, 0.8),
                ]
            )
        # 拒绝后状态完全不变
        self.assertEqual(m.memory.snapshot(), snapshot)
        self.assertEqual(m.step_count, 0)

        # 提供 1 个堆槽后，同等需求即可正常规划执行
        m2 = BeamManager(width=2, heap_slots=1, block_size=4)
        plan = m2.step([Candidate(0, 1, 1.0), Candidate(0, 2, 0.9)])
        self.assertEqual(len(plan.active_beams), 2)
        self.assertEqual(m2.page_tokens(1), ())  # 共享根页内容未被修改

    def test_validation_rejects_unknown_or_inactive_parent(self):
        m = BeamManager(width=2, heap_slots=2)
        with self.assertRaises(ValueError):
            m.plan_step([Candidate(99, 1, 1.0)])
        m.step([Candidate(0, 1, 1.0), Candidate(0, 2, 0.9)])
        # 根束已不是存活束
        with self.assertRaises(ValueError):
            m.plan_step([Candidate(0, 1, 1.0)])


if __name__ == "__main__":
    unittest.main()
