"""Tests for game mechanics: moves, rounds, deadlock and the rank fixpoint."""

import unittest

from adapter_synth.examples import (
    capacity_two_contention,
    fifo_order_incompatible,
    one_way_send,
    simple_request_reply,
)
from adapter_synth.game import (
    INF,
    PASS,
    WAIT,
    GameState,
    admissible_env_choices,
    apply_env,
    available_conversions,
    compute_ranks,
    enabled_moves,
    enumerate_reachable,
    play_round,
)
from adapter_synth.model import Act, BufferConfig


class TestEnabledMoves(unittest.TestCase):
    def setUp(self):
        self.proto, _ = simple_request_reply()
        self.cfg = BufferConfig(1, 1, 1, 1)
        self.start = GameState.initial(self.proto)

    def test_initial_send_enabled(self):
        moves = enabled_moves(self.proto, self.cfg, self.start)
        self.assertEqual(len(moves), 1)
        self.assertEqual((moves[0].peer, moves[0].act, moves[0].message),
                         ("a", Act.SEND, "REQ"))

    def test_send_blocked_when_pending_full(self):
        # Manually fill the A2B pending FIFO to capacity.
        state = GameState("a0", "b0", ("REQ",), (), (), ())
        self.assertEqual(enabled_moves(self.proto, self.cfg, state), [])

    def test_recv_requires_matching_head(self):
        # B expects REQ at its head.
        good = GameState("a1", "b0", (), ("REQ",), (), ())
        self.assertTrue(enabled_moves(self.proto, self.cfg, good))
        bad = GameState("a1", "b0", (), ("WRONG",), (), ())
        self.assertEqual(enabled_moves(self.proto, self.cfg, bad), [])

    def test_zero_capacity_blocks_send(self):
        proto, _ = one_way_send()
        cfg = BufferConfig(0, 0, 0, 0)
        state = GameState.initial(proto)
        self.assertEqual(enabled_moves(proto, cfg, state), [])


class TestEnvironmentChoices(unittest.TestCase):
    def setUp(self):
        self.proto, _ = simple_request_reply()
        self.cfg = BufferConfig(1, 1, 1, 1)

    def test_pass_only_when_no_peer_move(self):
        blocked = GameState("a0", "b0", ("REQ",), (), (), ())
        choices = admissible_env_choices(self.proto, self.cfg, blocked)
        self.assertEqual(choices, [PASS])

    def test_no_pass_when_peer_move_exists(self):
        start = GameState.initial(self.proto)
        choices = admissible_env_choices(self.proto, self.cfg, start)
        self.assertNotIn(PASS, choices)
        self.assertTrue(all(c is not PASS for c in choices))


class TestRoundSemantics(unittest.TestCase):
    def setUp(self):
        self.proto, _ = simple_request_reply()
        self.cfg = BufferConfig(1, 1, 1, 1)

    def test_forced_pass_and_wait_is_deadlock(self):
        blocked = GameState("a0", "b0", ("REQ",), (), (), ())
        outcome = play_round(self.proto, self.cfg, blocked, PASS, WAIT)
        self.assertTrue(outcome.deadlock)
        self.assertEqual(outcome.end_state, blocked)

    def test_forced_pass_with_conversion_progresses(self):
        # pending head REQ can be converted while no peer move is enabled.
        blocked = GameState("a0", "b0", ("REQ",), (), (), ())
        conv = available_conversions(self.proto, self.cfg, blocked)
        self.assertEqual(len(conv), 1)
        outcome = play_round(self.proto, self.cfg, blocked, PASS, conv[0])
        self.assertFalse(outcome.deadlock)
        self.assertEqual(outcome.end_state.converted_a2b, ("REQ",))
        self.assertEqual(outcome.end_state.pending_a2b, ())

    def test_illegal_adapter_action_raises(self):
        start = GameState.initial(self.proto)
        moves = enabled_moves(self.proto, self.cfg, start)
        with self.assertRaises(ValueError):
            play_round(self.proto, self.cfg, start, moves[0],
                       object())  # not a Convert/WAIT

    def test_illegal_env_action_raises(self):
        start = GameState.initial(self.proto)
        with self.assertRaises(ValueError):
            play_round(self.proto, self.cfg, start, PASS, WAIT)

    def test_full_request_reply_trace(self):
        state = GameState.initial(self.proto)
        # A sends REQ, adapter converts, B receives, B sends RESP, etc.
        trace = []
        for _ in range(8):
            if state.is_goal(self.proto):
                break
            choice = admissible_env_choices(self.proto, self.cfg, state)[0]
            if choice is PASS:
                mid = state
            else:
                mid = apply_env(state, choice)
            if mid.is_goal(self.proto):
                state = mid
                trace.append("goal-env")
                continue
            convs = available_conversions(self.proto, self.cfg, mid)
            action = convs[0] if convs else WAIT
            outcome = play_round(self.proto, self.cfg, state, choice, action)
            self.assertFalse(outcome.deadlock)
            state = outcome.end_state
            trace.append((choice.peer if hasattr(choice, "peer") else "pass",
                          action is not WAIT))
        self.assertTrue(state.is_goal(self.proto))


class TestReachabilityAndRanks(unittest.TestCase):
    def test_reachable_set_finite(self):
        proto, _ = simple_request_reply()
        cfg = BufferConfig(2, 2, 2, 2)
        states = enumerate_reachable(proto, cfg)
        self.assertGreater(len(states), 0)
        for s in states:
            self.assertLessEqual(len(s.pending_a2b), 2)
            self.assertLessEqual(len(s.converted_a2b), 2)

    def test_simple_protocol_winning_rank(self):
        proto, _ = simple_request_reply()
        cfg = BufferConfig(1, 1, 1, 1)
        ranks = compute_ranks(proto, cfg)
        self.assertEqual(ranks[GameState.initial(proto)], 4)
        goal_states = [s for s in ranks if s.is_goal(proto)]
        self.assertTrue(goal_states)
        self.assertTrue(all(ranks[s] == 0 for s in goal_states))

    def test_fifo_inversion_unsolvable_at_max_capacity(self):
        proto, _ = fifo_order_incompatible()
        cfg = BufferConfig(2, 2, 2, 2)
        ranks = compute_ranks(proto, cfg)
        self.assertEqual(ranks[GameState.initial(proto)], INF)

    def test_capacity_one_deadlocks_capacity_two_wins(self):
        proto, _ = capacity_two_contention()
        small = compute_ranks(proto, BufferConfig(1, 1, 1, 1))
        large = compute_ranks(proto, BufferConfig(2, 2, 2, 2))
        self.assertEqual(small[GameState.initial(proto)], INF)
        self.assertLess(large[GameState.initial(proto)], INF)


if __name__ == "__main__":
    unittest.main()
