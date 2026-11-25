"""交错情况：多批增删交替、与"每次从零重算"的参考实现逐批对照。

参考实现 ``reference_snapshot`` 完全不复用引擎的增量状态：它拿当前应有的
全部输入行，按分组集合重新扫描一遍算出结果。若引擎的分批增量与它逐批一致，
说明撤回扣减、重复集合、组消失等交错行为没有累积误差。
"""

import itertools
import random
import unittest

from grouping_sets import GroupingSetsEngine, GroupingSetsSpec, diff_snapshots


def content(snapshot_or_block):
    """同 :func:`canonical`，但忽略 ``set_index``——用于比较两份重复集合。"""

    return {
        (row.grouping_values, row.grouping_bits, row.aggregates)
        for row in _rows_of(snapshot_or_block)
    }


def reference_snapshot(spec, rows):
    """从零计算一份快照，结构与引擎的 ``snapshot`` 相同。"""

    blocks = []
    for set_index, grouping_set in enumerate(spec.grouping_sets):
        buckets = {}
        for row in rows:
            key = tuple(row.get(name) for name in grouping_set.columns)
            buckets.setdefault(key, []).append(row)
        out = []
        if not grouping_set.columns:
            source = buckets.get((), [])
            out.append(_make(spec, set_index, grouping_set, (), source))
        else:
            for key in sorted(buckets, key=repr):
                out.append(
                    _make(spec, set_index, grouping_set, key, buckets[key])
                )
        blocks.append(tuple(out))
    return tuple(blocks)


def _make(spec, set_index, grouping_set, key, source_rows):
    from grouping_sets.model import make_row

    values = []
    for name in spec.grouping_columns:
        values.append(key[grouping_set.columns.index(name)] if name in grouping_set.columns else None)
    bits = tuple(0 if name in grouping_set.columns else 1 for name in spec.grouping_columns)
    aggs = []
    for agg in spec.aggregates:
        if agg.column == "*":
            aggs.append(len(source_rows))
            continue
        values_present = [
            row.get(agg.column) for row in source_rows if row.get(agg.column) is not None
        ]
        if agg.func == "count":
            aggs.append(len(values_present))
        elif agg.func == "sum":
            aggs.append(sum(values_present) if values_present else None)
        elif agg.func == "min":
            aggs.append(min(values_present) if values_present else None)
        else:  # pragma: no cover
            raise AssertionError(agg.func)
    return make_row(set_index, tuple(values), bits, tuple(aggs))


def _rows_of(snapshot_or_block):
    """接受"快照（块的序列）"或"单个块（行的序列）"。"""

    items = list(snapshot_or_block)
    if items and isinstance(items[0], tuple):
        return [row for block in items for row in block]
    return items


def canonical(snapshot_or_block):
    """把快照/块转成可比较的集合（参考实现按 repr 排序，顺序不作为断言目标）。"""

    return {
        (row.set_index, row.grouping_values, row.grouping_bits, row.aggregates)
        for row in _rows_of(snapshot_or_block)
    }


def spec_for_tests():
    return GroupingSetsSpec(
        grouping_columns=["region", "product"],
        grouping_sets=[["region", "product"], ["region"], [], ["region"]],
        aggregates=[
            ("count", "*", "n"),
            ("count", "amount", "n_amount"),
            ("sum", "amount", "total"),
            ("min", "amount", "lowest"),
        ],
    )


