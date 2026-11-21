"""正常场景：配对收齐、流水发起、单成员组、事件与结果记录。"""

import unittest

from collectives import (
    Executor,
    Group,
    Initiated,
    Program,
    RankBlocked,
    RankEnded,
    SlotCompleted,
)


class NormalExecutionTest(unittest.TestCase):
    def test_two_ranks_one_op_each_completes(self) -> None:
        group = Group("all", [0, 1])
        p0 = Program(0)
        h0 = p0.initiate("all", "step-1")
        p0.wait(h0)
        p1 = Program(1)
        h1 = p1.initiate("all", "step-1")
        p1.wait(h1)

        result = Executor([group], {0: p0, 1: p1}).run()

        record0 = result.op_by_handle(h0)
        self.assertEqual(record0.rank, 0)
        self.assertEqual(record0.group, "all")
        self.assertEqual(record0.ordinal, 1)
        self.assertEqual(record0.tag, "step-1")
        self.assertEqual(record0.members, (0, 1))
        record1 = result.op_by_handle(h1)
        self.assertEqual((record1.rank, record1.ordinal), (1, 1))
        self.assertEqual(len(result.ops_by_rank[0]), 1)
        self.assertEqual(len(result.ops_by_rank[1]), 1)

        # 双方在等待前就已收齐，因此不应出现任何阻塞事件。
        self.assertNotIn(RankBlocked, [type(event) for event in result.events])
        self.assertEqual(
            [event for event in result.events if isinstance(event, RankEnded)],
            [RankEnded(rank=0), RankEnded(rank=1)],
        )

    def test_initiates_are_non_blocking_and_pair_by_ordinal(self) -> None:
        group = Group("g", [0, 1])
        p0 = Program(0)
        a0 = p0.initiate("g", "t1")
        b0 = p0.initiate("g", "t2")
        p1 = Program(1)
        a1 = p1.initiate("g", "t1")
        b1 = p1.initiate("g", "t2")

        result = Executor([group], {0: p0, 1: p1}).run()

        self.assertEqual(result.op_by_handle(a0).ordinal, 1)
        self.assertEqual(result.op_by_handle(a1).ordinal, 1)
        self.assertEqual(result.op_by_handle(b0).ordinal, 2)
        self.assertEqual(result.op_by_handle(b1).ordinal, 2)
        completed = [event for event in result.events if isinstance(event, SlotCompleted)]
        self.assertEqual([(event.group, event.ordinal) for event in completed],
                         [("g", 1), ("g", 2)])

    def test_single_member_group_completes_on_submit(self) -> None:
        group = Group("solo", [7])
        program = Program(7)
        handle = program.initiate("solo", "lonely")
        program.wait(handle)

        result = Executor([group], {7: program}).run()

        record = result.op_by_handle(handle)
        self.assertEqual((record.group, record.ordinal, record.members),
                         ("solo", 1, (7,)))
        self.assertNotIn(RankBlocked, [type(event) for event in result.events])

    def test_member_order_does_not_affect_pairing(self) -> None:
        # 声明顺序为 2,0,1，记录与事件中的成员名单仍按 rank 稳定排序。
        group = Group("g", [2, 0, 1])
        programs = {}
        handles = {}
        for rank in (0, 1, 2):
            program = Program(rank)
            handles[rank] = program.initiate("g", "same")
            program.wait(handles[rank])
            programs[rank] = program

        result = Executor([group], programs).run()

        for rank in (0, 1, 2):
            self.assertEqual(result.op_by_handle(handles[rank]).members, (0, 1, 2))
        completed = [event for event in result.events if isinstance(event, SlotCompleted)]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].members, (0, 1, 2))

    def test_event_sequence_reflects_submission_then_completion(self) -> None:
        group = Group("g", [0, 1])
        p0 = Program(0)
        p0.initiate("g", "t")
        p1 = Program(1)
        p1.initiate("g", "t")

        result = Executor([group], {0: p0, 1: p1}).run()
        events = result.events

        self.assertIsInstance(events[0], Initiated)
        self.assertEqual((events[0].rank, events[0].ordinal), (0, 1))
        self.assertIsInstance(events[1], Initiated)
        self.assertEqual((events[1].rank, events[1].ordinal), (1, 1))
        self.assertIsInstance(events[2], SlotCompleted)
        self.assertEqual((events[2].group, events[2].ordinal), ("g", 1))

    def test_two_executor_instances_have_independent_pairing_state(self) -> None:
        group = Group("g", [0, 1])

        def build():
            p0 = Program(0)
            h0 = p0.initiate("g", "t")
            p0.wait(h0)
            p1 = Program(1)
            h1 = p1.initiate("g", "t")
            p1.wait(h1)
            return {0: p0, 1: p1}, (h0, h1)

        first_programs, _ = build()
        first = Executor([group], first_programs).run()
        second_programs, second_handles = build()
        second = Executor([group], second_programs).run()

        # 两个执行器实例的配对次序互不影响，各自都从第 1 次发起开始。
        self.assertEqual(next(iter(first.ops_by_rank[0])).ordinal, 1)
        self.assertEqual(second.op_by_handle(second_handles[0]).ordinal, 1)
        self.assertEqual(second.op_by_handle(second_handles[1]).members, (0, 1))


if __name__ == "__main__":
    unittest.main()
