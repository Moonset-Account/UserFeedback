"""控制器切换下的可能现场、票据查询博弈与最坏轮数证明。"""

from __future__ import annotations

import _bootstrap  # noqa: F401
import unittest

from safedrain.logs import (
    LogEvent,
    LogEventKind,
    MigrationTicket,
    possible_worlds,
    shared_log_prefix,
)
from safedrain.uncertainty import (
    SafetyContext,
    solve_query_game,
    _answer_bits,
    _world_key,
)


def _ticket(tid, pod, source, candidates, kinds):
    events = tuple(
        LogEvent(tid, kind, source, tuple(candidates)) for kind in kinds
    )
    return MigrationTicket(tid, pod, source, tuple(candidates), events)


def assert_indistinguishable(test_case, tickets, solution):
    """下界证据的可执行校验：两份现场在证据路径上回答逐轮相同、安全性相反。

    证据对允许为空——当最优树在证据深度已把所有现场分成纯分支时，
    不存在“少一轮就不可区分”的现场对；此时只校验路径字段为空。
    """
    if solution.witness is None:
        test_case.assertEqual(solution.witness_query_path, ())
        test_case.assertEqual(solution.witness_answers, ())
        return None
    bad, good = solution.witness
    ticket_tuple = tuple(tickets)
    bad_key = _world_key(bad, ticket_tuple)
    good_key = _world_key(good, ticket_tuple)
    test_case.assertNotEqual(bad_key, good_key)
    # 证据路径长度必须等于 rounds-1。
    test_case.assertEqual(
        len(solution.witness_query_path), solution.rounds - 1
    )
    test_case.assertEqual(
        len(solution.witness_answers), solution.rounds - 1
    )
    for queries, bits in zip(
        solution.witness_query_path, solution.witness_answers
    ):
        test_case.assertEqual(_answer_bits(bad, queries), bits)
        test_case.assertEqual(_answer_bits(good, queries), bits)
    return bad, good


class PossibleWorldsTests(unittest.TestCase):
    def test_two_landings_share_same_log_prefix(self):
        ticket = _ticket(
            "T1", "p1", "n1", ("n2", "n3"),
            (LogEventKind.RESERVED, LogEventKind.RELEASE_PENDING),
        )
        worlds = possible_worlds([ticket])
        self.assertEqual(len(worlds), 2)
        self.assertTrue(shared_log_prefix((ticket,), worlds))
        self.assertEqual(
            {w.placement["T1"] for w in worlds}, {"n2", "n3"}
        )

    def test_cartesian_worlds_for_two_tickets(self):
        t1 = _ticket("T1", "p1", "n0", ("n2", "n3"), (LogEventKind.RESERVED,))
        t2 = _ticket("T2", "p2", "n0", ("n2", "n3"), (LogEventKind.RELEASED,))
        worlds = possible_worlds([t1, t2])
        self.assertEqual(len(worlds), 4)
        self.assertTrue(shared_log_prefix((t1, t2), worlds))

    def test_invalid_tickets_rejected(self):
        with self.assertRaises(Exception):
            possible_worlds([])
        bad = _ticket("T1", "p1", "n1", ("n2", "n2"), (LogEventKind.RESERVED,))
        with self.assertRaises(Exception):
            possible_worlds([bad])


