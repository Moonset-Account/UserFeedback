import unittest

import _bootstrap  # noqa: F401  (workspace-root path bootstrap)

from build_semantics import Step, UNSET, analyze, build_pipeline, replay
from build_semantics.model import State, RUNNING


class LockSemanticsTests(unittest.TestCase):
    def test_two_steps_sharing_executor_cannot_overlap(self):
        # The lock alone forbids overlap but not ordering; combine it with a
        # dependency so b necessarily starts after a has published.
        a = Step("a", ("x",), {(0,): {"x": 1}}, lock="gcc")
        b = Step("b", ("x",), {(0,): {"y": 2}, (1,): {"y": 3}},
                 lock="gcc", deps=("a",))
        p = build_pipeline([a, b], initial_values={"x": 0},
                           outputs_of_interest=["x", "y"])
        result = analyze(p)
        self.assertTrue(result.deterministic)
        self.assertEqual(result.final_outputs, (1, 3))

    def test_lock_serialization_does_not_impose_ordering(self):
        # Without a dependency either holder may go first; the lock only
        # removes the overlapping interleavings, not the two serial orders.
        a = Step("a", ("x",), {(0,): {"x": 1}}, lock="gcc")
        b = Step("b", ("x",),
                 {(0,): {"y": 2}, (1,): {"y": 3}, (UNSET,): {"y": 0}},
                 lock="gcc")
        p = build_pipeline([a, b], initial_values={"x": 0},
                           outputs_of_interest=["y"])
        result = analyze(p)
        self.assertFalse(result.deterministic)
        self.assertEqual(set(result.possible_outputs), {(2,), (3,)})
        for trace in result.traces:
            # Every witness must be a serial, non-overlapping schedule.
            running = set()
            for text in trace:
                kind, rest = text.split("(")
                name = rest[:-1]
                if kind == "start":
                    self.assertFalse(running, f"overlap in {trace}")
                    running.add(name)
                else:
                    running.discard(name)

    def test_replay_rejects_start_while_executor_is_held(self):
        a = Step("a", (), {(): {"x": 1}}, lock="gcc")
        b = Step("b", (), {(): {"y": 2}}, lock="gcc")
        p = build_pipeline([a, b])
        # a starts and holds the lock; starting b must be illegal.
        from build_semantics import TraceError
        with self.assertRaises(TraceError):
            replay(p, ["start(a)", "start(b)"])

    def test_lock_released_at_end_allows_next_holder(self):
        a = Step("a", (), {(): {"x": 1}}, lock="gcc")
        b = Step("b", (), {(): {"y": 2}}, lock="gcc")
        p = build_pipeline([a, b])
        outcome = replay(p, ["start(a)", "end(a)", "start(b)", "end(b)"])
        self.assertEqual(outcome.statuses, ("ended", "ended"))
        self.assertEqual(outcome.lock_owner, {})

    def test_different_executors_do_not_block_each_other(self):
        a = Step("a", (), {(): {"x": 1}}, lock="gcc")
        b = Step("b", (), {(): {"y": 2}}, lock="ld")
        p = build_pipeline([a, b])
        outcome = replay(p, ["start(a)", "start(b)", "end(a)", "end(b)"])
        self.assertEqual(outcome.statuses, ("ended", "ended"))

    def test_lock_owner_reflects_running_holder(self):
        a = Step("a", (), {(): {"x": 1}}, lock="gcc")
        p = build_pipeline([a])
        outcome = replay(p, ["start(a)"])
        self.assertEqual(outcome.statuses, (RUNNING,))
        self.assertEqual(outcome.lock_owner, {"gcc": "a"})

    def test_locked_race_witness_prefers_deepest_legal_split(self):
        # Two locked writers serialize, but a free reader can still freeze
        # the intermediate value while the second writer runs.
        w1 = Step("w1", (), {(): {"v": 1}}, lock="e")
        w2 = Step("w2", (), {(): {"v": 2}}, lock="e")
        rd = Step("rd", ("v",),
                  {(1,): {"out": "old"}, (2,): {"out": "new"},
                   (UNSET,): {"out": "none"}})
        p = build_pipeline([w1, w2, rd], outputs_of_interest=["out"])
        result = analyze(p)
        self.assertFalse(result.deterministic)
        for trace in result.traces:
            # No two lock-holding steps may be running at once: between the
            # start of w2 and end of w1 the trace would be illegal, and the
            # replay engine would have raised.
            replay(p, trace)
        self.assertNotEqual(*[replay(p, t).outputs for t in result.traces])


if __name__ == "__main__":
    unittest.main()
