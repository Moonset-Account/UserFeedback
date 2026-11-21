"""增量会话算子的验收用例。

覆盖三类约束：可接受的行为（会话合并、拆分、封存后的稳定性），应被拒绝的
行为（重复编号、未知编号、水位及以前的操作、封存后撤回、水位回退），以及
两类之间的边界与交错场景（恰好 10 秒间隔、恰好落在水位上、恰好等于
``last + gap`` 的封存线、桥接事件撤回后再插入）。
"""

from __future__ import annotations

import unittest

from session_operator import (
    ChangeKind,
    DuplicateEventId,
    RejectedOperation,
    SealedSessionViolation,
    SessionOperator,
    SessionResult,
    UnknownEventId,
    WatermarkRegression,
    WatermarkViolation,
)


def plain(result: SessionResult) -> tuple:
    """把结果压成可直接比较的普通元组。"""
    return (
        result.device,
        result.first_time,
        result.last_time,
        result.event_count,
        result.amount_sum,
    )


class Downstream:
    """只消费 changelog 的下游视图。

    它不读取算子内部状态，重复插入或撤回不存在的结果都会立刻报错，
    因此能验证“先合并、再拆分”的可见顺序以及 changelog 的自洽性。
    """

    def __init__(self) -> None:
        self._results: dict = {}

    def apply(self, changes) -> None:
        for change in changes:
            key = plain(change.result)
            if change.kind is ChangeKind.INSERT:
                if key in self._results:
                    raise AssertionError(f"重复插入未撤回的结果：{key}")
                self._results[key] = change.result
            elif change.kind is ChangeKind.RETRACT:
                if key not in self._results:
                    raise AssertionError(f"撤回了从未发布的结果：{key}")
                del self._results[key]
            else:  # pragma: no cover - 枚举被扩展时立即暴露
                raise AssertionError(f"未知变更类型：{change.kind!r}")

    def results(self) -> list:
        return sorted(
            (plain(result) for result in self._results.values()),
            key=lambda item: (item[0], item[1], item[2]),
        )


class ChangeAssertions(unittest.TestCase):
    def assert_changes(self, changes, expected) -> None:
        actual = [(change.kind.value, *plain(change.result)) for change in changes]
        self.assertEqual(actual, expected)

    def assert_results(self, results, expected) -> None:
        self.assertEqual([plain(result) for result in results], expected)

    def assert_consistent(self, operator: SessionOperator) -> None:
        self.assertEqual(
            sorted(plain(result) for result in operator.all_published_results()),
            sorted(plain(result) for result in self._materialize_all(operator)),
        )

    @staticmethod
    def _materialize_all(operator: SessionOperator) -> list:
        results = []
        for device in sorted(operator.devices()):
            results.extend(operator.materialized_results(device))
        return results


