import unittest

import _bootstrap  # noqa: F401  (workspace-root path bootstrap)

from build_semantics import (
    Snapshot,
    Step,
    UNSET,
    analyze,
    analyze_snapshot,
    build_pipeline,
    replay_snapshot,
)
from build_semantics.errors import TraceError, ValidationError
from build_semantics.model import ENDED, RUNNING


def stale_value_pipeline():
    """w publishes v=2; reader may freeze v=1 (stale) while w is running."""
    w = Step("w", (), {(): {"v": 2}}, lock="gcc")
    r = Step("r", ("v",),
             {(1,): {"out": "stale"}, (2,): {"out": "fresh"},
              (UNSET,): {"out": "none"}},
             deps=())
    return build_pipeline([w, r], initial_values={"v": 1},
                          outputs_of_interest=["out"])


class SnapshotResumeTests(unittest.TestCase):
    def test_frozen_inputs_carry_into_continuation(self):
        p = stale_value_pipeline()
        # Breakpoint: r already started and froze the stale value v=1; w is
        # still running and has not published yet.
        snapshot = Snapshot(
            pipeline=p,
            statuses={"w": RUNNING, "r": RUNNING},
            frozen_inputs={"w": (), "r": (1,)},
            published={"v": 1},
            lock_holders={"gcc": "w"},
        )
        result = analyze_snapshot(snapshot)
        # Whatever order the remaining events take, r already froze v=1, so
        # w's late publication cannot change the outcome.  This is exactly
        # the "read old value then finish late" trace that a naive restart
        # analysis would miss.
        self.assertTrue(result.deterministic)
        self.assertEqual(result.final_outputs, ("stale",))
        self.assertEqual(result.root, "snapshot")

    def test_continuation_trace_is_replayable_from_snapshot(self):
        p = stale_value_pipeline()
        snapshot = Snapshot(
            pipeline=p,
            statuses={"w": RUNNING, "r": RUNNING},
            frozen_inputs={"w": (), "r": (1,)},
            published={"v": 1},
            lock_holders={"gcc": "w"},
        )
        outcome = replay_snapshot(snapshot, ["end(w)", "end(r)"])
        self.assertEqual(outcome.statuses, (ENDED, ENDED))
        self.assertEqual(outcome.workspace["v"], 2)
        self.assertEqual(outcome.outputs, ("stale",))

    def test_snapshot_without_frozen_reader_still_has_two_futures(self):
        p = stale_value_pipeline()
        # Breakpoint before r starts: w is running, old value still visible.
        snapshot = Snapshot(
            pipeline=p,
            statuses={"w": RUNNING, "r": "pending"},
            frozen_inputs={"w": ()},
            published={"v": 1},
            lock_holders={"gcc": "w"},
        )
        result = analyze_snapshot(snapshot)
        self.assertFalse(result.deterministic)
        self.assertEqual(set(result.possible_outputs), {("stale",), ("fresh",)})
        # r can start now (freeze 1) or wait for w's publication (freeze 2).
        for trace in result.traces:
            replay_snapshot(snapshot, trace)

    def test_lock_holder_blocks_contending_start_in_resume(self):
        # Build a pipeline with two contenders for the same executor; w is
        # already running at the breakpoint and therefore holds the lock.
        w = Step("w", (), {(): {"v": 2}}, lock="gcc")
        contender = Step("c", (), {(): {"z": 1}}, lock="gcc")
        p2 = build_pipeline([w, contender], initial_values={"v": 1})
        snap2 = Snapshot(
            pipeline=p2,
            statuses={"w": RUNNING, "c": "pending"},
            frozen_inputs={"w": ()},
            published={"v": 1},
            lock_holders={"gcc": "w"},
        )
        with self.assertRaises(TraceError):
            replay_snapshot(snap2, ["start(c)"])

    def test_published_values_are_resumed_not_reinitialized(self):
        w1 = Step("w1", (), {(): {"v": 2}})
        w2 = Step("w2", ("v",),
                  {(2,): {"out": "two"}, (UNSET,): {"out": "none"}})
        p = build_pipeline([w1, w2], outputs_of_interest=["out"])
        snapshot = Snapshot(
            pipeline=p,
            statuses={"w1": ENDED, "w2": "pending"},
            frozen_inputs={},
            published={"v": 2},
            lock_holders={},
        )
        result = analyze_snapshot(snapshot)
        self.assertTrue(result.deterministic)
        self.assertEqual(result.final_outputs, ("two",))

    def test_running_step_without_frozen_inputs_is_rejected(self):
        p = stale_value_pipeline()
        with self.assertRaises(ValidationError):
            Snapshot(
                pipeline=p,
                statuses={"w": RUNNING, "r": "pending"},
                frozen_inputs={},
                published={"v": 1},
                lock_holders={"gcc": "w"},
            )

    def test_lock_holder_must_be_running_and_declare_executor(self):
        p = stale_value_pipeline()
        with self.assertRaises(ValidationError):
            Snapshot(
                pipeline=p,
                statuses={"w": ENDED, "r": "pending"},
                frozen_inputs={},
                published={"v": 1},
                lock_holders={"gcc": "w"},
            )

    def test_running_locked_step_must_own_its_executor(self):
        p = stale_value_pipeline()
        with self.assertRaises(ValidationError):
            Snapshot(
                pipeline=p,
                statuses={"w": RUNNING, "r": "pending"},
                frozen_inputs={"w": ()},
                published={"v": 1},
                lock_holders={},
            )

    def test_snapshot_respects_dependency_statuses(self):
        a = Step("a", (), {(): {"x": 1}})
        b = Step("b", (), {(): {"y": 1}}, deps=("a",))
        p = build_pipeline([a, b])
        with self.assertRaises(ValidationError):
            Snapshot(
                pipeline=p,
                statuses={"a": "pending", "b": RUNNING},
                frozen_inputs={"b": ()},
                published={},
                lock_holders={},
            )

    def test_cannot_replay_a_start_for_a_step_running_at_snapshot(self):
        p = stale_value_pipeline()
        snapshot = Snapshot(
            pipeline=p,
            statuses={"w": RUNNING, "r": "pending"},
            frozen_inputs={"w": ()},
            published={"v": 1},
            lock_holders={"gcc": "w"},
        )
        with self.assertRaises(TraceError):
            replay_snapshot(snapshot, ["start(w)"])


if __name__ == "__main__":
    unittest.main()
