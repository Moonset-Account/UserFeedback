"""三个连续时间步的端到端核对。

场景设计（宽度 3，家槽 0/1/2，堆槽 3..6，临时槽 7，block_size=8）：

* 时间步 1 —— 重排环：三个父束各自只有一个存活孩子，但排名顺序与父束
  家槽顺序发生旋转，独占继承的末页必须通过“环 + 唯一临时槽”完成重排。
* 时间步 2 —— 同父分叉 + 早结束：同一个父束的末页被多个孩子共享，
  其中两个是存活孩子、一个是结束孩子；结束孩子进入结束假设并占用堆槽，
  存活名额由后续候选递补。
* 时间步 3 —— 在分叉后的新末页上继续追加，并再次制造重排环。

每个候选都核对：稳定父链 token 回溯、逻辑页链、物理槽位与页内容。
"""

import unittest

from beam_manager import (
    AppendPage,
    BeamManager,
    Candidate,
    CapacityError,
    CopyPage,
    DropPage,
    MovePage,
)


def op_types(plan):
    return [type(op).__name__ for op in plan.ops]


class ThreeStepScenarioTest(unittest.TestCase):
    def setUp(self):
        # 3 家槽 + 6 堆槽 + 1 临时槽（槽 9）
        self.m = BeamManager(width=3, heap_slots=6, block_size=8)

    # ---------------------------------------------------------------- #
    # 时间步 0：根束三分叉，建立 A/B/C 三个存活束
    # ---------------------------------------------------------------- #
    def test_step0_root_fork(self):
        plan = self.m.step(
            [
                Candidate(0, token=10, score=1.0),
                Candidate(0, token=20, score=0.9),
                Candidate(0, token=30, score=0.8),
                Candidate(0, token=40, score=0.7),  # 被淘汰
            ]
        )
        self.assertEqual([b.token for b in plan.active_beams], [10, 20, 30])
        self.assertEqual(len(plan.finished_beams), 0)
        # 根页被写时复制三次，根页本身搬到堆槽 3 作为共享只读历史
        self.assertEqual(self.m.slot_of(1), 3)
        for pid, token in ((2, 10), (3, 20), (4, 30)):
            self.assertEqual(self.m.slot_of(pid), pid - 2)
            self.assertEqual(self.m.page_tokens(pid), (token,))
        # 淘汰候选不产生任何页
        self.assertEqual(set(self.m.memory.pages), {1, 2, 3, 4})
        # 临时槽始终为空
        self.assertTrue(self.m.memory.temp_is_free())

    # ---------------------------------------------------------------- #
    # 时间步 1：独占继承 + 重排环（家槽旋转 0->2->1->0）
    # ---------------------------------------------------------------- #
    def test_step1_rearrangement_ring(self):
        self.test_step0_root_fork()

        # 父束稳定编号：beam 1 -> 家槽0 (token10), 2 -> 家槽1 (token20),
        #               beam 3 -> 家槽2 (token30)
        # 目标排名顺序（每个父仅一个存活孩子 => 独占继承，末页 MOVE）：
        #   home0 = beam3 的孩子（页4）, home1 = beam1 的孩子（页2）,
        #   home2 = beam2 的孩子（页3）
        plan = self.m.step(
            [
                Candidate(3, token=31, score=3.0),   # 排名第1 -> home0
                Candidate(1, token=11, score=2.0),   # 排名第2 -> home1
                Candidate(2, token=21, score=1.0),   # 排名第3 -> home2
            ]
        )

        moves = [op for op in plan.ops if isinstance(op, MovePage)]
        # 家槽重排环：页4: 2->0, 页2: 0->1, 页3: 1->2。
        # 环本身恰好 4 个 MOVE（环首入临时槽 + 两次顺移 + 接回）；
        # 另有共享根历史页在堆槽间的迁移，不属于该环。
        home_moves = [
            op for op in moves
            if op.dst < 3 or op.src < 3 or op.dst == self.m.memory.temp_slot
        ]
        ring_moves = [
            op for op in home_moves if op.pid in (2, 3, 4)
        ]
        self.assertEqual(len(ring_moves), 4)
        temp_uses = [op for op in ring_moves
                     if op.dst == self.m.memory.temp_slot]
        self.assertEqual(len(temp_uses), 1)
        # 环首是 pid 最小的页 2（slot 0 -> temp 7）
        self.assertEqual((temp_uses[0].pid, temp_uses[0].src), (2, 0))

        # 执行后的物理布局
        self.assertEqual(self.m.slot_of(4), 0)
        self.assertEqual(self.m.slot_of(2), 1)
        self.assertEqual(self.m.slot_of(3), 2)
        self.assertTrue(self.m.memory.temp_is_free())

        # 独占继承：逻辑页链不变，仅在原末页上追加
        self.assertEqual(self.m.page_tokens(4), (30, 31))
        self.assertEqual(self.m.page_tokens(2), (10, 11))
        self.assertEqual(self.m.page_tokens(3), (20, 21))

        # 新束的 token 回溯
        beams = plan.active_beams
        self.assertEqual([b.home_slot for b in beams], [0, 1, 2])
        traces = {b.token: self.m.trace(b.beam_id) for b in beams}
        self.assertEqual(traces[31], (30, 31))
        self.assertEqual(traces[11], (10, 11))
        self.assertEqual(traces[21], (20, 21))

    # ---------------------------------------------------------------- #
    # 时间步 2：同父分叉（写时复制）+ 早结束（结束假设单独维护+递补）
    # ---------------------------------------------------------------- #
    def test_step2_same_parent_fork_and_early_end(self):
        self.test_step1_rearrangement_ring()
        # 当前存活：home0 beam(tokens 30,31, pid4),
        #           home1 beam(10,11, pid2), home2 beam(20,21, pid3)
        beam_ids = self.m.active_beam_ids
        b_home0, b_home1, b_home2 = beam_ids

        plan = self.m.step(
            [
                # home0 的父束分叉出三个孩子：两个存活 + 一个结束
                Candidate(b_home0, token=310, score=9.0),
                Candidate(b_home0, token=311, score=8.0),
                Candidate(b_home0, token=999, score=7.0, ended=True),
                # 一个排名靠后的非结束候选，用于把结束名额递补为存活束
                Candidate(b_home1, token=110, score=6.0),
                # 排在前 3 名之外的结束候选必须被丢弃
                Candidate(b_home2, token=888, score=5.0, ended=True),
            ]
        )

        # 排名：9.0, 8.0, 7.0(end), 6.0, 5.0(end)
        # 前 3 名中的结束候选 999 进入结束假设；6.0 递补为第 3 个存活束
        self.assertEqual(len(plan.finished_beams), 1)
        self.assertEqual(plan.finished_beams[0].token, 999)
        self.assertEqual(
            [b.token for b in plan.active_beams], [310, 311, 110]
        )
        self.assertEqual(len(plan.active_beams), 3)  # 固定宽度被递补补齐

        # 父末页 pid4 被三个孩子引用 -> 升级为共享只读页，搬到堆槽
        self.assertGreaterEqual(self.m.slot_of(4), 3)
        self.assertEqual(self.m.page_tokens(4), (30, 31))  # 内容不被修改

        # 两个存活孩子 + 结束孩子各自持有独立可写末页（COW）
        fork_beams = {b.token: b for b in plan.active_beams}
        end_beam = plan.finished_beams[0]
        for beam, token in (
            (fork_beams[310], 310),
            (fork_beams[311], 311),
            (end_beam, 999),
        ):
            tail = beam.page_ids[-1]
            self.assertNotEqual(tail, 4)             # 不是共享的父页
            self.assertEqual(self.m.page_tokens(tail), (30, 31, token))
        # 结束束末页在堆槽，不占家槽
        self.assertGreaterEqual(self.m.slot_of(end_beam.page_ids[-1]), 3)
        self.assertIsNone(end_beam.home_slot)

        # 存活孩子的末页在家槽
        self.assertEqual(
            self.m.slot_of(fork_beams[310].page_ids[-1]), 0
        )
        self.assertEqual(
            self.m.slot_of(fork_beams[311].page_ids[-1]), 1
        )

        # 递补上来的 home1 孩子独占继承 pid2
        filler = fork_beams[110]
        self.assertEqual(filler.page_ids[-1], 2)
        self.assertEqual(self.m.slot_of(2), 2)
        self.assertEqual(self.m.page_tokens(2), (10, 11, 110))

        # 被丢弃的结束候选不产生页
        pids = set(self.m.memory.pages)
        self.assertNotIn(888, [t for pid in pids for t in self.m.page_tokens(pid)])

        # 回溯核对全部候选
        self.assertEqual(
            self.m.trace(fork_beams[310].beam_id), (30, 31, 310)
        )
        self.assertEqual(
            self.m.trace(end_beam.beam_id), (30, 31, 999)
        )
        self.assertEqual(
            self.m.trace(filler.beam_id), (10, 11, 110)
        )
        self.assertTrue(self.m.memory.temp_is_free())

    # ---------------------------------------------------------------- #
    # 时间步 3：分叉后继续追加，并再次制造重排环
    # ---------------------------------------------------------------- #
    def test_step3_second_ring_after_fork(self):
        self.test_step2_same_parent_fork_and_early_end()
        active = {b.token: b for b in self.m.active_beams}
        b310, b311, b110 = active[310], active[311], active[110]

        plan = self.m.step(
            [
                Candidate(b110.beam_id, token=1110, score=5.0),  # -> home0
                Candidate(b310.beam_id, token=3100, score=4.0),  # -> home1
                Candidate(b311.beam_id, token=3110, score=3.0),  # -> home2
            ]
        )

        moves = [op for op in plan.ops if isinstance(op, MovePage)]
        # 末页当前在 home 2,0,1，目标 home 0,1,2 -> 又一个三环
        temp_uses = [op for op in moves if op.dst == self.m.memory.temp_slot]
        self.assertEqual(len(temp_uses), 1)
        self.assertTrue(self.m.memory.temp_is_free())

        # 物理槽与内容
        for beam, home in (
            (plan.active_beams[0], 0),
            (plan.active_beams[1], 1),
            (plan.active_beams[2], 2),
        ):
            tail = beam.page_ids[-1]
            self.assertEqual(self.m.slot_of(tail), home)

        self.assertEqual(
            self.m.page_tokens(plan.active_beams[0].page_ids[-1]),
            (10, 11, 110, 1110),
        )
        self.assertEqual(
            self.m.page_tokens(plan.active_beams[1].page_ids[-1]),
            (30, 31, 310, 3100),
        )
        self.assertEqual(
            self.m.page_tokens(plan.active_beams[2].page_ids[-1]),
            (30, 31, 311, 3110),
        )

        # token 回溯
        self.assertEqual(
            self.m.trace(plan.active_beams[0].beam_id),
            (10, 11, 110, 1110),
        )
        self.assertEqual(
            self.m.trace(plan.active_beams[1].beam_id),
            (30, 31, 310, 3100),
        )

        # 结束假设仍然单独保留、内容冻结
        self.assertEqual(len(self.m.finished), 1)
        end = self.m.finished[0]
        self.assertEqual(self.m.trace(end.beam_id), (30, 31, 999))
        self.assertEqual(
            self.m.page_tokens(end.page_ids[-1]), (30, 31, 999)
        )


if __name__ == "__main__":
    unittest.main()
