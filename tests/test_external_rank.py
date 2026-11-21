"""Acceptance tests for :mod:`external_rank`.

These tests exercise a synthetic, local-only fixture set.  They assert the
observable contract of the operator:

* descending score ordering with stable ties and empty scores last;
* the skip-M / take-N window plus trailing same-score rows;
* an offset that cuts into a tie group never resurrects skipped rows;
* a trailing tie group spread over several temporary segments yields the same
  row ordinal order as an in-memory sort, for several slot counts and merge
  fan-ins (and therefore several merge pass counts);
* early consumer exit releases every temporary segment;
* invalid configurations are rejected rather than silently clamped.
"""

from __future__ import annotations

import math
import os
import sys
import unittest

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKSPACE not in sys.path:
    sys.path.insert(0, WORKSPACE)

from external_rank import (  # noqa: E402
    ExternalRanker,
    SortStats,
    default_score_key,
    default_is_empty_score,
)

TEMP_ROOT = os.path.join(WORKSPACE, "tests", ".tmp-runs")


def setUpModule() -> None:
    import shutil

    shutil.rmtree(TEMP_ROOT, ignore_errors=True)


def tearDownModule() -> None:
    """Remove the fixture temp root; per-run dirs are already gone."""

    import shutil

    shutil.rmtree(TEMP_ROOT, ignore_errors=True)


def rows(scores, *, prefix="r") -> list[dict]:
    return [{"id": f"{prefix}{i}", "score": score} for i, score in enumerate(scores)]


def is_empty(score) -> bool:
    if score is None:
        return True
    if isinstance(score, float) and math.isnan(score):
        return True
    return isinstance(score, str) and score == ""


def ranked_order(items: list[dict]) -> list[dict]:
    """Reference full ordering with no memory limit."""

    non_empty = [(row["score"], i, row) for i, row in enumerate(items)]
    non_empty = [entry for entry in non_empty if not is_empty(entry[0])]
    empty = [row for row in items if is_empty(row["score"])]
    non_empty.sort(key=lambda entry: (-entry[0], entry[1]))
    return [entry[2] for entry in non_empty] + empty


def reference_window(items: list[dict], offset: int, limit: int) -> list[dict]:
    """Reference window semantics, implemented independently of the operator."""

    ordered = ranked_order(items)
    window = ordered[offset : offset + limit]
    if len(window) < limit:
        return window
    last = window[-1]
    index = offset + limit
    while index < len(ordered):
        candidate = ordered[index]
        if is_empty(last["score"]) != is_empty(candidate["score"]):
            break
        if not is_empty(last["score"]) and candidate["score"] != last["score"]:
            break
        window.append(candidate)
        index += 1
    return window


def run_operator(items, *, slots, fan_in, offset=0, limit=None, **kwargs):
    if limit is None:
        limit = len(items) or 1
    stats = SortStats()
    with ExternalRanker(
        iter(items),
        slots=slots,
        merge_fan_in=fan_in,
        offset=offset,
        limit=limit,
        temp_parent=TEMP_ROOT,
        stats=stats,
        **kwargs,
    ) as result:
        return list(result), stats


def ids(records) -> list[str]:
    return [row["id"] for row in records]


