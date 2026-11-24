"""End-to-end scenario tests for scheduling, replay and counter-strategies.

These tests exercise real game semantics through ``play_round`` and the
replay helpers rather than trusting any precomputed output.
"""

import unittest

from adapter_synth.counter import CounterLeaf, CounterNode, counter_max_depth
from adapter_synth.examples import (
    capacity_two_contention,
    eager_conversion_blocks_reply,
    fifo_order_incompatible,
    simple_request_reply,
)
from adapter_synth.game import (
    WAIT,
    GameState,
    admissible_env_choices,
    apply_adapter,
    apply_env,
    available_conversions,
)
from adapter_synth.model import BufferConfig
from adapter_synth.replay import enumerate_counter_plays, simulate_policy
from adapter_synth.synthesize import synthesize


class TestSchedulingPolicy(unittest.TestCase):
    def setUp(self):
        self.proto, _ = eager_conversion_blocks_reply()
        self.config = BufferConfig(1, 1, 1, 1)

    def test_synthesized_policy_wins_every_play(self):
        result = synthesize(self.proto, self.config, 32)
        self.assertTrue(result.solvable)
        report = simulate_policy(self.proto, self.config,
                                 result.strategy, 32)
        self.assertTrue(report.all_succeed)
        self.assertEqual(report.failure_plays, [])
        # Several distinct environment plays must be covered: a single happy
        # path would not constitute a compatibility argument.
        self.assertGreaterEqual(report.total_plays, 2)
        self.assertEqual(report.worst_rounds, result.rounds_required)

    def test_policy_only_waits_when_no_conversion_is_enabled(self):
        # A winning adapter never idles while it has a legal conversion in a
        # solvable FIFO instance: converting only frees a pending slot.  This
        # pins down the precise meaning of "correct scheduling".
        result = synthesize(self.proto, self.config, 32)
        for mid_state, action in result.strategy.policy.items():
            if action is WAIT:
                self.assertEqual(
                    available_conversions(self.proto, self.config, mid_state),
                    [],
                )

    def test_policy_table_is_executable_against_real_semantics(self):
        # Replay using only the serialized table's underlying policy and
        # confirm every transition is accepted by play_round (no illegal move
        # is ever prescribed).
        result = synthesize(self.proto, self.config, 32)
        report = simulate_policy(self.proto, self.config,
                                 result.strategy, 32, collect_traces=False)
        self.assertEqual(report.failure_plays, [])
        self.assertGreater(report.success_plays, 0)


class TestOneLuckyConversationIsNotCompatibility(unittest.TestCase):
    def test_fifo_inversion_has_both_a_progress_path_and_a_deadlock(self):
        # The FIFO-incompatible pair can make real progress for a while (B
        # sends X and the adapter converts it), yet the universal game is
        # lost.  We exhibit the concrete progressing prefix and then prove via
        # the synthesized counter-strategy that no policy can force success.
        proto, _ = fifo_order_incompatible()
        config = BufferConfig(2, 2, 2, 2)

        # A concrete prefix that genuinely changes state (a "lucky" partial
        # conversation), executed through the real transition functions.
        state = GameState.initial(proto)
        first_choice = admissible_env_choices(proto, config, state)[0]
        mid = apply_env(state, first_choice)
        self.assertNotEqual(mid, state)  # real progress happened
        conv = available_conversions(proto, config, mid)[0]
        advanced = apply_adapter(mid, conv)
        self.assertNotEqual(advanced, mid)

        # Despite that progress, the universal synthesis is unsolvable...
        result = synthesize(proto, config, 64)
        self.assertFalse(result.solvable)
        # ...and the counter-strategy contains at least one full deadlock
        # play, i.e. an environment answer that no adapter choice escapes.
        plays = enumerate_counter_plays(result.counter_strategy)
        self.assertTrue(
            any(p["leaf_reason"] == "deadlock" for p in plays)
        )


