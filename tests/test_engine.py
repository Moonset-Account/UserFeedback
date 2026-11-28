import _bootstrap  # noqa: F401

import unittest

from attribution_migration.engine import evaluate_all, evaluate_conclusion
from attribution_migration.model import (
    ConclusionSpec,
    Observation,
    Requirement,
)


def make_obs(obs_id, fact, source, level):
    return Observation.from_dict(
        {
            "obs_id": obs_id,
            "fact": fact,
            "source_id": source,
            "evidence_level": level,
            "valid_from": "2026-03-01T00:00:00Z",
            "valid_to": None,
        }
    )


def spec(spec_id, requires):
    return ConclusionSpec(
        conclusion_id=spec_id, label=spec_id, requires=tuple(requires)
    )


def r(fact, level="confirmed", independent=False):
    return Requirement(fact=fact, min_level=level, independent_source=independent)


class TestEngine(unittest.TestCase):
    def test_threshold_is_inclusive(self):
        observations = [make_obs("o1", "f1", "s1", "confirmed")]
        evaluation = evaluate_conclusion(spec("c1", [r("f1", "confirmed")]), observations)
        self.assertTrue(evaluation.satisfied)
        self.assertEqual(evaluation.used_obs_ids, ("o1",))

    def test_rank_order_weak_to_strong(self):
        # rumor < initial < preliminary < confirmed < official
        observations = [make_obs("o1", "f1", "s1", "preliminary")]
        ok = evaluate_conclusion(spec("c1", [r("f1", "preliminary")]), observations)
        self.assertTrue(ok.satisfied)
        fail = evaluate_conclusion(spec("c2", [r("f1", "confirmed")]), observations)
        self.assertFalse(fail.satisfied)
        self.assertEqual(fail.deficits[0].reason, "below_threshold")
        # 等级不足时仍把最接近门槛的观测带回复核线索
        self.assertEqual(fail.deficits[0].candidate_obs_ids, ("o1",))

    def test_backtracking_finds_valid_source_assignment(self):
        # f1: s1/s2 都有；f2: 只有 s2。正确分配必须 f1->s1, f2->s2
        observations = [
            make_obs("o1", "f1", "s1", "confirmed"),
            make_obs("o2", "f1", "s2", "confirmed"),
            make_obs("o3", "f2", "s2", "confirmed"),
        ]
        specs = [
            spec("c1", [r("f1", independent=True)]),
            spec("c2", [r("f2", independent=True)]),
        ]
        evals = evaluate_all(specs, observations)
        self.assertTrue(all(e.satisfied for e in evals.values()))
        self.assertEqual(evals["c1"].used_obs_ids, ("o1",))
        self.assertEqual(evals["c2"].used_obs_ids, ("o3",))

    def test_unsolvable_independence_reports_conflict(self):
        observations = [
            make_obs("o1", "f1", "s1", "confirmed"),
            make_obs("o2", "f2", "s1", "confirmed"),
        ]
        specs = [
            spec("c1", [r("f1", independent=True)]),
            spec("c2", [r("f2", independent=True)]),
        ]
        evals = evaluate_all(specs, observations)
        self.assertFalse(any(e.satisfied for e in evals.values()))
        for evaluation in evals.values():
            self.assertEqual(
                evaluation.deficits[0].reason, "lacks_independent_source"
            )

    def test_non_independent_requirements_ignore_source_sharing(self):
        observations = [
            make_obs("o1", "f1", "s1", "confirmed"),
            make_obs("o2", "f2", "s1", "confirmed"),
        ]
        specs = [spec("c1", [r("f1"), r("f2")])]
        evals = evaluate_all(specs, observations)
        self.assertTrue(evals["c1"].satisfied)

    def test_highest_level_observation_preferred(self):
        observations = [
            make_obs("low", "f1", "s1", "confirmed"),
            make_obs("high", "f1", "s2", "official"),
        ]
        evaluation = evaluate_conclusion(spec("c1", [r("f1")]), observations)
        self.assertEqual(evaluation.used_obs_ids, ("high",))

    def test_missing_fact_reports_below_threshold_without_candidates(self):
        observations = [make_obs("o1", "other", "s1", "official")]
        evaluation = evaluate_conclusion(spec("c1", [r("f1")]), observations)
        self.assertFalse(evaluation.satisfied)
        self.assertEqual(evaluation.deficits[0].candidate_obs_ids, ())


if __name__ == "__main__":
    unittest.main()