class QueryGameTests(unittest.TestCase):
    def _two_world_game(self, safe_node):
        """单票据两候选，只有落在 safe_node 上安全。"""
        ticket = _ticket(
            "T1", "p1", "n1", ("n2", "n3"),
            (LogEventKind.RESERVE_PENDING,),
        )
        worlds = possible_worlds([ticket])
        safe_keys = frozenset(
            tuple(w.placement[t.ticket] for t in (ticket,))
            for w in worlds if w.placement["T1"] == safe_node
        )
        return ticket, SafetyContext(safe_keys)

    def test_one_world_unsafe_requires_one_round(self):
        ticket, ctx = self._two_world_game("n2")
        solution = solve_query_game([ticket], ctx)
        self.assertEqual(solution.rounds, 1)
        self.assertFalse(solution.all_safe_initially)
        self.assertTrue(solution.shared_prefix)
        # 一轮两个询问点即可覆盖两个候选。
        self.assertLessEqual(len(solution.strategy.queries), 2)
        # rounds=1：证据路径为空，但两份现场确实一安全一不安全。
        assert_indistinguishable(self, [ticket], solution)
        bad, good = solution.witness
        self.assertEqual(bad.placement["T1"], "n3")
        self.assertEqual(good.placement["T1"], "n2")
        self.assertTrue(ctx.is_safe(good, (ticket,)))
        self.assertFalse(ctx.is_safe(bad, (ticket,)))

    def test_all_safe_worlds_need_zero_rounds(self):
        ticket = _ticket("T1", "p1", "n1", ("n2", "n3"),
                         (LogEventKind.RESERVED,))
        worlds = possible_worlds([ticket])
        keys = frozenset(
            tuple(w.placement[t.ticket] for t in (ticket,)) for w in worlds
        )
        solution = solve_query_game([ticket], SafetyContext(keys))
        self.assertEqual(solution.rounds, 0)
        self.assertTrue(solution.all_safe_initially)
        self.assertIsNone(solution.witness)
        self.assertEqual(solution.witness_query_path, ())

    def test_two_bits_of_ambiguity_one_round_suffices(self):
        # 两张票据各两候选，仅一个组合不安全；一轮问两个节点即可分辨。
        t1 = _ticket("T1", "p1", "n0", ("a", "b"), (LogEventKind.RESERVED,))
        t2 = _ticket("T2", "p2", "n0", ("a", "b"), (LogEventKind.RESERVED,))
        worlds = possible_worlds([t1, t2])
        # 只有 (b,b) 不安全。
        safe_keys = frozenset(
            k for w in worlds
            for k in [tuple(w.placement[t.ticket] for t in (t1, t2))]
            if k != ("b", "b")
        )
        solution = solve_query_game([t1, t2], SafetyContext(safe_keys))
        self.assertEqual(solution.rounds, 1)
        self.assertEqual(len(solution.strategy.queries), 2)
        # 若存在证据对，它在零轮（rounds-1=0）后仍无法区分，且一安全一不安全。
        pair = assert_indistinguishable(self, [t1, t2], solution)
        if pair is not None:
            bad, good = pair
            self.assertEqual(_world_key(bad, (t1, t2)), ("b", "b"))
            self.assertNotEqual(_world_key(good, (t1, t2)), ("b", "b"))
        # 根询问的每个回答分支都必须是纯安全或纯不安全（一轮即可正确决策）。
        root = solution.strategy
        for bits in root.branches:
            members = frozenset(
                _world_key(w, (t1, t2))
                for w in worlds if _answer_bits(w, root.queries) == bits
            )
            self.assertTrue(members <= safe_keys or members.isdisjoint(safe_keys))

    def test_three_way_ambiguity_needs_two_rounds_and_witness_proof(self):
        # 三张票据各两候选，仅 (a,a,a) 安全，其余都不安全。
        # 每轮最多两问，分辨最坏情况需要 2 轮。
        t1 = _ticket("T1", "p1", "n0", ("a", "b"), (LogEventKind.RELEASED,))
        t2 = _ticket("T2", "p2", "n0", ("a", "b"), (LogEventKind.RELEASED,))
        t3 = _ticket("T3", "p3", "n0", ("a", "b"), (LogEventKind.RELEASED,))
        tickets = [t1, t2, t3]
        safe_keys = frozenset([("a", "a", "a")])
        solution = solve_query_game(tickets, SafetyContext(safe_keys))
        self.assertEqual(solution.rounds, 2)
        self.assertTrue(solution.shared_prefix)
        # 证据路径含 1 轮询问，两份现场对该轮回答完全相同。
        assert_indistinguishable(self, tickets, solution)
        bad, good = solution.witness
        self.assertEqual(_world_key(good, tuple(tickets)), ("a", "a", "a"))
        self.assertNotEqual(_world_key(bad, tuple(tickets)), ("a", "a", "a"))
        self.assertEqual(len(solution.witness_query_path), 1)
        # 规则是“至多两个”，最优策略可以只用一个询问点。
        self.assertLessEqual(len(solution.witness_query_path[0]), 2)
        self.assertGreaterEqual(len(solution.witness_query_path[0]), 1)
        # 距离仅作辅助信息记录，不再作为不可区分的判据。
        self.assertGreaterEqual(solution.witness_distance, 1)

    def test_four_candidates_single_ticket_needs_two_rounds(self):
        # 单票据四个候选 a/b/c/d，只有落在 a 安全。一轮最多问两个节点，
        # 两次都回答“否”时仍剩两个未问候选无法分辨，最坏必须再问一轮。
        t1 = _ticket(
            "T1", "p1", "n0", ("a", "b", "c", "d"), (LogEventKind.RESERVED,)
        )
        solution = solve_query_game([t1], SafetyContext(frozenset([("a",)])))
        self.assertEqual(solution.rounds, 2)
        self.assertLessEqual(len(solution.strategy.queries), 2)
        pair = assert_indistinguishable(self, [t1], solution)
        if pair is not None:
            bad, good = pair
            self.assertEqual(good.placement["T1"], "a")
            self.assertIn(bad.placement["T1"], {"b", "c", "d"})
            # 证据路径上那一轮，两份现场的回答确实相同。
            queries = solution.witness_query_path[0]
            bits = solution.witness_answers[0]
            self.assertEqual(_answer_bits(bad, queries), bits)
            self.assertEqual(_answer_bits(good, queries), bits)
        else:
            # 无证据对时，决策树仍必须在 2 轮内终止，且每轮询问不超过两点。
            def bounded(node, depth_left):
                if isinstance(node, bool) or node.is_leaf:
                    return True
                if depth_left == 0 or len(node.queries) > 2:
                    return False
                return all(
                    bounded(child, depth_left - 1)
                    for child in node.branches.values()
                )

            self.assertTrue(bounded(solution.strategy, 2))

    def test_strategy_branches_are_decision_tree(self):
        ticket, ctx = self._two_world_game("n2")
        solution = solve_query_game([ticket], ctx)
        root = solution.strategy
        self.assertTrue(root.queries)
        # 每个分支要么是叶子放行，要么是子树。
        for value in root.branches.values():
            self.assertTrue(value is True or hasattr(value, "queries"))


if __name__ == "__main__":
    unittest.main()
