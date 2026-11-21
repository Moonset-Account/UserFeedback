"""交错场景：一个 rank 等待时其他 rank 继续前进、解除阻塞、同成员名单独立配对。"""

import unittest

from collectives import (
    DeadlockError,
    Executor,
    Group,
    Program,
    RankBlocked,
    RankUnblocked,
    SlotCompleted,
)


class InterleavingTest(unittest.TestCase):
    def test_waiting_rank_blocks_while_other_ranks_progress(self) -> None:
        group = Group("g", [0, 1])
        # rank 1 的单成员组用来延迟它在 g 组的发起，迫使 rank 0 先阻塞。
        solo = Group("solo1", [1])
        p0 = Program(0)
        h0 = p0.initiate("g", "t")
        p0.wait(h0)  # rank 0 先阻塞，直到 rank 1 发起同位置操作
        p1 = Program(1)
        p1.initiate("solo1", "warmup")  # 与 rank 0 无关，rank 1 仍可前进
        h1 = p1.initiate("g", "t")
        p1.wait(h1)

        result = Executor([group, solo], {0: p0, 1: p1}).run()
        event_types = [type(event) for event in result.events]

        self.assertIn(RankBlocked, event_types)
        blocked = next(event for event in result.events
                       if isinstance(event, RankBlocked))
        self.assertEqual((blocked.rank, blocked.group, blocked.ordinal),
                         (0, "g", 1))
        # 阻塞之后由另一个 rank 的发起触发收齐与解除阻塞。
        block_index = event_types.index(RankBlocked)
        later = result.events[block_index + 1:]
        self.assertTrue(any(isinstance(event, SlotCompleted) for event in later))
        unblocked = next(event for event in later if isinstance(event, RankUnblocked))
        self.assertEqual(unblocked.rank, 0)

    def test_unblocked_rank_resumes_and_finishes(self) -> None:
        group = Group("g", [0, 1])
        solo = Group("solo1", [1])
        p0 = Program(0)
        first = p0.initiate("g", "t1")
        p0.wait(first)
        second = p0.initiate("g", "t2")
        p0.wait(second)
        # rank 1 在每次 g 组发起前插入单成员组操作，制造两次阻塞窗口。
        p1 = Program(1)
        p1.initiate("solo1", "s1")
        a1 = p1.initiate("g", "t1")
        p1.wait(a1)
        p1.initiate("solo1", "s2")
        b1 = p1.initiate("g", "t2")
        p1.wait(b1)

        result = Executor([group, solo], {0: p0, 1: p1}).run()

        ordinals = [record.ordinal for record in result.ops_by_rank[0]]
        self.assertEqual(ordinals, [1, 2])
        self.assertEqual(
            [event.ordinal for event in result.events
             if isinstance(event, RankBlocked) and event.rank == 0],
            [1, 2],
        )
        self.assertEqual(
            [event.ordinal for event in result.events
             if isinstance(event, RankUnblocked) and event.rank == 0],
            [1, 2],
        )

    def test_multiple_outstanding_ops_before_waiting(self) -> None:
        group = Group("g", [0, 1])
        p0 = Program(0)
        a = p0.initiate("g", "t1")
        b = p0.initiate("g", "t2")
        p0.wait(a)
        p0.wait(b)
        p1 = Program(1)
        c = p1.initiate("g", "t1")
        d = p1.initiate("g", "t2")
        p1.wait(c)
        p1.wait(d)

        result = Executor([group], {0: p0, 1: p1}).run()

        self.assertEqual(result.op_by_handle(a).ordinal, 1)
        self.assertEqual(result.op_by_handle(b).ordinal, 2)
        # 两个位置都在 rank 1 发起后收齐，rank 0 的等待不再阻塞。
        self.assertFalse(
            [event for event in result.events
             if isinstance(event, RankBlocked) and event.rank == 0]
        )

    def test_groups_with_same_members_keep_independent_ordinals_success(self) -> None:
        ga = Group("A", [0, 1])
        gb = Group("B", [0, 1])
        p0 = Program(0)
        ha = p0.initiate("A", "tag-a")
        hb = p0.initiate("B", "tag-b")
        p0.wait(ha)
        p0.wait(hb)
        p1 = Program(1)
        # 故意以相反顺序发起：B 组的配对不受 A 组次序影响。
        hb1 = p1.initiate("B", "tag-b")
        ha1 = p1.initiate("A", "tag-a")
        p1.wait(hb1)
        p1.wait(ha1)

        result = Executor([ga, gb], {0: p0, 1: p1}).run()

        self.assertEqual(result.op_by_handle(ha).group, "A")
        self.assertEqual(result.op_by_handle(ha).ordinal, 1)
        self.assertEqual(result.op_by_handle(hb).group, "B")
        self.assertEqual(result.op_by_handle(hb).ordinal, 1)
        self.assertEqual(result.op_by_handle(ha1).ordinal, 1)
        self.assertEqual(result.op_by_handle(hb1).ordinal, 1)

    def test_groups_with_same_members_keep_independent_ordinals_deadlock(self) -> None:
        ga = Group("A", [0, 1])
        gb = Group("B", [0, 1])
        p0 = Program(0)
        h0 = p0.initiate("A", "x")
        p0.wait(h0)  # 等 A 组第 1 次，缺 rank 1 的 A 组发起
        p1 = Program(1)
        h1 = p1.initiate("B", "x")
        p1.wait(h1)  # 等 B 组第 1 次，缺 rank 0 的 B 组发起

        with self.assertRaises(DeadlockError) as context:
            Executor([ga, gb], {0: p0, 1: p1}).run()

        chain = context.exception.chain
        self.assertEqual(len(chain), 2)
        by_waiter = {edge.waiter: edge for edge in chain}
        self.assertEqual(
            (by_waiter[0].group, by_waiter[0].ordinal, by_waiter[0].missing_rank),
            ("A", 1, 1),
        )
        self.assertEqual(
            (by_waiter[1].group, by_waiter[1].ordinal, by_waiter[1].missing_rank),
            ("B", 1, 0),
        )
        # 闭合链：每条边指向的缺失成员正是下一条边的等待者。
        for index, edge in enumerate(chain):
            self.assertEqual(
                edge.missing_rank, chain[(index + 1) % len(chain)].waiter
            )


if __name__ == "__main__":
    unittest.main()