class TestDocumentedScenario(ChangeAssertions):
    """题面场景：按 0、18、9 秒加入三条，再撤回 9 秒那条。"""

    def test_merge_then_split_with_bystander_device(self):
        operator = SessionOperator()
        operator.insert("b1", "B", 5.0, 100.0)

        self.assert_changes(
            operator.insert("e1", "A", 0.0, 10.0),
            [("insert", "A", 0.0, 0.0, 1, 10.0)],
        )
        self.assert_changes(
            operator.insert("e2", "A", 18.0, 20.0),
            [("insert", "A", 18.0, 18.0, 1, 20.0)],
        )

        # 9 秒的桥接事件把两个单事件会话合并成一个 (0..18)。
        merge = operator.insert("e3", "A", 9.0, 5.0)
        self.assert_changes(
            merge,
            [
                ("retract", "A", 0.0, 0.0, 1, 10.0),
                ("retract", "A", 18.0, 18.0, 1, 20.0),
                ("insert", "A", 0.0, 18.0, 3, 35.0),
            ],
        )

        # 撤回桥接事件把会话重新拆成两个。
        split = operator.retract("e3")
        self.assert_changes(
            split,
            [
                ("retract", "A", 0.0, 18.0, 3, 35.0),
                ("insert", "A", 0.0, 0.0, 1, 10.0),
                ("insert", "A", 18.0, 18.0, 1, 20.0),
            ],
        )

        self.assertEqual(
            [change.kind for change in merge],
            [ChangeKind.RETRACT, ChangeKind.RETRACT, ChangeKind.INSERT],
        )
        self.assertEqual(
            [change.kind for change in split],
            [ChangeKind.RETRACT, ChangeKind.INSERT, ChangeKind.INSERT],
        )
        # 其他设备完全不受影响。
        self.assertTrue(all(change.result.device == "A" for change in merge + split))
        self.assert_results(operator.published_results("B"), [("B", 5.0, 5.0, 1, 100.0)])
        self.assert_consistent(operator)

    def test_downstream_sees_merge_before_split(self):
        operator = SessionOperator()
        operator.insert("b1", "B", 5.0, 100.0)
        downstream = Downstream()

        snapshots = []

        def consume(changes):
            downstream.apply(changes)
            snapshots.append(downstream.results())

        consume(operator.insert("e1", "A", 0.0, 10.0))
        consume(operator.insert("e2", "A", 18.0, 20.0))
        consume(operator.insert("e3", "A", 9.0, 5.0))
        consume(operator.retract("e3"))

        self.assertEqual(
            snapshots,
            [
                [("A", 0.0, 0.0, 1, 10.0)],
                [("A", 0.0, 0.0, 1, 10.0), ("A", 18.0, 18.0, 1, 20.0)],
                [("A", 0.0, 18.0, 3, 35.0)],
                [("A", 0.0, 0.0, 1, 10.0), ("A", 18.0, 18.0, 1, 20.0)],
            ],
        )
        self.assertEqual(operator.materialized_results("A"), operator.published_results("A"))

    def test_bystander_insert_emits_only_its_own_device(self):
        operator = SessionOperator()
        operator.insert("a1", "A", 0.0, 1.0)
        changes = operator.insert("b1", "B", 0.0, 2.0)
        self.assertEqual([change.result.device for change in changes], ["B"])


class TestSessionBoundaries(ChangeAssertions):
    def test_exactly_ten_seconds_is_the_same_session(self):
        operator = SessionOperator()
        operator.insert("e1", "A", 0.0, 1.0)
        self.assert_changes(
            operator.insert("e2", "A", 10.0, 2.0),
            [
                ("retract", "A", 0.0, 0.0, 1, 1.0),
                ("insert", "A", 0.0, 10.0, 2, 3.0),
            ],
        )

    def test_just_over_ten_seconds_starts_a_new_session(self):
        operator = SessionOperator()
        operator.insert("e1", "A", 0.0, 1.0)
        self.assert_changes(
            operator.insert("e2", "A", 10.001, 2.0),
            [("insert", "A", 10.001, 10.001, 1, 2.0)],
        )

    def test_chain_of_ten_second_hops_forms_one_session(self):
        operator = SessionOperator()
        for index in range(5):
            operator.insert(f"e{index}", "A", index * 10.0, 1.0)
        self.assert_results(operator.materialized_results("A"), [("A", 0.0, 40.0, 5, 5.0)])

    def test_out_of_order_and_negative_times(self):
        operator = SessionOperator()
        operator.insert("e2", "A", -5.0, 2.0)
        operator.insert("e1", "A", -12.0, 1.0)
        self.assert_results(operator.materialized_results("A"), [("A", -12.0, -5.0, 2, 3.0)])

    def test_custom_gap(self):
        operator = SessionOperator(gap_seconds=2.0)
        operator.insert("e1", "A", 0.0, 1.0)
        self.assert_changes(
            operator.insert("e2", "A", 3.0, 1.0),
            [("insert", "A", 3.0, 3.0, 1, 1.0)],
        )

    def test_invalid_gap_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            SessionOperator(gap_seconds=-1)
        with self.assertRaises(ValueError):
            SessionOperator(gap_seconds=float("inf"))
        with self.assertRaises(TypeError):
            SessionOperator(gap_seconds=True)  # type: ignore[arg-type]


