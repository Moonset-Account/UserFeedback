"""Tests for the demand-driven row expander.

Run with::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import random
import unittest

from row_expand import (
    PADDED_ORDINAL,
    Cursor,
    CursorError,
    Level,
    RowExpander,
    Snapshot,
)


# ---------------------------------------------------------------------------
# Independent, eager reference model
# ---------------------------------------------------------------------------
def _eager_items(level: Level, row, bindings):
    if isinstance(level.source, str):
        items = row[level.source]
    else:
        items = level.source(row, bindings)
    return items


def eager_expand(expander: RowExpander, rows):
    """Materialise the full expansion with plain recursion (reference model)."""
    out = []

    def walk(ri, row, base, k, bindings):
        if k == len(expander.levels):
            record = dict(base)
            for level in expander.levels:
                record[level.name] = bindings[level.name]
                pos = bindings["__pos__"][level.name]
                if pos < 0:
                    record[level.ordinal_name] = PADDED_ORDINAL
                else:
                    record[level.ordinal_name] = level.ordinal_start + pos
            out.append(record)
            return
        level = expander.levels[k]
        items = _eager_items(level, row, bindings)
        materialised = [] if items is None else list(items)
        if not materialised:
            if level.mode == "inner":
                return
            bindings[level.name] = None
            bindings["__pos__"][level.name] = -1
            walk(ri, row, base, k + 1, bindings)
            return
        for i, value in enumerate(materialised):
            bindings[level.name] = value
            bindings["__pos__"][level.name] = i
            walk(ri, row, base, k + 1, bindings)

    for ri, row in enumerate(rows):
        base = dict(row)
        walk(ri, row, base, 0, {"__pos__": {}})
    return out


def register_builders():
    """Return a dict of {name: (levels, rows)} fixtures sharing semantics."""

    def b_of_a(row, bindings):
        a = bindings.get("a")
        if a is None:
            return None
        if a < 0:
            return []
        return range(a)

    def c_of_ab(row, bindings):
        a, b = bindings.get("a"), bindings.get("b")
        if a is None or b is None:
            return None
        return [f"{a}:{b}:{i}" for i in range((a + b) % 3)]

    fixtures = {
        "single_inner": (
            [Level("a", "xs")],
            [
                {"id": 1, "xs": [10, 20]},
                {"id": 2, "xs": []},
                {"id": 3, "xs": None},
                {"id": 4, "xs": [None, 10]},
            ],
        ),
        "single_outer": (
            [Level("a", "xs", mode="outer")],
            [
                {"id": 1, "xs": [10, 20]},
                {"id": 2, "xs": []},
                {"id": 3, "xs": None},
                {"id": 4, "xs": [None]},
            ],
        ),
        "lateral_chain": (
            [
                Level("a", "xs"),
                Level("b", b_of_a, mode="inner"),
                Level("c", c_of_ab, mode="inner"),
            ],
            [
                {"id": 1, "xs": [0, 1, 2]},
                {"id": 2, "xs": [3, -1]},
                {"id": 3, "xs": []},
            ],
        ),
        "mixed_modes": (
            [
                Level("a", "xs", mode="outer"),
                Level("b", b_of_a, mode="inner"),
                Level("c", c_of_ab, mode="outer"),
            ],
            [
                {"id": 1, "xs": [0, 1, 2]},
                {"id": 2, "xs": [3, -1]},
                {"id": 3, "xs": None},
                {"id": 4, "xs": [None, 2]},
            ],
        ),
        "all_outer": (
            [
                Level("a", "xs", mode="outer"),
                Level("b", b_of_a, mode="outer"),
                Level("c", c_of_ab, mode="outer"),
            ],
            [
                {"id": 1, "xs": [0, 1]},
                {"id": 2, "xs": []},
                {"id": 3, "xs": None},
                {"id": 4, "xs": [None]},
            ],
        ),
        "custom_ordinals": (
            [
                Level("a", "xs", mode="outer", ordinal_name="a_no", ordinal_start=0),
                Level(
                    "b",
                    b_of_a,
                    mode="outer",
                    ordinal_name="b_no",
                    ordinal_start=100,
                ),
            ],
            [
                {"id": 1, "xs": [0, 1]},
                {"id": 2, "xs": []},
            ],
        ),
    }
    return fixtures


# ---------------------------------------------------------------------------
# Basic semantics
# ---------------------------------------------------------------------------
class TestBasicSemantics(unittest.TestCase):
    def test_inner_drops_empty_and_null(self):
        exp = RowExpander([Level("a", "xs")])
        rows = [
            {"id": 1, "xs": [10, 20]},
            {"id": 2, "xs": []},
            {"id": 3, "xs": None},
        ]
        got = list(exp.expand(rows))
        self.assertEqual([(r["id"], r["a"], r["a_ordinal"]) for r in got],
                         [(1, 10, 1), (1, 20, 2)])

    def test_outer_keeps_one_padded_row(self):
        exp = RowExpander([Level("a", "xs", mode="outer")])
        rows = [{"id": 2, "xs": []}, {"id": 3, "xs": None}]
        got = list(exp.expand(rows))
        self.assertEqual(got, [
            {"id": 2, "xs": [], "a": None, "a_ordinal": None},
            {"id": 3, "xs": None, "a": None, "a_ordinal": None},
        ])

    def test_real_none_element_is_distinct_from_padded(self):
        exp = RowExpander([Level("a", "xs", mode="outer")])
        rows = [{"id": 1, "xs": [None]}]
        (row,) = list(exp.expand(rows))
        self.assertIsNone(row["a"])
        self.assertEqual(row["a_ordinal"], 1)  # real element -> integer ordinal
        self.assertIsNot(row["a_ordinal"], PADDED_ORDINAL)

    def test_equal_values_at_two_positions_are_independent(self):
        exp = RowExpander([Level("a", "xs")])
        got = list(exp.expand([{"id": 1, "xs": [7, 7, 7]}]))
        self.assertEqual([r["a"] for r in got], [7, 7, 7])
        self.assertEqual([r["a_ordinal"] for r in got], [1, 2, 3])

    def test_equal_outer_values_expand_independently(self):
        # Two equal ``a`` values must each drive a fresh inner expansion, in
        # outer-major order, with their own ordinals (no shared iterator).
        calls = {"n": 0}

        def inner(row, bindings):
            calls["n"] += 1
            n = bindings["a"]
            return [f"child{n}-{i}" for i in range(n)]

        exp = RowExpander([Level("a", "xs"), Level("b", inner)])
        got = list(exp.expand([{"id": 1, "xs": [2, 2]}]))
        self.assertEqual(calls["n"], 2)  # invoked once per occurrence, not merged
        self.assertEqual(
            [(r["a"], r["a_ordinal"], r["b"], r["b_ordinal"]) for r in got],
            [
                (2, 1, "child2-0", 1),
                (2, 1, "child2-1", 2),
                (2, 2, "child2-0", 1),
                (2, 2, "child2-1", 2),
            ],
        )

    def test_equal_values_interleaved_across_input_rows(self):
        exp = RowExpander([
            Level("a", "xs"),
            Level("b", lambda row, b: [f"{row['id']}:{b['a']}"]),
        ])
        rows = [{"id": 1, "xs": [5]}, {"id": 2, "xs": [5]}]
        got = list(exp.expand(rows))
        self.assertEqual([(r["id"], r["a"], r["b"]) for r in got],
                         [(1, 5, "1:5"), (2, 5, "2:5")])

    def test_lateral_generator_reads_previous_current_element(self):
        exp = RowExpander([
            Level("a", "xs"),
            Level("b", lambda row, b: [b["a"], b["a"] * 10]),
        ])
        got = list(exp.expand([{"id": 1, "xs": [1, 2]}]))
        self.assertEqual(
            [(r["a"], r["a_ordinal"], r["b"], r["b_ordinal"]) for r in got],
            [
                (1, 1, 1, 1),
                (1, 1, 10, 2),
                (2, 2, 2, 1),
                (2, 2, 20, 2),
            ],
        )

    def test_input_columns_are_preserved(self):
        exp = RowExpander([Level("a", "xs")])
        (row,) = list(exp.expand([{"id": 9, "tag": "t", "xs": [1]}]))
        self.assertEqual(row["id"], 9)
        self.assertEqual(row["tag"], "t")
        self.assertEqual(row["a"], 1)

    def test_multiple_input_rows_concatenate(self):
        exp = RowExpander([Level("a", "xs")])
        got = list(exp.expand([{"id": 1, "xs": [1, 2]}, {"id": 2, "xs": [3]}]))
        self.assertEqual([(r["id"], r["a"]) for r in got],
                         [(1, 1), (1, 2), (2, 3)])

    def test_empty_input(self):
        exp = RowExpander([Level("a", "xs")])
        self.assertEqual(list(exp.expand([])), [])

    def test_deeper_level_still_expands_from_outer_padded_binding(self):
        # a is padded (None); b source can still produce rows from that binding.
        exp = RowExpander([
            Level("a", "xs", mode="outer"),
            Level("b", lambda row, b: ["pad-child", "second"], mode="inner"),
        ])
        got = list(exp.expand([{"id": 1, "xs": []}]))
        self.assertEqual(
            [(r["a"], r["a_ordinal"], r["b"], r["b_ordinal"]) for r in got],
            [(None, None, "pad-child", 1), (None, None, "second", 2)],
        )

    def test_outer_padded_parent_with_inner_empty_child_drops_branch(self):
        exp = RowExpander([
            Level("a", "xs", mode="outer"),
            Level("b", lambda row, b: [], mode="inner"),
        ])
        self.assertEqual(list(exp.expand([{"id": 1, "xs": []}])), [])

    def test_str_and_bytes_are_not_lists(self):
        exp = RowExpander([Level("a", "xs")])
        with self.assertRaises(TypeError):
            list(exp.expand([{"id": 1, "xs": "abc"}]))


# ---------------------------------------------------------------------------
# Cross-check against the eager reference model
# ---------------------------------------------------------------------------
class TestAgainstReferenceModel(unittest.TestCase):
    def test_all_fixtures_match_eager_model(self):
        for name, (levels, rows) in register_builders().items():
            with self.subTest(fixture=name):
                exp = RowExpander(levels)
                expected = eager_expand(exp, rows)
                actual = list(exp.expand(rows))
                self.assertEqual(actual, expected)
                # Sanity: the reference model actually produced something.
                self.assertGreater(len(expected), 0)


class TestRandomizedDifferential(unittest.TestCase):
    """Random level trees cross-checked against the eager reference model."""

    def _random_spec(self, rng):
        n_levels = rng.randint(1, 4)
        levels = []
        for i in range(n_levels):
            mode = rng.choice(["inner", "outer"])
            if i == 0:
                source = "xs"
            else:
                prev = levels[-1].name

                def make(prev=prev):
                    def src(row, bindings, _prev=prev):
                        parent = bindings[_prev]
                        if parent is None:
                            return rng.choice([None, []])
                        fan = (parent if isinstance(parent, int) else len(str(parent))) % 3
                        return [f"{_prev}{parent}#{j}" for j in range(fan)]
                    return src

                source = make()
            levels.append(Level(f"l{i}", source, mode=mode))
        return levels

    def _random_rows(self, rng):
        rows = []
        for rid in range(rng.randint(0, 5)):
            choice = rng.random()
            if choice < 0.15:
                xs = None
            elif choice < 0.3:
                xs = []
            else:
                xs = [rng.choice([0, 1, 2, None]) for _ in range(rng.randint(0, 4))]
            rows.append({"id": rid, "xs": xs})
        return rows

    def test_random_trees_match_and_resume(self):
        rng = random.Random(20261128)
        total_rows = 0
        for trial in range(60):
            levels = self._random_spec(rng)
            rows = self._random_rows(rng)
            exp = RowExpander(levels)
            with self.subTest(trial=trial):
                full = eager_expand(exp, rows)
                actual = list(exp.expand(rows))
                self.assertEqual(actual, full)
                total_rows += len(full)

                # Every output boundary must be a valid resume point.
                for cut in range(len(full) + 1):
                    head, cur = [], exp.expand(rows)
                    while len(head) < cut:
                        head.append(cur.next_row())
                    tail = list(exp.expand(rows, cur.cursor()))
                    self.assertEqual(head + tail, full,
                                     msg=f"trial={trial} cut={cut}")
        # Ensure the fuzzer exercised non-trivial expansions.
        self.assertGreater(total_rows, 100)


# ---------------------------------------------------------------------------
# Pulling in fixed-size batches
# ---------------------------------------------------------------------------
class TestBatching(unittest.TestCase):
    def _all(self, exp, rows):
        return list(exp.expand(rows))

    def test_batches_reassemble_full_stream(self):
        exp = RowExpander([
            Level("a", "xs"),
            Level("b", lambda row, b: range(b["a"])),
        ])
        rows = [{"id": i, "xs": [0, 1, 2, 3]} for i in range(3)]
        full = self._all(exp, rows)
        cur = exp.expand(rows)
        collected = []
        while True:
            batch = cur.next_batch(5)
            if not batch:
                break
            self.assertLessEqual(len(batch), 5)
            collected.extend(batch)
        self.assertEqual(collected, full)
        self.assertEqual(len(full), 3 * (0 + 1 + 2 + 3))

    def test_batch_larger_than_stream(self):
        exp = RowExpander([Level("a", "xs")])
        cur = exp.expand([{"id": 1, "xs": [1, 2]}])
        self.assertEqual(len(cur.next_batch(100)), 2)
        self.assertEqual(cur.next_batch(100), [])

    def test_zero_sized_batch(self):
        exp = RowExpander([Level("a", "xs")])
        cur = exp.expand([{"id": 1, "xs": [1, 2]}])
        self.assertEqual(cur.next_batch(0), [])
        self.assertEqual(len(cur.next_batch(1)), 1)

    def test_negative_batch_rejected(self):
        exp = RowExpander([Level("a", "xs")])
        cur = exp.expand([{"id": 1, "xs": [1]}])
        with self.assertRaises(ValueError):
            cur.next_batch(-1)

    def test_iter_protocol(self):
        exp = RowExpander([Level("a", "xs")])
        cur = exp.expand([{"id": 1, "xs": [1, 2, 3]}])
        self.assertEqual([r["a"] for r in cur], [1, 2, 3])

    def test_next_row_after_exhaustion_raises(self):
        exp = RowExpander([Level("a", "xs")])
        cur = exp.expand([{"id": 1, "xs": [1]}])
        cur.next_row()
        with self.assertRaises(StopIteration):
            cur.next_row()
        with self.assertRaises(StopIteration):
            cur.next_row()


# ---------------------------------------------------------------------------
# Cursor save / resume at every boundary
# ---------------------------------------------------------------------------
class TestCursorResume(unittest.TestCase):
    def _fixtures(self):
        return {
            "lateral": RowExpander([
                Level("a", "xs"),
                Level("b", lambda row, b: [] if b["a"] is None else range(b["a"]),
                      mode="inner"),
            ]),
            "mixed": RowExpander([
                Level("a", "xs", mode="outer"),
                Level("b", lambda row, b: (None if b["a"] is None else range(b["a"])),
                      mode="outer"),
            ]),
        }

    def _rows(self):
        return [
            {"id": 1, "xs": [2, 0, 1]},
            {"id": 2, "xs": []},
            {"id": 3, "xs": [None, 3]},
            {"id": 4, "xs": [1]},
        ]

    def test_resume_at_every_output_boundary(self):
        for name, exp in self._fixtures().items():
            with self.subTest(fixture=name):
                rows = self._rows()
                full = list(exp.expand(rows))
                self.assertGreater(len(full), 1)
                for cut in range(len(full) + 1):
                    head: list = []
                    cur = exp.expand(rows)
                    while len(head) < cut:
                        head.append(cur.next_row())
                    saved = cur.cursor()
                    resumed = exp.expand(rows, saved)
                    tail = list(resumed)
                    self.assertEqual(head + tail, full,
                                     msg=f"cut={cut} fixture={name}")

    def test_resume_with_batches_matches_full(self):
        exp = self._fixtures()["lateral"]
        rows = self._rows()
        full = list(exp.expand(rows))
        collected = []
        cur = exp.expand(rows)
        while True:
            batch = cur.next_batch(2)
            if not batch:
                break
            collected.extend(batch)
        self.assertEqual(collected, full)

    def test_cursor_at_start_resumes_full_stream(self):
        exp = self._fixtures()["lateral"]
        rows = self._rows()
        full = list(exp.expand(rows))
        cur = exp.expand(rows)
        start = cur.cursor()
        self.assertIsNone(start.positions)
        self.assertFalse(start.done)
        self.assertEqual(list(exp.expand(rows, start)), full)

    def test_cursor_at_end_resumes_to_nothing(self):
        exp = self._fixtures()["lateral"]
        rows = self._rows()
        cur = exp.expand(rows)
        list(cur)
        end = cur.cursor()
        self.assertTrue(end.done)
        self.assertEqual(list(exp.expand(rows, end)), [])

    def test_resume_mid_input_row(self):
        exp = RowExpander([
            Level("a", "xs"),
            Level("b", lambda row, b: range(b["a"])),
        ])
        rows = [{"id": 1, "xs": [3]}]
        full = list(exp.expand(rows))
        cur = exp.expand(rows)
        cur.next_row()  # a=3, b=0
        cur.next_row()  # a=3, b=1
        saved = cur.cursor()
        self.assertEqual(saved.row_index, 0)
        self.assertEqual(saved.positions, (0, 1))
        self.assertEqual(list(exp.expand(rows, saved)), full[2:])

    def test_resume_right_after_padded_branch(self):
        exp = RowExpander([
            Level("a", "xs", mode="outer"),
            Level("b", lambda row, b: [1], mode="outer"),
        ])
        rows = [{"id": 1, "xs": None}]
        full = list(exp.expand(rows))
        self.assertEqual(len(full), 1)
        cur = exp.expand(rows)
        cur.next_row()
        saved = cur.cursor()
        self.assertEqual(saved.positions, (-1, 0))
        self.assertEqual(list(exp.expand(rows, saved)), [])

    def test_resume_across_input_row_boundary(self):
        exp = RowExpander([Level("a", "xs")])
        rows = [{"id": 1, "xs": [1, 2]}, {"id": 2, "xs": [3, 4]}]
        full = list(exp.expand(rows))
        cur = exp.expand(rows)
        cur.next_row()
        cur.next_row()  # end of first input row
        saved = cur.cursor()
        self.assertEqual(list(exp.expand(rows, saved)), full[2:])

    def test_multiple_resumes_are_stable(self):
        exp = RowExpander([
            Level("a", "xs"),
            Level("b", lambda row, b: range(b["a"])),
        ])
        rows = [{"id": 1, "xs": [2, 3]}]
        cur = exp.expand(rows)
        cur.next_row()
        saved = cur.cursor()
        first = list(exp.expand(rows, saved))
        second = list(exp.expand(rows, saved))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 4)  # a=2:1 remaining, a=3:3 -> 4

    def test_cursor_is_a_value_object(self):
        exp = RowExpander([Level("a", "xs")])
        cur = exp.expand([{"id": 1, "xs": [1, 2]}])
        cur.next_row()
        c1 = cur.cursor()
        c2 = cur.cursor()
        self.assertEqual(c1, c2)
        cur.next_row()
        self.assertNotEqual(c1, cur.cursor())


# ---------------------------------------------------------------------------
# Lazy / no over-materialisation
# ---------------------------------------------------------------------------
class _CountingIterable:
    """A re-iterable range-like source that counts yielded items."""

    def __init__(self, length):
        self.length = length
        self.yielded = 0

    def __iter__(self):
        for i in range(self.length):
            self.yielded += 1
            yield i


class TestLaziness(unittest.TestCase):
    def test_first_three_rows_of_million_item_list(self):
        million = _CountingIterable(1_000_000)
        exp = RowExpander([Level("a", "xs")])

        def rows():
            yield {"id": 1, "xs": million}

        cur = exp.expand(rows)
        batch = cur.next_batch(3)
        self.assertEqual([r["a"] for r in batch], [0, 1, 2])
        self.assertEqual([r["a_ordinal"] for r in batch], [1, 2, 3])
        # Only the three rows we asked for (one element each) were drawn.
        self.assertEqual(million.yielded, 3)

        saved = cur.cursor()
        self.assertEqual(saved.positions, (2,))
        self.assertEqual(million.yielded, 3, "capturing a cursor must not pull")

        # Resuming replays positionally: bounded by the saved position.
        resumed = exp.expand(rows, saved)
        self.assertEqual([r["a"] for r in resumed.next_batch(2)], [3, 4])
        self.assertEqual(million.yielded, 3 + 5)  # 0..4 replayed, no full scan

    def test_cartesian_product_is_not_materialised(self):
        outer = _CountingIterable(1_000_000)
        inner = _CountingIterable(1_000_000)
        exp = RowExpander([
            Level("a", "xs"),
            Level("b", lambda row, b: inner),
        ])

        def rows():
            yield {"id": 1, "xs": outer}

        cur = exp.expand(rows)
        batch = cur.next_batch(3)
        self.assertEqual([(r["a"], r["b"]) for r in batch],
                         [(0, 0), (0, 1), (0, 2)])
        # Product is 10**12 rows; we must only have touched a handful.
        self.assertEqual(outer.yielded, 1)
        self.assertEqual(inner.yielded, 3)
        cur.cursor()  # capture is O(depth), not O(product)
        self.assertLessEqual(outer.yielded + inner.yielded, 10)

    def test_lazy_generator_source_is_never_fully_consumed(self):
        pulled = {"n": 0}

        def infinite(row, bindings):
            i = 0
            while True:
                pulled["n"] += 1
                yield i
                i += 1

        exp = RowExpander([Level("a", lambda row, b: infinite(row, b))])
        rows = [{"id": 1}]
        cur = exp.expand(rows)
        self.assertEqual([r["a"] for r in cur.next_batch(3)], [0, 1, 2])
        self.assertEqual(pulled["n"], 3)
        cur.close()

    def test_deep_inner_empty_scans_but_emits_nothing(self):
        # When every branch is dropped by an inner level, correctness requires
        # inspecting every outer element -- but no Cartesian row is produced.
        outer = _CountingIterable(1_000_000)
        exp = RowExpander([
            Level("a", "xs"),
            Level("b", lambda row, b: [], mode="inner"),
        ])

        def rows():
            yield {"id": 1, "xs": outer}

        collected = list(exp.expand(rows))
        self.assertEqual(collected, [])
        self.assertEqual(outer.yielded, 1_000_000)

    def test_close_releases_generators(self):
        closed = {"n": 0}

        def gen(row, bindings):
            try:
                i = 0
                while True:
                    yield i
                    i += 1
            finally:
                closed["n"] += 1

        exp = RowExpander([Level("a", lambda row, b: gen(row, b))])
        cur = exp.expand([{"id": 1}])
        cur.next_row()
        cur.close()
        self.assertGreaterEqual(closed["n"], 1)


# ---------------------------------------------------------------------------
# Configuration and error handling
# ---------------------------------------------------------------------------
class TestValidation(unittest.TestCase):
    def test_requires_at_least_one_level(self):
        with self.assertRaises(ValueError):
            RowExpander([])

    def test_invalid_mode(self):
        with self.assertRaises(ValueError):
            Level("a", "xs", mode="left")

    def test_duplicate_level_names(self):
        with self.assertRaises(ValueError):
            RowExpander([Level("a", "xs"), Level("a", "ys")])

    def test_duplicate_ordinal_names(self):
        with self.assertRaises(ValueError):
            RowExpander([
                Level("a", "xs", ordinal_name="n"),
                Level("b", "ys", ordinal_name="n"),
            ])

    def test_ordinal_collides_with_level_name(self):
        with self.assertRaises(ValueError):
            RowExpander([
                Level("a", "xs", ordinal_name="b"),
                Level("b", "ys"),
            ])

    def test_level_name_collides_with_input_column(self):
        exp = RowExpander([Level("a", "xs")])
        with self.assertRaises(ValueError):
            list(exp.expand([{"a": 1, "xs": [1]}]))

    def test_ordinal_collides_with_input_column(self):
        exp = RowExpander([Level("a", "xs")])
        with self.assertRaises(ValueError):
            list(exp.expand([{"a_ordinal": 1, "xs": [1]}]))

    def test_missing_source_column(self):
        exp = RowExpander([Level("a", "xs")])
        with self.assertRaises(KeyError):
            list(exp.expand([{"id": 1}]))

    def test_one_shot_column_iterator_rejected(self):
        exp = RowExpander([Level("a", "xs")])
        with self.assertRaises(TypeError):
            list(exp.expand([{"id": 1, "xs": iter([1, 2])}]))

    def test_non_mapping_row_rejected(self):
        exp = RowExpander([Level("a", "xs")])
        with self.assertRaises(TypeError):
            list(exp.expand([(1, 2)]))

    def test_one_shot_snapshot_rejected(self):
        with self.assertRaises(TypeError):
            Snapshot(iter([{"id": 1, "xs": [1]}]))

    def test_cursor_from_wrong_level_count(self):
        exp = RowExpander([Level("a", "xs")])
        rows = [{"id": 1, "xs": [1, 2]}]
        cur = exp.expand(rows)
        cur.next_row()
        bad = cur.cursor()
        two_level = RowExpander([Level("a", "xs"), Level("b", lambda r, b: [1])])
        with self.assertRaises(CursorError):
            two_level.expand(rows, bad)

    def test_cursor_position_past_end(self):
        exp = RowExpander([Level("a", "xs")])
        rows = [{"id": 1, "xs": [1]}]
        with self.assertRaises(CursorError):
            exp.expand(rows, Cursor(row_index=0, positions=(5,)))

    def test_cursor_padded_on_inner_level_rejected(self):
        exp = RowExpander([Level("a", "xs")])
        rows = [{"id": 1, "xs": [1]}]
        with self.assertRaises(CursorError):
            exp.expand(rows, Cursor(row_index=0, positions=(-1,)))

    def test_cursor_row_index_past_end(self):
        exp = RowExpander([Level("a", "xs")])
        rows = [{"id": 1, "xs": [1]}]
        with self.assertRaises(CursorError):
            exp.expand(rows, Cursor(row_index=9, positions=None))

    def test_snapshot_token_mismatch_rejected(self):
        exp = RowExpander([Level("a", "xs")])
        rows = [{"id": 1, "xs": [1, 2]}]
        snap = Snapshot(rows, token="A")
        cur = exp.expand(snap)
        cur.next_row()
        saved = cur.cursor()
        other = Snapshot(rows, token="B")
        with self.assertRaises(CursorError):
            exp.expand(other, saved)

    def test_cursor_finished_with_positions_rejected(self):
        with self.assertRaises(ValueError):
            Cursor(row_index=0, positions=(0,), done=True)

    def test_ordinal_start_validation(self):
        with self.assertRaises(ValueError):
            Level("a", "xs", ordinal_start=-1)
        with self.assertRaises(ValueError):
            Level("a", "xs", ordinal_start=True)

    def test_mapping_level_spec_accepted(self):
        exp = RowExpander([{"name": "a", "source": "xs"}])
        self.assertEqual([r["a"] for r in exp.expand([{"id": 1, "xs": [5]}])], [5])


# ---------------------------------------------------------------------------
# Snapshot resumption
# ---------------------------------------------------------------------------
class TestSnapshotResume(unittest.TestCase):
    def test_resume_from_factory_snapshot(self):
        def rows():
            yield {"id": 1, "xs": [1, 2]}
            yield {"id": 2, "xs": [3]}

        exp = RowExpander([Level("a", "xs")])
        full = list(exp.expand(rows))
        cur = exp.expand(rows)
        cur.next_row()
        cur.next_row()
        saved = cur.cursor()
        self.assertEqual(list(exp.expand(rows, saved)), full[2:])

    def test_resume_method(self):
        exp = RowExpander([Level("a", "xs")])
        rows = [{"id": 1, "xs": [1, 2, 3]}]
        cur = exp.expand(rows)
        cur.next_row()
        self.assertEqual(
            [r["a"] for r in exp.resume(rows, cur.cursor())], [2, 3]
        )


if __name__ == "__main__":
    unittest.main()