class WindowSemanticsTests(unittest.TestCase):
    def test_skip_and_take_then_extend_tie(self):
        items = rows([10, 9, 9, 9, 8, 7])
        output, _ = run_operator(items, slots=4, fan_in=2, offset=1, limit=1)
        # Skip the 10, take one 9, then extend every remaining 9 and stop.
        self.assertEqual(ids(output), ["r1", "r2", "r3"])

    def test_offset_cutting_into_tie_does_not_restore_skipped_rows(self):
        items = rows([10, 9, 9, 9, 9, 8])
        output, _ = run_operator(items, slots=4, fan_in=2, offset=2, limit=1)
        # r1 is a skipped 9.  It must not come back even though the emitted
        # row r2 has the same score.
        self.assertEqual(ids(output), ["r2", "r3", "r4"])

    def test_limit_cutting_into_tie_extends_but_not_to_next_score(self):
        items = rows([10, 9, 9, 9, 8, 8])
        output, _ = run_operator(items, slots=4, fan_in=2, offset=1, limit=2)
        self.assertEqual(ids(output), ["r1", "r2", "r3"])

    def test_window_larger_than_input_returns_everything(self):
        items = rows([5, 3, 4])
        output, _ = run_operator(items, slots=4, fan_in=2, offset=0, limit=50)
        self.assertEqual(ids(output), ["r0", "r2", "r1"])

    def test_offset_equal_to_input_size_returns_nothing(self):
        items = rows([5, 3, 4])
        output, _ = run_operator(items, slots=4, fan_in=2, offset=3, limit=1)
        self.assertEqual(output, [])

    def test_offset_past_input_size_returns_nothing(self):
        items = rows([5, 3, 4])
        output, _ = run_operator(items, slots=4, fan_in=2, offset=99, limit=1)
        self.assertEqual(output, [])

    def test_empty_input_yields_nothing_and_no_merge(self):
        output, stats = run_operator([], slots=4, fan_in=2)
        self.assertEqual(output, [])
        self.assertEqual(stats.merge_passes, 0)
        self.assertEqual(stats.initial_run_count, 0)


class EmptyScoreTests(unittest.TestCase):
    def test_empty_scores_sort_last_and_keep_input_order(self):
        items = rows([None, 5, "", 7, None, float("nan"), ""])
        output, _ = run_operator(items, slots=4, fan_in=2, offset=0, limit=100)
        self.assertEqual(ids(output), ["r3", "r1", "r0", "r2", "r4", "r5", "r6"])

    def test_all_empty_scores_window_keeps_input_order(self):
        items = rows([None, "", None, "", None])
        output, _ = run_operator(items, slots=4, fan_in=3, offset=1, limit=2)
        self.assertEqual(ids(output), ["r1", "r2", "r3", "r4"])

    def test_limit_landing_on_empty_row_extends_across_all_empties(self):
        items = rows([7, 6, None, "", None, "", None])
        output, _ = run_operator(items, slots=4, fan_in=2, offset=2, limit=1)
        # r0, r1 are skipped; r2 is the first empty and every remaining empty
        # is tied with it, so the whole empty tail is emitted.
        self.assertEqual(ids(output), ["r2", "r3", "r4", "r5", "r6"])

    def test_empty_tie_is_not_extended_by_non_empty_threshold(self):
        items = rows([5, 5, None, None])
        output, _ = run_operator(items, slots=4, fan_in=2, offset=1, limit=1)
        # Threshold is the second 5; only it plus the tied 5 survivor follow.
        self.assertEqual(ids(output), ["r1"])

    def test_custom_score_key_and_empty_predicate(self):
        items = [
            {"id": "a", "points": 3},
            {"id": "b", "points": None},
            {"id": "c", "points": 9},
        ]
        output, _ = run_operator(
            items,
            slots=4,
            fan_in=2,
            limit=2,
            score_key=lambda row: row["points"],
            is_empty_score=lambda value: value is None,
        )
        self.assertEqual(ids(output), ["c", "a"])

    def test_default_helpers(self):
        self.assertEqual(default_score_key({"score": 1}), 1)
        self.assertIsNone(default_score_key({}))
        self.assertTrue(default_is_empty_score(None))
        self.assertTrue(default_is_empty_score(""))
        self.assertTrue(default_is_empty_score(float("nan")))
        self.assertFalse(default_is_empty_score(0))