class TestRejectedOperations(ChangeAssertions):
    def test_duplicate_event_id_rejected_without_side_effects(self):
        operator = SessionOperator()
        operator.insert("e1", "A", 0.0, 1.0)
        before = operator.published_results("A")
        with self.assertRaises(DuplicateEventId):
            operator.insert("e1", "A", 50.0, 9.0)
        self.assertEqual(operator.published_results("A"), before)
        self.assert_results(operator.materialized_results("A"), [("A", 0.0, 0.0, 1, 1.0)])

    def test_event_id_is_unique_across_devices(self):
        operator = SessionOperator()
        operator.insert("e1", "A", 0.0, 1.0)
        with self.assertRaises(DuplicateEventId):
            operator.insert("e1", "B", 0.0, 1.0)

    def test_unknown_event_id_rejected(self):
        operator = SessionOperator()
        operator.insert("e1", "A", 0.0, 1.0)
        operator.retract("e1")
        with self.assertRaises(UnknownEventId):
            operator.retract("e1")
        with self.assertRaises(UnknownEventId):
            operator.retract("never-seen")

    def test_rejections_share_the_rejected_operation_base(self):
        self.assertTrue(issubclass(DuplicateEventId, RejectedOperation))
        self.assertTrue(issubclass(UnknownEventId, RejectedOperation))
        self.assertTrue(issubclass(WatermarkViolation, RejectedOperation))
        self.assertTrue(issubclass(SealedSessionViolation, RejectedOperation))
        self.assertTrue(issubclass(WatermarkRegression, RejectedOperation))

    def test_non_numeric_inputs_rejected(self):
        operator = SessionOperator()
        with self.assertRaises(TypeError):
            operator.insert("e1", "A", "0", 1.0)
        with self.assertRaises(ValueError):
            operator.insert("e1", "A", float("nan"), 1.0)
        with self.assertRaises(ValueError):
            operator.insert("e1", "A", 0.0, float("inf"))


