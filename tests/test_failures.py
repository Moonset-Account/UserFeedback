"""失败场景：标签不一致、成员结束却缺席、闭合等待链死锁。"""

import unittest

from collectives import (
    DeadlockError,
    Executor,
    Group,
    MemberExitedError,
    Program,
    TagMismatchError,
)


class TagMismatchTest(unittest.TestCase):
    def test_different_tags_at_same_slot_raise_immediately(self) -> None:
        group = Group("g", [0, 1])
        p0 = Program(0)
        p0.initiate("g", "alpha")
        p1 = Program(1)
        p1.initiate("g", "beta")

        with self.assertRaises(TagMismatchError) as context:
            Executor([group], {0: p0, 1: p1}).run()

        error = context.exception
        self.assertEqual(error.group, "g")
        self.assertEqual(error.ordinal, 1)
        self.assertEqual(error.rank, 1)
        self.assertEqual(error.tag, "beta")
        self.assertEqual(error.existing_tag, "alpha")
        self.assertEqual(error.members, (0, 1))
        self.assertIn("标签不一致", str(error))

    def test_mismatch_detected_eagerly_even_when_deadlock_would_follow(self) -> None:
        # rank 0 与 rank 1 在 A 组标签冲突；若不立即报错，后续会停滞。
        ga = Group("A", [0, 1])
        gb = Group("B", [0, 1])
        p0 = Program(0)
        h0 = p0.initiate("A", "left")
        p0.wait(h0)
        p1 = Program(1)
        h1 = p1.initiate("A", "right")
        p1.wait(h1)
        # 额外的 B 组指令永远不会执行到，仅用于构造潜在环。
        p0.initiate("B", "b")
        p1.initiate("B", "b")

        with self.assertRaises(TagMismatchError):
            Executor([ga, gb], {0: p0, 1: p1}).run()

    def test_same_tag_at_second_ordinal_pairs_normally(self) -> None:
        group = Group("g", [0, 1])
        p0 = Program(0)
        p0.initiate("g", "t1")
        h02 = p0.initiate("g", "t2")
        p1 = Program(1)
        p1.initiate("g", "t1")
        h12 = p1.initiate("g", "t2")

        result = Executor([group], {0: p0, 1: p1}).run()
        self.assertEqual(result.op_by_handle(h02).ordinal, 2)
        self.assertEqual(result.op_by_handle(h12).ordinal, 2)


class MemberExitedTest(unittest.TestCase):
    def test_member_ends_without_submitting_waited_op(self) -> None:
        group = Group("g", [0, 1])
        p0 = Program(0)
        h0 = p0.initiate("g", "t")
        p0.wait(h0)
        p1 = Program(1)  # 空程序：直接结束

        with self.assertRaises(MemberExitedError) as context:
            Executor([group], {0: p0, 1: p1}).run()

        error = context.exception
        self.assertEqual(error.waiter, 0)
        self.assertEqual(error.group, "g")
        self.assertEqual(error.ordinal, 1)
        self.assertEqual(error.missing_rank, 1)
        self.assertEqual(error.missing_members, (1,))
        edge = error.as_wait_edge()
        self.assertEqual((edge.waiter, edge.group, edge.ordinal, edge.missing_rank),
                         (0, "g", 1, 1))
        self.assertIn("成员缺席", str(error))

    def test_member_exits_after_first_op_but_before_second(self) -> None:
        group = Group("g", [0, 1])
        p0 = Program(0)
        p0.initiate("g", "t1")
        h02 = p0.initiate("g", "t2")
        p0.wait(h02)
        p1 = Program(1)
        p1.initiate("g", "t1")  # 只完成第 1 次发起就结束

        with self.assertRaises(MemberExitedError) as context:
            Executor([group], {0: p0, 1: p1}).run()

        error = context.exception
        self.assertEqual((error.group, error.ordinal, error.missing_rank),
                         ("g", 2, 1))

    def test_member_exited_takes_priority_over_cycle_reporting(self) -> None:
        # 三个两组本可构成 0->1->2->0 的环，但 rank 2 是空程序，第一轮即结束；
        # rank 0 在 C 组等待的位置因此永远收不齐，必须按“成员缺席”而非死锁报告。
        g01 = Group("A", [0, 1])
        g12 = Group("B", [1, 2])
        g20 = Group("C", [2, 0])
        p0 = Program(0)
        p0.wait(p0.initiate("C", "x"))  # C 组第 1 次，缺已结束的 rank 2
        p1 = Program(1)
        p1.wait(p1.initiate("A", "x"))  # A 组第 1 次，缺 rank 0
        p2 = Program(2)  # 空程序：立即结束，无任何指令

        with self.assertRaises(MemberExitedError) as context:
            Executor([g01, g12, g20], {0: p0, 1: p1, 2: p2}).run()

        error = context.exception
        self.assertEqual(error.waiter, 0)
        self.assertEqual((error.group, error.ordinal, error.missing_rank),
                         ("C", 1, 2))
        edge = error.as_wait_edge()
        self.assertEqual(edge.missing_rank, 2)