class ExternalOrderingTests(unittest.TestCase):
    def _trailing_tie_fixture(self):
        # The final 9s alone exceed any slot count used below, so the last
        # tie group is necessarily split across several temp segments.
        scores = [20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10]
        scores += [9] * 17
        scores += [8] * 3
        scores += [None, "", None, ""]
        return rows(scores)

    def test_trailing_tie_matches_in_memory_order_across_configs(self):
        items = self._trailing_tie_fixture()
        expected = reference_window(items, offset=8, limit=12)
        expected_ids = ids(expected)

        configs = [
            (4, 2),
            (4, 3),
            (5, 2),
            (5, 4),
            (6, 2),
            (6, 5),
            (7, 3),
            (8, 7),
        ]
        tie_group_size = 17
        pass_counts = set()
        for slots, fan_in in configs:
            with self.subTest(slots=slots, fan_in=fan_in):
                output, stats = run_operator(
                    list(items), slots=slots, fan_in=fan_in, offset=8, limit=12
                )
                self.assertEqual(ids(output), expected_ids)
                self.assertGreater(stats.initial_run_count, 1)
                self.assertLessEqual(stats.peak_buffered_records, slots)
                for size in stats.initial_run_sizes:
                    self.assertLessEqual(size, slots)
                # No single temp segment can hold the whole trailing 9 group,
                # so that group really is spread across several segments.
                self.assertLess(max(stats.initial_run_sizes), tie_group_size)
                pass_counts.add(stats.merge_passes)

        self.assertGreater(len(pass_counts), 1)

    def test_tie_group_split_across_segments_still_stable(self):
        scores = [5] * 13 + [4] * 5
        items = rows(scores)
        expected = ids(reference_window(items, offset=0, limit=3))
        observed = ids(
            run_operator(items, slots=4, fan_in=2, offset=0, limit=3)[0]
        )
        self.assertEqual(observed, expected)
        # limit 3 lands inside the 13-way tie: all 13 rows are emitted.
        self.assertEqual(len(observed), 13)
        self.assertEqual(observed, [f"r{i}" for i in range(13)])

    def test_offset_and_limit_both_land_inside_cross_segment_tie(self):
        scores = [9] * 9 + [8] * 9 + [None] * 4
        items = rows(scores)
        for slots, fan_in in ((4, 2), (4, 3), (6, 2), (6, 5)):
            with self.subTest(slots=slots, fan_in=fan_in):
                expected = ids(reference_window(items, offset=2, limit=4))
                observed = ids(
                    run_operator(
                        list(items), slots=slots, fan_in=fan_in, offset=2, limit=4
                    )[0]
                )
                self.assertEqual(observed, expected)
                # r0 and r1 are skipped 9s and must not reappear.  The window
                # takes r2..r5, then extends over the rest of the 9 tie group
                # (r6..r8) and stops before the first 8.
                self.assertNotIn("r0", observed)
                self.assertNotIn("r1", observed)
                self.assertEqual(observed, [f"r{i}" for i in range(2, 9)])
                self.assertEqual(len(observed), 7)

    def test_many_rows_multiple_merge_passes(self):
        scores = [100 - (i % 7) for i in range(120)]
        items = rows(scores)
        expected = ids(reference_window(items, offset=33, limit=9))
        output, stats = run_operator(
            list(items), slots=4, fan_in=2, offset=33, limit=9
        )
        self.assertEqual(ids(output), expected)
        self.assertGreater(stats.merge_passes, 3)
        self.assertGreaterEqual(stats.segments_written, stats.initial_run_count)

    def test_duplicate_extremes_stable(self):
        scores = [10, 10, 10, 10, 1, 1, 1, 1]
        items = rows(scores)
        ordered = [row["id"] for row in ranked_order(items)]
        self.assertEqual(ordered, [f"r{i}" for i in range(8)])
        output, _ = run_operator(items, slots=4, fan_in=2, offset=0, limit=100)
        self.assertEqual(ids(output), ordered)

    def test_deterministic_fuzz_matches_reference(self):
        import random

        rng = random.Random(20261121)
        pool = [10, 9, 8, 7, 6, 5, None, "", float("nan")]

        for case in range(12):
            size = rng.randint(0, 60)
            scores = [rng.choice(pool) for _ in range(size)]
            # Keep NaN out of comparisons by mapping it to empty in the pool
            # membership check above; reference handles it explicitly.
            items = rows(scores)
            offset = rng.randint(0, max(size, 1) + 2)
            limit = rng.randint(1, 15)
            for slots, fan_in in ((4, 2), (5, 3), (8, 2), (8, 7)):
                with self.subTest(case=case, slots=slots, fan_in=fan_in):
                    expected = ids(reference_window(items, offset, limit))
                    observed = ids(
                        run_operator(
                            list(items),
                            slots=slots,
                            fan_in=fan_in,
                            offset=offset,
                            limit=limit,
                        )[0]
                    )
                    self.assertEqual(observed, expected)