class TestWatermarkRules(ChangeAssertions):
    def test_event_time_equal_to_watermark_is_rejected(self):
        operator = SessionOperator()
        operator.advance_watermark(10.0)
        with self.assertRaises(WatermarkViolation):
            operator.insert("e1", "A", 10.0, 1.0)
        self.assert_results(operator.materialized_results("A"), [])

    def test_event_after_watermark_is_accepted(self):
        operator = SessionOperator()
        operator.advance_watermark(10.0)
        self.assert_changes(
            operator.insert("e1", "A", 10.0001, 1.0),
            [("insert", "A", 10.0001, 10.0001, 1, 1.0)],
        )

    def test_retract_below_watermark_is_rejected_even_if_unsealed(self):
        operator = SessionOperator()
        operator.insert("e1", "A", 0.0, 1.0)
        # 会话 (0..0) 尚未封存：last+gap=10 > 5。
        operator.advance_watermark(5.0)
        self.assertFalse(operator.is_sealed(0.0))
        with self.assertRaises(WatermarkViolation):
            operator.retract("e1")
        self.assert_results(operator.published_results("A"), [("A", 0.0, 0.0, 1, 1.0)])

    def test_retract_above_watermark_allowed(self):
        operator = SessionOperator()
        operator.insert("e1", "A", 30.0, 2.0)
        operator.advance_watermark(5.0)
        self.assert_changes(
            operator.retract("e1"),
            [("retract", "A", 30.0, 30.0, 1, 2.0)],
        )

    def test_sealing_boundary_is_last_time_plus_gap(self):
        operator = SessionOperator()
        operator.insert("e1", "A", 0.0, 1.0)
        operator.advance_watermark(9.999)
        # last + gap = 10 > 9.999，尚未封存，但 0 <= 水位，撤回仍被水位规则拒绝。
        with self.assertRaises(WatermarkViolation):
            operator.retract("e1")
        self.assertFalse(operator.is_sealed(0.0))

        operator.advance_watermark(10.0)
        # last + gap = 10 <= 10，已封存。
        self.assertTrue(operator.is_sealed(0.0))
        with self.assertRaises(SealedSessionViolation):
            operator.retract("e1")
        self.assert_results(operator.published_results("A"), [("A", 0.0, 0.0, 1, 1.0)])

    def test_sealed_retract_is_rejected_atomically(self):
        operator = SessionOperator()
        operator.insert("e1", "A", 0.0, 1.0)
        operator.insert("e2", "A", 4.0, 2.0)
        operator.advance_watermark(14.0)
        before = operator.published_results("A")
        with self.assertRaises(WatermarkViolation):
            operator.retract("e2")
        self.assertEqual(operator.published_results("A"), before)
        self.assert_results(operator.materialized_results("A"), [("A", 0.0, 4.0, 2, 3.0)])

    def test_unsealed_session_stays_mutable_above_the_watermark(self):
        operator = SessionOperator()
        operator.insert("e1", "A", 0.0, 1.0)
        operator.insert("e2", "A", 20.0, 2.0)
        operator.advance_watermark(12.0)
        # 会话 (0..0) 已封存（10 <= 12）；会话 (20..20) 未封存也未到水位。
        self.assertTrue(operator.is_sealed(0.0))
        self.assertFalse(operator.is_sealed(20.0))
        self.assert_changes(
            operator.retract("e2"),
            [("retract", "A", 20.0, 20.0, 1, 2.0)],
        )
        self.assert_results(operator.published_results("A"), [("A", 0.0, 0.0, 1, 1.0)])

    def test_sealing_is_judged_by_session_end_not_event_time(self):
        # 会话 (0..16) 的末尾 16 + 10 = 26 > 18，未封存；事件 8 虽在水位及
        # 以前被拒绝，但拒绝原因是水位规则而不是封存。
        operator = SessionOperator()
        operator.insert("e1", "A", 0.0, 1.0)
        operator.insert("e2", "A", 8.0, 1.0)
        operator.insert("e3", "A", 16.0, 1.0)
        operator.advance_watermark(18.0)
        self.assertFalse(operator.is_sealed(16.0))
        with self.assertRaises(WatermarkViolation) as raised:
            operator.retract("e2")
        self.assertNotIsInstance(raised.exception, SealedSessionViolation)
        # 同一事件若所在会话已封存，则报封存错误。
        operator.insert("e4", "A", 60.0, 1.0)
        operator.advance_watermark(70.0)  # 会话 (60..60)：70 >= 70，封存
        with self.assertRaises(SealedSessionViolation):
            operator.retract("e4")

    def test_watermark_regression_rejected_and_watermark_unchanged(self):
        operator = SessionOperator()
        operator.advance_watermark(20.0)
        with self.assertRaises(WatermarkRegression):
            operator.advance_watermark(19.999)
        self.assertEqual(operator.watermark, 20.0)

    def test_watermark_repeat_is_accepted(self):
        operator = SessionOperator()
        operator.advance_watermark(20.0)
        self.assertEqual(operator.advance_watermark(20.0), [])
        self.assertEqual(operator.watermark, 20.0)

    def test_invalid_watermark_values_rejected(self):
        operator = SessionOperator()
        with self.assertRaises(TypeError):
            operator.advance_watermark("10")
        with self.assertRaises(ValueError):
            operator.advance_watermark(float("nan"))

    def test_insert_with_atomic_watermark_batch(self):
        operator = SessionOperator()
        changes = operator.insert("e1", "A", 15.0, 1.0, watermark=5.0)
        self.assertEqual(operator.watermark, 5.0)
        self.assert_changes(changes, [("insert", "A", 15.0, 15.0, 1, 1.0)])

    def test_atomic_watermark_batch_rolls_back_on_rejection(self):
        operator = SessionOperator()
        operator.advance_watermark(2.0)
        with self.assertRaises(WatermarkViolation):
            operator.insert("e1", "A", 1.0, 1.0, watermark=8.0)
        # 失败批次不会推进水位，也不会写入事件。
        self.assertEqual(operator.watermark, 2.0)
        self.assert_results(operator.materialized_results("A"), [])

    def test_atomic_watermark_batch_rolls_back_on_duplicate(self):
        operator = SessionOperator()
        operator.insert("e1", "A", 100.0, 1.0)
        with self.assertRaises(DuplicateEventId):
            operator.insert("e1", "A", 100.0, 1.0, watermark=50.0)
        self.assertIsNone(operator.watermark)

    def test_retract_with_atomic_watermark_batch(self):
        operator = SessionOperator()
        operator.insert("e1", "A", 50.0, 1.0)
        changes = operator.retract("e1", watermark=45.0)
        self.assertEqual(operator.watermark, 45.0)
        self.assert_changes(changes, [("retract", "A", 50.0, 50.0, 1, 1.0)])

    def test_atomic_watermark_can_seal_while_inserting_later_event(self):
        operator = SessionOperator()
        operator.insert("e1", "A", 0.0, 1.0)
        # 同一批次推进水位到 10 后插入 20 秒的事件：水位 10 恰好封存 (0..0)。
        operator.insert("e2", "A", 20.0, 2.0, watermark=10.0)
        self.assertTrue(operator.is_sealed(0.0))
        with self.assertRaises(WatermarkViolation):
            operator.retract("e1")