class InterleavedBatchesTests(unittest.TestCase):
    def test_alternating_adds_and_retracts_match_reference(self):
        spec = spec_for_tests()
        engine = GroupingSetsEngine(spec)
        live = []
        rows = [
            {"region": "n", "product": "a", "amount": 3},
            {"region": "n", "product": "a", "amount": 3},
            {"region": "n", "product": "b", "amount": -4},
            {"region": "s", "product": "a", "amount": None},
            {"region": "s", "product": "b", "amount": 10},
            {"region": "s", "product": "b", "amount": 1},
        ]
        script = [
            ("add", 0), ("add", 1), ("retract", 0), ("add", 2),
            ("add", 3), ("retract", 1), ("add", 4), ("retract", 2),
            ("add", 5), ("retract", 3), ("retract", 4), ("retract", 5),
        ]
        for step, (op, index) in enumerate(script):
            row = rows[index]
            if op == "add":
                result = engine.apply(adds=[row])
                live.append(row)
            else:
                result = engine.apply(retracts=[row])
                live.remove(row)
            expected = reference_snapshot(spec, live)
            self.assertEqual(
                canonical(result.snapshot),
                canonical(expected),
                "snapshot mismatch at step %d (%s %r)" % (step, op, row),
            )

    def test_random_workload_matches_reference_and_diff(self):
        rng = random.Random(20261126)
        spec = spec_for_tests()
        engine = GroupingSetsEngine(spec)
        pool = []
        for region in ("n", "s", "e"):
            for product in ("a", "b"):
                for amount in (1, 2, 2, -3, None):
                    pool.append(
                        {"region": region, "product": product, "amount": amount}
                    )
        live = []
        previous_snapshot = engine.current_snapshot()
        for batch in range(40):
            adds = []
            retracts = []
            for _ in range(rng.randint(0, 3)):
                if live and rng.random() < 0.5:
                    row = live.pop(rng.randrange(len(live)))
                    retracts.append(row)
                else:
                    row = pool[rng.randrange(len(pool))]
                    live.append(row)
                    adds.append(row)
            result = engine.apply(adds=adds, retracts=retracts)

            expected = reference_snapshot(spec, live)
            self.assertEqual(
                canonical(result.snapshot),
                canonical(expected),
                "snapshot mismatch in batch %d" % batch,
            )

            expected_upserts, expected_deletes = diff_snapshots(
                previous_snapshot, expected
            )
            self.assertEqual(
                canonical([result.upserts]),
                canonical([expected_upserts]),
                "upserts mismatch in batch %d" % batch,
            )
            self.assertEqual(
                canonical([result.deletes]),
                canonical([expected_deletes]),
                "deletes mismatch in batch %d" % batch,
            )
            previous_snapshot = expected

    def test_two_rows_that_cancel_inside_one_batch_report_nothing(self):
        spec = spec_for_tests()
        engine = GroupingSetsEngine(spec)
        row = {"region": "n", "product": "a", "amount": 7}
        engine.apply(adds=[row])
        result = engine.apply(adds=[row], retracts=[row])
        self.assertEqual(result.upserts, ())
        self.assertEqual(result.deletes, ())
        self.assertEqual(canonical(result.snapshot), canonical(engine.current_snapshot()))

    def test_group_reappears_after_vanishing(self):
        spec = spec_for_tests()
        engine = GroupingSetsEngine(spec)
        row = {"region": "n", "product": "a", "amount": 7}
        engine.apply(adds=[row])
        gone = engine.apply(retracts=[row])
        self.assertIn((0, ("n", "a")), {(r.set_index, r.grouping_values) for r in gone.deletes})
        back = engine.apply(adds=[row])
        self.assertIn((0, ("n", "a")), {(r.set_index, r.grouping_values) for r in back.upserts})
        self.assertEqual(
            canonical(back.snapshot),
            canonical(reference_snapshot(spec, [row])),
        )

    def test_many_identical_rows_counts_survive_repeated_retraction(self):
        spec = GroupingSetsSpec(
            grouping_columns=["g"],
            grouping_sets=[["g"], [], ["g"]],
            aggregates=[("count", "*", "n"), ("sum", "v", "s"), ("min", "v", "m")],
        )
        engine = GroupingSetsEngine(spec)
        row = {"g": "x", "v": 2}
        engine.apply(adds=[row] * 5)
        for remaining in range(4, -1, -1):
            result = engine.apply(retracts=[row])
            leaf = [r for r in result.snapshot[0] if r.grouping_values == ("x",)]
            if remaining:
                self.assertEqual(leaf[0].aggregates, (remaining, 2 * remaining, 2))
            else:
                self.assertEqual(leaf, [])
            self.assertEqual(
                result.snapshot[1][0].aggregates,
                (remaining, 2 * remaining if remaining else None, 2 if remaining else None),
            )
            # 两个重复集合的叶子行必须始终一致
            self.assertEqual(
                content(result.snapshot[0]), content(result.snapshot[2])
            )

    def test_batches_are_isolated_from_each_other(self):
        spec = spec_for_tests()
        engine = GroupingSetsEngine(spec)
        first = engine.apply(adds=[{"region": "n", "product": "a", "amount": 1}])
        first_snapshot = first.snapshot
        engine.apply(adds=[{"region": "n", "product": "a", "amount": 1}])
        # 已经返回的快照对象不应被后续批次改写
        self.assertEqual(
            canonical(first_snapshot),
            canonical(reference_snapshot(spec, [{"region": "n", "product": "a", "amount": 1}])),
        )