class OneShotInputTests(unittest.TestCase):
    def test_input_iterable_is_consumed_exactly_once(self):
        class OneShot:
            def __init__(self, items):
                self._items = items
                self.iter_calls = 0
                self.next_calls = 0
                self._used = False

            def __iter__(self):
                self.iter_calls += 1
                if self.iter_calls > 1:
                    raise AssertionError("input was iterated more than once")
                return self

            def __next__(self):
                if self._used:
                    raise StopIteration
                self.next_calls += 1
                if not self._items:
                    self._used = True
                    raise StopIteration
                return self._items.pop(0)

        source = OneShot(rows([3, 1, 2, 2]))
        result = ExternalRanker(
            source,
            slots=4,
            merge_fan_in=2,
            offset=0,
            limit=10,
            temp_parent=TEMP_ROOT,
        )
        # Construction must not consume the input.
        self.assertEqual(source.iter_calls, 1)
        self.assertEqual(source.next_calls, 0)
        output = list(result)
        self.assertEqual(ids(output), ["r0", "r2", "r3", "r1"])
        self.assertEqual(source.iter_calls, 1)
        self.assertEqual(source.next_calls, 5)

    def test_generator_input_is_used_once(self):
        consumed = []

        def source():
            for index, score in enumerate([4, 2, 3, 3]):
                consumed.append(index)
                yield {"id": f"r{index}", "score": score}

        output, _ = run_operator(source(), slots=4, fan_in=2, offset=0, limit=3)
        self.assertEqual(ids(output), ["r0", "r2", "r3"])
        self.assertEqual(consumed, [0, 1, 2, 3])


class TempSegmentLifecycleTests(unittest.TestCase):
    def _segments(self, directory):
        if not os.path.isdir(directory):
            return []
        return sorted(os.listdir(directory))

    def test_natural_exhaustion_removes_temp_directory(self):
        items = rows([i % 5 for i in range(40)])
        stats = SortStats()
        result = ExternalRanker(
            iter(items),
            slots=4,
            merge_fan_in=2,
            offset=0,
            limit=100,
            temp_parent=TEMP_ROOT,
            stats=stats,
        )
        list(result)
        self.assertIsNotNone(stats.temp_dir)
        self.assertFalse(os.path.exists(stats.temp_dir))

    def test_early_break_with_context_manager_removes_every_segment(self):
        items = rows([i % 5 for i in range(40)])
        stats = SortStats()
        seen = []
        with ExternalRanker(
            iter(items),
            slots=4,
            merge_fan_in=2,
            offset=0,
            limit=100,
            temp_parent=TEMP_ROOT,
            stats=stats,
        ) as result:
            for row in result:
                seen.append(row["id"])
                self.assertTrue(os.path.isdir(stats.temp_dir))
                break
            # Segments are on disk while the window is being streamed.
            self.assertGreater(len(self._segments(stats.temp_dir)), 0)
        self.assertEqual(len(seen), 1)
        self.assertFalse(os.path.exists(stats.temp_dir))

    def test_early_break_without_context_manager_requires_close(self):
        items = rows([i % 5 for i in range(40)])
        stats = SortStats()
        result = ExternalRanker(
            iter(items),
            slots=4,
            merge_fan_in=2,
            offset=0,
            limit=100,
            temp_parent=TEMP_ROOT,
            stats=stats,
        )
        next(result)
        temp_dir = stats.temp_dir
        self.assertTrue(os.path.isdir(temp_dir))
        result.close()
        self.assertFalse(os.path.exists(temp_dir))
        result.close()  # idempotent
        self.assertEqual(list(result), [])

    def test_closed_result_is_empty_iterator(self):
        result = ExternalRanker(
            iter(rows([1, 2])),
            slots=4,
            merge_fan_in=2,
            offset=0,
            limit=2,
            temp_parent=TEMP_ROOT,
        )
        result.close()
        self.assertEqual(list(result), [])

    def test_dropping_reference_releases_segments(self):
        import gc

        stats = SortStats()
        result = ExternalRanker(
            iter(rows([i % 4 for i in range(30)])),
            slots=4,
            merge_fan_in=2,
            offset=0,
            limit=100,
            temp_parent=TEMP_ROOT,
            stats=stats,
        )
        next(result)
        temp_dir = stats.temp_dir
        self.assertTrue(os.path.isdir(temp_dir))
        del result
        gc.collect()
        self.assertFalse(os.path.exists(temp_dir))

    def test_consumer_break_then_close_after_partial_read(self):
        stats = SortStats()
        result = ExternalRanker(
            iter(rows([i % 4 for i in range(30)])),
            slots=4,
            merge_fan_in=2,
            offset=0,
            limit=100,
            temp_parent=TEMP_ROOT,
            stats=stats,
        )
        iterator = iter(result)
        first = next(iterator)
        self.assertIsNotNone(first)
        self.assertTrue(os.path.isdir(stats.temp_dir))
        result.close()
        self.assertFalse(os.path.exists(stats.temp_dir))
        self.assertEqual(list(iterator), [])

    def test_temp_parent_is_created_on_demand(self):
        parent = os.path.join(TEMP_ROOT, "nested", "deeper")
        items = rows(list(range(12)))
        with ExternalRanker(
            iter(items),
            slots=4,
            merge_fan_in=2,
            offset=0,
            limit=3,
            temp_parent=parent,
        ) as result:
            got = [row["id"] for row in result]
        self.assertEqual(got, ["r11", "r10", "r9"])
        self.assertTrue(os.path.isdir(parent))