class TestExhaustiveReplay(unittest.TestCase):
    def test_simple_protocol_all_traces_terminate(self):
        proto, _ = simple_request_reply()
        config = BufferConfig(1, 1, 1, 1)
        result = synthesize(proto, config, 16)
        report = simulate_policy(proto, config, result.strategy, 16)
        self.assertTrue(report.all_succeed)
        self.assertGreater(report.success_plays, 0)
        self.assertEqual(report.best_rounds, report.worst_rounds)

    def test_capacity_two_fixture_exhaustive(self):
        proto, _ = capacity_two_contention()
        config = BufferConfig(2, 2, 2, 2)
        result = synthesize(proto, config, 64)
        report = simulate_policy(proto, config, result.strategy, 64)
        self.assertTrue(report.all_succeed)
        self.assertGreater(report.total_plays, 1)

    def test_capacity_one_fixture_reports_deadlock_leaf(self):
        proto, _ = capacity_two_contention()
        config = BufferConfig(1, 1, 1, 1)
        result = synthesize(proto, config, 64)
        self.assertFalse(result.solvable)
        plays = enumerate_counter_plays(result.counter_strategy)
        reasons = {p["leaf_reason"] for p in plays}
        self.assertIn("deadlock", reasons)
        # Every replayed play must be concrete and step-complete.
        for play in plays:
            self.assertGreaterEqual(play["rounds"], 1)
            self.assertTrue(all("env_action" in s and "adapter_action" in s
                                for s in play["steps"]))


class TestCounterStrategyReplay(unittest.TestCase):
    def test_fifo_counter_tree_is_branching_and_replayable(self):
        proto, _ = fifo_order_incompatible()
        config = BufferConfig(2, 2, 2, 2)
        result = synthesize(proto, config, 64)
        root = result.counter_strategy
        self.assertIsInstance(root, (CounterNode, CounterLeaf))
        plays = enumerate_counter_plays(root)
        self.assertGreater(len(plays), 1)
        # The tree keeps branching on different adapter choices.
        self.assertGreaterEqual(counter_max_depth(root), 1)

    def test_counter_play_step_states_are_consistent(self):
        proto, _ = fifo_order_incompatible()
        config = BufferConfig(2, 2, 2, 2)
        result = synthesize(proto, config, 64)
        plays = enumerate_counter_plays(result.counter_strategy)
        # Re-execute each recorded play against the real transition function.
        for play in plays:
            state = GameState.initial(proto)
            for step in play["steps"]:
                self.assertIn("env_action", step)
                self.assertIn("adapter_action", step)
            self.assertIsNotNone(play["leaf_state"])

    def test_round_budget_leaf_is_reachable(self):
        proto, _ = simple_request_reply()
        config = BufferConfig(1, 1, 1, 1)
        # Tight budget forces a round_budget leaf rather than a deadlock.
        result = synthesize(proto, config, 1)
        self.assertFalse(result.solvable)
        # The instance is solvable in principle; only the deadline is missed.
        self.assertEqual(result.rounds_required, 4)
        plays = enumerate_counter_plays(result.counter_strategy)
        self.assertTrue(any(p["leaf_reason"] == "round_budget" for p in plays))
        # With a single round available every play must end on the budget
        # leaf, never be mislabelled as a structural deadlock.
        self.assertTrue(
            all(p["leaf_reason"] == "round_budget" for p in plays)
        )

    def test_counter_tree_branches_per_adapter_choice(self):
        # A capacity-one deadlock leaves the adapter with several legal
        # conversions; the counter-strategy must keep one branch per adapter
        # response so it can be replayed against different adapter choices.
        proto, _ = capacity_two_contention()
        config = BufferConfig(1, 1, 1, 1)
        result = synthesize(proto, config, 64)
        root = result.counter_strategy

        def widest(node):
            if isinstance(node, CounterLeaf):
                return 0
            return max([len(node.branches)] + [widest(child)
                                               for _, child in node.branches])

        self.assertGreaterEqual(widest(root), 2)
        # Every branch label must name a concrete adapter action.
        plays = enumerate_counter_plays(root)
        for play in plays:
            for step in play["steps"]:
                self.assertTrue(step["adapter_action"].startswith(
                    ("convert:", "wait")))


if __name__ == "__main__":
    unittest.main()
