import unittest

import _bootstrap  # noqa: F401  (workspace-root path bootstrap)

from build_semantics import (
    TraceError,
    analyze,
    build_pipeline,
    event_strings,
    pipeline_from_dict,
    replay,
    snapshot_from_dict,
)
from build_semantics.model import ENDED, RUNNING, parse_event


class ReplayLegalityTests(unittest.TestCase):
    def _pipeline(self):
        a = __import__("build_semantics").Step("a", (), {(): {"x": 1}})
        b = __import__("build_semantics").Step("b", ("x",),
                                               {(1,): {"y": 2}}, deps=("a",))
        return build_pipeline([a, b], outputs_of_interest=["y"])

    def test_complete_legal_trace_replays_to_completion(self):
        p = self._pipeline()
        outcome = replay(p, ["start(a)", "end(a)", "start(b)", "end(b)"])
        self.assertEqual(outcome.statuses, (ENDED, ENDED))
        self.assertEqual(outcome.outputs, (2,))

    def test_partial_trace_reports_running_status(self):
        p = self._pipeline()
        outcome = replay(p, ["start(a)"])
        self.assertEqual(outcome.statuses, (RUNNING, "pending"))
        self.assertEqual(outcome.trace, ("start(a)",))

    def test_ending_a_step_that_never_started_is_rejected(self):
        p = self._pipeline()
        with self.assertRaises(TraceError):
            replay(p, ["end(a)"])

    def test_starting_before_dependency_ended_is_rejected(self):
        p = self._pipeline()
        with self.assertRaises(TraceError):
            replay(p, ["start(a)", "start(b)"])

    def test_malformed_event_string_is_rejected(self):
        p = self._pipeline()
        with self.assertRaises(TraceError):
            replay(p, ["begin(a)"])
        with self.assertRaises(TraceError):
            replay(p, ["start(ghost)"])

    def test_parse_event_roundtrip(self):
        p = self._pipeline()
        event = parse_event("end(b)", p)
        self.assertEqual(event_strings([event], p), ["end(b)"])

    def test_witness_traces_from_analyze_are_replayable(self):
        from build_semantics import Step, UNSET

        w1 = Step("w1", (), {(): {"v": 1}})
        w2 = Step("w2", (), {(): {"v": 2}})
        r = Step("r", ("v",),
                 {(1,): {"out": "a"}, (2,): {"out": "b"},
                  (UNSET,): {"out": "c"}})
        p = build_pipeline([w1, w2, r], outputs_of_interest=["out"])
        result = analyze(p)
        outputs = []
        for trace in result.traces:
            outcome = replay(p, trace)
            self.assertEqual(outcome.statuses, (ENDED, ENDED, ENDED))
            outputs.append(outcome.outputs)
        self.assertNotEqual(outputs[0], outputs[1])
        self.assertEqual(set(outputs), set(result.witness_outputs))


class LoaderTests(unittest.TestCase):
    SPEC = {
        "initial_values": {"x": 0},
        "outputs_of_interest": ["z"],
        "steps": [
            {
                "name": "a",
                "reads": ["x"],
                "deps": [],
                "lock": "gcc",
                "table": [
                    {"when": [0], "publish": {"x": 1, "tag": "one"}},
                    {"when": [1], "publish": {"x": 2, "tag": "two"}},
                ],
            },
            {
                "name": "b",
                "reads": ["x"],
                "deps": ["a"],
                "table": [
                    {"when": [1], "publish": {"z": 9}},
                    {"when": [{"$unset": True}], "publish": {"z": 0}},
                ],
            },
        ],
    }

    def test_pipeline_from_dict_runs_end_to_end(self):
        p = pipeline_from_dict(self.SPEC)
        result = analyze(p)
        self.assertTrue(result.deterministic)
        self.assertEqual(result.final_outputs, (9,))

    def test_pipeline_round_trips_through_json_file(self):
        import json
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pipeline.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self.SPEC, handle)
            p = __import__("build_semantics").pipeline_from_file(path)
        self.assertEqual([s.name for s in p.steps], ["a", "b"])
        self.assertEqual(p.outputs_of_interest, ("z",))

    def test_unset_marker_decodes_to_sentinel(self):
        from build_semantics import UNSET

        spec = {
            "steps": [
                {
                    "name": "r",
                    "reads": ["missing"],
                    "table": [
                        {"when": [{"$unset": True}],
                         "publish": {"seen": "unset"}}
                    ],
                }
            ],
            "outputs_of_interest": ["seen"],
        }
        p = pipeline_from_dict(spec)
        result = analyze(p)
        self.assertTrue(result.deterministic)
        self.assertEqual(result.final_outputs, ("unset",))
        self.assertIn((UNSET,), p.step("r").table)

    def test_snapshot_from_dict_resumes_analysis(self):
        p = pipeline_from_dict(self.SPEC)
        snap_spec = {
            "statuses": {"a": "running", "b": "pending"},
            "frozen_inputs": {"a": [0]},
            "published": {"x": 0},
            "lock_holders": {"gcc": "a"},
        }
        snapshot = snapshot_from_dict(p, snap_spec)
        result = __import__("build_semantics").analyze_snapshot(snapshot)
        self.assertTrue(result.deterministic)
        self.assertEqual(result.final_outputs, (9,))

    def test_duplicate_table_entry_is_rejected(self):
        from build_semantics import ValidationError

        spec = {
            "steps": [
                {
                    "name": "a",
                    "reads": ["x"],
                    "table": [
                        {"when": [0], "publish": {"x": 1}},
                        {"when": [0], "publish": {"x": 2}},
                    ],
                }
            ]
        }
        with self.assertRaises(ValidationError):
            pipeline_from_dict(spec)

    def test_bad_spec_shapes_are_rejected(self):
        from build_semantics import ValidationError

        with self.assertRaises(ValidationError):
            pipeline_from_dict({"steps": []})
        with self.assertRaises(ValidationError):
            pipeline_from_dict({"steps": [{"name": "a"}]})


if __name__ == "__main__":
    unittest.main()