class RejectionTests(unittest.TestCase):
    def build(self, **overrides):
        params = dict(
            slots=4,
            merge_fan_in=2,
            offset=0,
            limit=1,
            temp_parent=TEMP_ROOT,
        )
        params.update(overrides)
        return ExternalRanker(iter(rows([1, 2, 3])), **params)

    def test_slots_below_minimum_rejected(self):
        with self.assertRaises(ValueError):
            self.build(slots=3)

    def test_fan_in_below_two_rejected(self):
        with self.assertRaises(ValueError):
            self.build(merge_fan_in=1)

    def test_fan_in_equal_to_slots_rejected(self):
        with self.assertRaises(ValueError):
            self.build(slots=4, merge_fan_in=4)

    def test_fan_in_above_slots_rejected(self):
        with self.assertRaises(ValueError):
            self.build(slots=5, merge_fan_in=9)

    def test_limit_must_be_positive(self):
        with self.assertRaises(ValueError):
            self.build(limit=0)
        with self.assertRaises(ValueError):
            self.build(limit=-3)

    def test_offset_must_be_non_negative(self):
        with self.assertRaises(ValueError):
            self.build(offset=-1)

    def test_non_integer_parameters_rejected(self):
        with self.assertRaises(TypeError):
            self.build(slots=4.0)
        with self.assertRaises(TypeError):
            self.build(merge_fan_in=True)
        with self.assertRaises(TypeError):
            self.build(offset="1")
        with self.assertRaises(TypeError):
            self.build(limit=2.5)

    def test_non_callable_hooks_rejected(self):
        with self.assertRaises(TypeError):
            self.build(score_key=5)
        with self.assertRaises(TypeError):
            self.build(is_empty_score="nope")

    def test_boundary_values_accepted(self):
        for slots, fan_in in ((4, 2), (4, 3), (9, 8), (2 + 2, 2 + 1)):
            with self.subTest(slots=slots, fan_in=fan_in):
                output, _ = run_operator(
                    rows([1, 2, 3]), slots=slots, fan_in=fan_in, offset=0, limit=1
                )
                self.assertEqual(ids(output), ["r2"])


if __name__ == "__main__":
    unittest.main()