class DuplicateSetInterleavingTests(unittest.TestCase):
    def test_duplicate_sets_stay_in_lockstep_under_random_workload(self):
        rng = random.Random(7)
        spec = GroupingSetsSpec(
            grouping_columns=["a", "b"],
            grouping_sets=[["a", "b"], ["a", "b"], ["a"]],
            aggregates=[("count", "*", "n"), ("sum", "v", "s")],
        )
        engine = GroupingSetsEngine(spec)
        live = []
        pool = [
            {"a": a, "b": b, "v": v}
            for a in (1, 2)
            for b in ("x", "y")
            for v in (1, -1, 4)
        ]
        for _ in range(30):
            if live and rng.random() < 0.5:
                row = live.pop(rng.randrange(len(live)))
                engine.apply(retracts=[row])
            else:
                row = pool[rng.randrange(len(pool))]
                live.append(row)
                engine.apply(adds=[row])
            # 两份重复集合的状态必须逐行一致
            self.assertEqual(
                content(engine.current_snapshot()[0]),
                content(engine.current_snapshot()[1]),
            )
            self.assertEqual(
                canonical(engine.current_snapshot()),
                canonical(reference_snapshot(spec, live)),
            )

    def test_retract_affects_each_duplicate_exactly_once(self):
        spec = GroupingSetsSpec(
            grouping_columns=["g"],
            grouping_sets=[["g"], ["g"], ["g"]],
            aggregates=[("count", "*", "n")],
        )
        engine = GroupingSetsEngine(spec)
        engine.apply(adds=[{"g": "x"}, {"g": "x"}])
        engine.apply(retracts=[{"g": "x"}])
        self.assertEqual(engine.stored_row_counts(), (1, 1, 1))
        for block in engine.current_snapshot():
            self.assertEqual([row.aggregates for row in block], [(1,)])

    def test_duplicate_empty_sets_each_keep_their_own_zero_row(self):
        spec = GroupingSetsSpec(
            grouping_columns=["g"],
            grouping_sets=[[], []],
            aggregates=[("count", "*", "n"), ("sum", "v", "s")],
        )
        engine = GroupingSetsEngine(spec)
        result = engine.apply(adds=[{"g": "x", "v": 3}, {"g": "y", "v": 4}])
        self.assertEqual(len(result.snapshot), 2)
        for block in result.snapshot:
            self.assertEqual(len(block), 1)
            self.assertEqual(block[0].aggregates, (2, 7))
        engine.reset()
        for block in engine.current_snapshot():
            self.assertEqual(block[0].aggregates, (0, None))


class LargeBatchTests(unittest.TestCase):
    def test_thousands_of_rows_across_batches_match_reference(self):
        spec = GroupingSetsSpec(
            grouping_columns=["region", "product"],
            grouping_sets=[["region", "product"], ["region"], []],
            aggregates=[("count", "*", "n"), ("sum", "amount", "total")],
        )
        engine = GroupingSetsEngine(spec)
        rng = random.Random(1234)
        live = []
        for batch in range(5):
            adds = [
                {
                    "region": "r%d" % rng.randrange(4),
                    "product": "p%d" % rng.randrange(3),
                    "amount": rng.randrange(-50, 50),
                }
                for _ in range(400)
            ]
            live.extend(adds)
            result = engine.apply(adds=adds)
            self.assertEqual(
                canonical(result.snapshot), canonical(reference_snapshot(spec, live))
            )
            retracts = [live.pop(rng.randrange(len(live))) for _ in range(150)]
            result = engine.apply(retracts=retracts)
            self.assertEqual(
                canonical(result.snapshot), canonical(reference_snapshot(spec, live))
            )


if __name__ == "__main__":
    unittest.main()