class TestChangelogInvariants(ChangeAssertions):
    def test_retracts_precede_inserts_within_a_batch(self):
        operator = SessionOperator()
        operator.insert("e1", "A", 0.0, 1.0)
        operator.insert("e2", "A", 18.0, 2.0)
        changes = operator.insert("e3", "A", 9.0, 3.0)
        self.assertEqual(
            [change.kind for change in changes],
            [ChangeKind.RETRACT, ChangeKind.RETRACT, ChangeKind.INSERT],
        )
        self.assert_changes(
            changes,
            [
                ("retract", "A", 0.0, 0.0, 1, 1.0),
                ("retract", "A", 18.0, 18.0, 1, 2.0),
                ("insert", "A", 0.0, 18.0, 3, 6.0),
            ],
        )

    def test_split_then_remerge(self):
        operator = SessionOperator()
        operator.insert("e1", "A", 0.0, 1.0)
        operator.insert("e2", "A", 18.0, 2.0)
        operator.insert("e3", "A", 9.0, 3.0)
        operator.retract("e3")
        self.assert_changes(
            operator.insert("e3", "A", 9.0, 3.0),
            [
                ("retract", "A", 0.0, 0.0, 1, 1.0),
                ("retract", "A", 18.0, 18.0, 1, 2.0),
                ("insert", "A", 0.0, 18.0, 3, 6.0),
            ],
        )

    def test_downstream_replay_matches_materialized_state(self):
        operator = SessionOperator()
        downstream = Downstream()
        script = [
            ("insert", "e1", "A", 0.0, 1.5),
            ("insert", "e2", "A", 25.0, 2.5),
            ("insert", "b1", "B", 3.0, 7.0),
            ("insert", "e3", "A", 12.0, 4.0),
            ("retract", "e3"),
            ("insert", "e4", "A", 40.0, 8.0),
            ("retract", "e1"),
            ("insert", "b2", "B", 40.0, 1.0),
        ]
        for step in script:
            downstream.apply(operator.apply(step))
            self.assertEqual(
                downstream.results(),
                sorted(
                    (plain(result) for result in operator.all_published_results()),
                    key=lambda item: (item[0], item[1], item[2]),
                ),
            )

    def test_amount_sum_uses_float_tolerance(self):
        operator = SessionOperator()
        for index, amount in enumerate([0.1, 0.2, 0.3]):
            operator.insert(f"e{index}", "A", float(index), amount)
        (result,) = operator.materialized_results("A")
        self.assertAlmostEqual(result.amount_sum, 0.6, places=12)

    def test_multi_device_isolation_under_changelog(self):
        operator = SessionOperator()
        downstream = Downstream()
        downstream.apply(operator.insert("a1", "A", 0.0, 1.0))
        downstream.apply(operator.insert("a2", "A", 20.0, 2.0))
        downstream.apply(operator.insert("b1", "B", 0.0, 9.0))
        downstream.apply(operator.insert("b2", "B", 20.0, 9.0))
        downstream.apply(operator.insert("b3", "B", 10.0, 9.0))
        before_a = operator.published_results("A")
        downstream.apply(operator.retract("b3"))
        self.assertEqual(operator.published_results("A"), before_a)
        self.assertEqual(
            downstream.results(),
            [
                ("A", 0.0, 0.0, 1, 1.0),
                ("A", 20.0, 20.0, 1, 2.0),
                ("B", 0.0, 0.0, 1, 9.0),
                ("B", 20.0, 20.0, 1, 9.0),
            ],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