class DeadlockTest(unittest.TestCase):
    def test_two_rank_cross_group_cycle(self) -> None:
        ga = Group("A", [0, 1])
        gb = Group("B", [0, 1])
        p0 = Program(0)
        h0 = p0.initiate("A", "x")
        p0.wait(h0)
        p1 = Program(1)
        h1 = p1.initiate("B", "x")
        p1.wait(h1)

        with self.assertRaises(DeadlockError) as context:
            Executor([ga, gb], {0: p0, 1: p1}).run()

        chain = context.exception.chain
        self.assertEqual(len(chain), 2)
        waiters = [edge.waiter for edge in chain]
        self.assertEqual(sorted(waiters), [0, 1])
        for index, edge in enumerate(chain):
            self.assertEqual(
                edge.missing_rank, chain[(index + 1) % len(chain)].waiter
            )
        self.assertIn("闭合等待链", str(context.exception))

    def test_three_rank_cycle_with_three_groups(self) -> None:
        g01 = Group("G01", [0, 1])
        g12 = Group("G12", [1, 2])
        g20 = Group("G20", [2, 0])
        p0 = Program(0)
        e0 = p0.initiate("G01", "t")
        p0.wait(e0)  # 缺 rank 1 的 G01 发起
        p1 = Program(1)
        e1 = p1.initiate("G12", "t")
        p1.wait(e1)  # 缺 rank 2 的 G12 发起
        p2 = Program(2)
        e2 = p2.initiate("G20", "t")
        p2.wait(e2)  # 缺 rank 0 的 G20 发起

        with self.assertRaises(DeadlockError) as context:
            Executor([g01, g12, g20], {0: p0, 1: p1, 2: p2}).run()

        chain = context.exception.chain
        self.assertEqual(len(chain), 3)
        self.assertEqual(sorted(edge.waiter for edge in chain), [0, 1, 2])
        for index, edge in enumerate(chain):
            self.assertEqual(
                edge.missing_rank, chain[(index + 1) % 3].waiter
            )
        # 每条边都应标明具体的组与第几次发起。
        described = {(edge.group, edge.ordinal) for edge in chain}
        self.assertEqual(described, {("G01", 1), ("G12", 1), ("G20", 1)})

    def test_cycle_can_reference_second_ordinal(self) -> None:
        ga = Group("A", [0, 1])
        gb = Group("B", [0, 1])
        p0 = Program(0)
        p0.initiate("A", "a1")
        p0.initiate("B", "b1")
        a02 = p0.initiate("A", "a2")
        p0.wait(a02)  # 等 A 组第 2 次，缺 rank 1 的第 2 次 A 发起
        p1 = Program(1)
        p1.initiate("A", "a1")
        p1.initiate("B", "b1")
        b12 = p1.initiate("B", "b2")
        p1.wait(b12)  # 等 B 组第 2 次，缺 rank 0 的第 2 次 B 发起

        with self.assertRaises(DeadlockError) as context:
            Executor([ga, gb], {0: p0, 1: p1}).run()

        chain = context.exception.chain
        self.assertEqual(len(chain), 2)
        ordinals = {edge.waiter: edge.ordinal for edge in chain}
        self.assertEqual(ordinals, {0: 2, 1: 2})
        groups = {edge.waiter: edge.group for edge in chain}
        self.assertEqual(groups, {0: "A", 1: "B"})

    def test_participating_third_rank_breaks_no_cycle_when_also_blocked(self) -> None:
        # 三人组中三人各自等待下一人的同组第 1 次发起。
        group = Group("ring", [0, 1, 2])
        p0 = Program(0)
        p0.initiate("ring", "t")
        # rank 0 已发起，需要另一个组让三人互相等待：
        g01 = Group("g01", [0, 1])
        g12 = Group("g12", [1, 2])
        g20 = Group("g20", [2, 0])
        p0.wait(p0.initiate("g01", "x"))
        p1 = Program(1)
        p1.initiate("ring", "t")
        p1.wait(p1.initiate("g12", "x"))
        p2 = Program(2)
        p2.initiate("ring", "t")
        p2.wait(p2.initiate("g20", "x"))

        with self.assertRaises(DeadlockError) as context:
            Executor([group, g01, g12, g20], {0: p0, 1: p1, 2: p2}).run()

        chain = context.exception.chain
        self.assertEqual(len(chain), 3)
        for index, edge in enumerate(chain):
            self.assertEqual(
                edge.missing_rank, chain[(index + 1) % 3].waiter
            )


if __name__ == "__main__":
    unittest.main()
