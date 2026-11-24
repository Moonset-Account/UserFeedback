"""Tests for synthesis, round budgets and minimum buffer search."""

import unittest

from adapter_synth.examples import (
    already_terminated,
    capacity_two_contention,
    fifo_order_incompatible,
    nondeterministic_response,
    one_way_send,
    simple_request_reply,
)
from adapter_synth.synthesize import minimum_buffer_capacity, synthesize
from adapter_synth.model import BufferConfig


class TestSynthesize(unittest.TestCase):
    def test_simple_request_reply_solvable(self):
        proto, _ = simple_request_reply()
        result = synthesize(proto, BufferConfig(1, 1, 1, 1), 16)
        self.assertTrue(result.solvable)
        self.assertEqual(result.rounds_required, 4)
        self.assertIsNotNone(result.strategy)
        # The action table must be a list of state/action rows.
        table = result.strategy.action_table()
        self.assertIsInstance(table, list)
        self.assertTrue(all("state" in row and "adapter_action" in row
                            for row in table))

    def test_round_budget_too_tight_is_unsolvable(self):
        proto, _ = simple_request_reply()
        # Four rounds are required; allowing only three must fail even though
        # a winning policy exists in principle.
        result = synthesize(proto, BufferConfig(1, 1, 1, 1), 3)
        self.assertFalse(result.solvable)
        self.assertIsNone(result.strategy)
        self.assertIsNotNone(result.counter_strategy)

    def test_negative_budget_rejected(self):
        proto, _ = simple_request_reply()
        with self.assertRaises(ValueError):
            synthesize(proto, BufferConfig(1, 1, 1, 1), -1)

    def test_already_terminated_needs_zero_rounds_and_zero_buffers(self):
        proto, _ = already_terminated()
        result = synthesize(proto, BufferConfig(0, 0, 0, 0), 5)
        self.assertTrue(result.solvable)
        self.assertEqual(result.rounds_required, 0)
        self.assertEqual(result.strategy.action_table(), [])

    def test_nondeterministic_response_wins_on_every_branch(self):
        proto, _ = nondeterministic_response()
        result = synthesize(proto, BufferConfig(1, 1, 1, 1), 32)
        self.assertTrue(result.solvable)

    def test_strategy_covers_every_required_post_env_state(self):
        from adapter_synth.game import (
            GameState, admissible_env_choices, apply_env,
            enumerate_reachable, PASS,
        )
        proto, _ = simple_request_reply()
        config = BufferConfig(1, 1, 1, 1)
        result = synthesize(proto, config, 16)
        policy = result.strategy.policy
        for state in enumerate_reachable(proto, config):
            if result.ranks[state] >= 10**9 or state.is_goal(proto):
                continue
            for choice in admissible_env_choices(proto, config, state):
                mid = state if choice is PASS else apply_env(state, choice)
                if mid.is_goal(proto):
                    continue
                self.assertIn(mid, policy)


class TestMinimumBufferCapacity(unittest.TestCase):
    def test_simple_minimum(self):
        proto, _ = simple_request_reply()
        report = minimum_buffer_capacity(proto, 32)
        self.assertTrue(report["solvable"])
        self.assertEqual(report["minimal_total_capacity"], 4)
        caps = [tuple(c["capacities"]) for c in report["minimum_configs"]]
        self.assertIn((1, 1, 1, 1), caps)

    def test_one_way_uses_only_two_queues(self):
        proto, _ = one_way_send()
        report = minimum_buffer_capacity(proto, 32)
        self.assertEqual(report["minimal_total_capacity"], 2)
        caps = [tuple(c["capacities"]) for c in report["minimum_configs"]]
        self.assertIn((1, 1, 0, 0), caps)

    def test_capacity_two_contention_requires_two_slots(self):
        proto, _ = capacity_two_contention()
        report = minimum_buffer_capacity(proto, 64)
        self.assertTrue(report["solvable"])
        self.assertEqual(report["minimal_total_capacity"], 5)
        # Every minimum vector must use at least one capacity-2 queue, and no
        # all-ones vector may appear among the winners.
        for cfg in report["minimum_configs"]:
            self.assertTrue(any(c == 2 for c in cfg["capacities"]))
            self.assertNotEqual(tuple(cfg["capacities"]), (1, 1, 1, 1))

    def test_unsolvable_returns_counter_strategy(self):
        proto, _ = fifo_order_incompatible()
        report = minimum_buffer_capacity(proto, 64)
        self.assertFalse(report["solvable"])
        self.assertIsNone(report["minimal_total_capacity"])
        self.assertIsNotNone(report["counter_strategy"])
        self.assertGreater(report["counter_branch_count"], 0)


if __name__ == "__main__":
    unittest.main()
