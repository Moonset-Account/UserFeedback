"""收集与合并模块的单元测试。

运行：
    python3 -m unittest discover -s tests -v

测试数据全部为本地合成数据，不访问任何外部目标。
"""

import unittest

from pipeline_collect_merge import (
    Collector,
    Interval,
    MergeResult,
    Replacement,
    ShardAttempt,
    compose,
)
from pipeline_collect_merge.transforms import LabeledTransform, identity


def attempt(shard_id, number, lo, hi, label, success=True):
    """构造一个带标签变换的分片尝试。"""
    return ShardAttempt(
        shard_id=shard_id,
        attempt=number,
        interval=Interval(lo, hi),
        transform=LabeledTransform(label),
        success=success,
    )


class IntervalTests(unittest.TestCase):
    def test_valid_interval(self):
        iv = Interval(0, 10)
        self.assertEqual(iv.length, 10)

    def test_reversed_or_empty_interval_rejected(self):
        with self.assertRaises(ValueError):
            Interval(5, 5)
        with self.assertRaises(ValueError):
            Interval(5, 2)

    def test_negative_endpoint_rejected(self):
        with self.assertRaises(ValueError):
            Interval(-1, 5)


class ComposeTests(unittest.TestCase):
    def test_compose_left_to_right(self):
        f = compose([LabeledTransform("a"), LabeledTransform("b")])
        self.assertEqual(f([]), ["a", "b"])

    def test_compose_order_matters(self):
        # 顺序颠倒会改变状态：两个非交换变换的组合顺序不可互换。
        ab = compose([LabeledTransform("a"), LabeledTransform("b")])
        ba = compose([LabeledTransform("b"), LabeledTransform("a")])
        self.assertNotEqual(ab([]), ba([]))

    def test_compose_empty_is_identity(self):
        f = compose([])
        self.assertEqual(f(["x"]), identity()(["x"]))

    def test_compose_uses_snapshot(self):
        transforms = [LabeledTransform("a")]
        f = compose(transforms)
        transforms.append(LabeledTransform("b"))  # 事后修改不影响已组合结果
        self.assertEqual(f([]), ["a"])


class BasicMergeTests(unittest.TestCase):
    def test_contiguous_shards_merge_in_order(self):
        collector = Collector()
        # 故意乱序上报，合并必须按区间排序。
        collector.report_attempt(attempt("s2", 0, 6, 10, "D"))
        collector.report_attempt(attempt("s1", 0, 0, 6, "A"))

        result = collector.merge()
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.used_segments, [("s1", 0), ("s2", 0)])
        self.assertEqual(result.full_transform([]), ["A", "D"])
        self.assertEqual(result.missing, [])
        self.assertEqual(result.gap, [])

    def test_gap_between_shards_keeps_waiting(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 4, "A"))
        collector.report_attempt(attempt("s2", 0, 8, 10, "D"))

        result = collector.merge()
        self.assertEqual(result.status, "waiting")
        self.assertFalse(result.ready)
        self.assertEqual(result.missing, [Interval(4, 8)])
        self.assertEqual(result.gap, [Interval(4, 8)])
        # 等待期间不输出不完整的变换。
        self.assertEqual(result.used_segments, [])
        self.assertEqual(result.full_transform(["keep"]), ["keep"])

    def test_gap_filled_after_missing_shard_arrives(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 4, "A"))
        collector.report_attempt(attempt("s2", 0, 8, 10, "D"))
        self.assertEqual(collector.merge().status, "waiting")

        collector.report_attempt(attempt("s3", 0, 4, 8, "M"))
        result = collector.merge()
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.full_transform([]), ["A", "M", "D"])

    def test_no_reports_is_waiting(self):
        result = Collector().merge()
        self.assertEqual(result.status, "waiting")
        self.assertEqual(result.missing, [])


class ReplacementTests(unittest.TestCase):
    def _setup_split(self, success=True):
        """s1@0 [0,10) 重试拆为 s2@1 [0,4) 与 s3@1 [4,10)。"""
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 10, "OLD"))
        collector.report_replacement(
            Replacement("s1", 0, [("s2", 1), ("s3", 1)])
        )
        collector.report_attempt(attempt("s2", 1, 0, 4, "P1", success))
        collector.report_attempt(attempt("s3", 1, 4, 10, "P2", success=True))
        return collector

    def test_successful_split_enables_replacement(self):
        collector = self._setup_split(success=True)
        result = collector.merge()
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.used_segments, [("s2", 1), ("s3", 1)])
        self.assertEqual(result.full_transform([]), ["P1", "P2"])

    def test_failed_segment_disables_replacement_and_falls_back(self):
        collector = self._setup_split(success=False)
        result = collector.merge()
        # 新片段未全部成功 -> 替换不启用；旧分片成功 -> 回退。
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.used_segments, [("s1", 0)])
        self.assertEqual(result.full_transform([]), ["OLD"])

    def test_missing_segment_keeps_waiting(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 10, "OLD"))
        collector.report_replacement(
            Replacement("s1", 0, [("s2", 1), ("s3", 1)])
        )
        collector.report_attempt(attempt("s2", 1, 0, 4, "P1"))

        result = collector.merge()
        self.assertEqual(result.status, "waiting")
        # 已上报邻片可推断精确缺口 [4,10)。
        self.assertEqual(result.missing, [Interval(4, 10)])
        self.assertEqual(result.used_segments, [])

    def test_missing_middle_segment_infers_exact_gap(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 12, "OLD"))
        collector.report_replacement(
            Replacement("s1", 0, [("a", 1), ("b", 1), ("c", 1)])
        )
        collector.report_attempt(attempt("a", 1, 0, 4, "A"))
        collector.report_attempt(attempt("c", 1, 8, 12, "C"))

        result = collector.merge()
        self.assertEqual(result.status, "waiting")
        self.assertEqual(result.missing, [Interval(4, 8)])

        collector.report_attempt(attempt("b", 1, 4, 8, "B"))
        result = collector.merge()
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.full_transform([]), ["A", "B", "C"])

    def test_waiting_then_switch_to_replacement_when_segments_arrive(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 10, "OLD"))
        collector.report_replacement(
            Replacement("s1", 0, [("s2", 1), ("s3", 1)])
        )
        collector.report_attempt(attempt("s2", 1, 0, 4, "P1"))
        self.assertEqual(collector.merge().status, "waiting")

        collector.report_attempt(attempt("s3", 1, 4, 10, "P2"))
        result = collector.merge()
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.used_segments, [("s2", 1), ("s3", 1)])

    def test_failed_split_with_failed_old_shard_falls_back_to_older_attempt(
        self,
    ):
        collector = Collector()
        # 同一分片两次尝试：attempt 1 失败且其替换片段也失败，
        # 回退到成功的 attempt 0。
        collector.report_attempt(attempt("s1", 0, 0, 10, "OLD-OK"))
        collector.report_attempt(attempt("s1", 1, 0, 10, "RETRY", success=False))
        collector.report_replacement(
            Replacement("s1", 1, [("s2", 2), ("s3", 2)])
        )
        collector.report_attempt(attempt("s2", 2, 0, 4, "P1", success=False))
        collector.report_attempt(attempt("s3", 2, 4, 10, "P2"))

        result = collector.merge()
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.used_segments, [("s1", 0)])
        self.assertEqual(result.full_transform([]), ["OLD-OK"])

    def test_all_attempts_failed_is_failed_status(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 5, "A0", success=False))
        collector.report_attempt(attempt("s1", 1, 0, 5, "A1", success=False))
        result = collector.merge()
        self.assertEqual(result.status, "failed")
        self.assertIn(("s1", 1), result.failures)

    def test_nested_replacement_resolves_recursively(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 10, "OLD"))
        # 第一层拆 [0,6) [6,10)
        collector.report_replacement(
            Replacement("s1", 0, [("s2", 1), ("s3", 1)])
        )
        collector.report_attempt(attempt("s2", 1, 0, 6, "MID"))
        collector.report_attempt(attempt("s3", 1, 6, 10, "R"))
        # 第二层把 [0,6) 再拆 [0,2) [2,6)
        collector.report_replacement(
            Replacement("s2", 1, [("s4", 2), ("s5", 2)])
        )
        collector.report_attempt(attempt("s4", 2, 0, 2, "L1"))
        collector.report_attempt(attempt("s5", 2, 2, 6, "L2"))

        result = collector.merge()
        self.assertEqual(result.status, "ready")
        self.assertEqual(
            result.used_segments, [("s4", 2), ("s5", 2), ("s3", 1)]
        )
        self.assertEqual(result.full_transform([]), ["L1", "L2", "R"])

    def test_nested_replacement_failure_falls_back_level_by_level(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 10, "OLD-OK"))
        collector.report_replacement(
            Replacement("s1", 0, [("s2", 1), ("s3", 1)])
        )
        collector.report_attempt(attempt("s2", 1, 0, 6, "MID-OK"))
        collector.report_attempt(attempt("s3", 1, 6, 10, "R"))
        # 第二层替换的一个片段失败 -> 第二层不启用，回退到 s2@1。
        collector.report_replacement(
            Replacement("s2", 1, [("s4", 2), ("s5", 2)])
        )
        collector.report_attempt(attempt("s4", 2, 0, 2, "L1", success=False))
        collector.report_attempt(attempt("s5", 2, 2, 6, "L2"))

        result = collector.merge()
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.used_segments, [("s2", 1), ("s3", 1)])
        self.assertEqual(result.full_transform([]), ["MID-OK", "R"])


class StructuralValidationTests(unittest.TestCase):
    def test_duplicate_segment_rejected(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 10, "OLD"))
        with self.assertRaises(ValueError):
            collector.report_replacement(
                Replacement("s1", 0, [("s2", 1), ("s2", 1)])
            )

    def test_replacement_before_old_attempt_rejected(self):
        collector = Collector()
        with self.assertRaises(ValueError):
            collector.report_replacement(
                Replacement("s1", 0, [("s2", 1)])
            )

    def test_segments_must_tile_old_interval(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 10, "OLD"))
        collector.report_replacement(
            Replacement("s1", 0, [("s2", 1), ("s3", 1)])
        )
        collector.report_attempt(attempt("s2", 1, 0, 3, "P1"))
        # 片段到齐即校验：s3 与 s2 之间留洞，上报时直接拒绝。
        with self.assertRaises(ValueError):
            collector.report_attempt(attempt("s3", 1, 5, 10, "P2"))

    def test_segment_out_of_bounds_rejected_on_registration(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 10, "OLD"))
        collector.report_replacement(
            Replacement("s1", 0, [("s2", 1)])
        )
        with self.assertRaises(ValueError):
            collector.report_attempt(attempt("s2", 1, 0, 12, "P1"))

    def test_overlapping_top_level_intervals_rejected(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 6, "A"))
        collector.report_attempt(attempt("s2", 0, 4, 10, "B"))
        with self.assertRaises(ValueError):
            collector.merge()

    def test_circular_replacement_rejected(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 5, "A"))
        collector.report_attempt(attempt("s2", 0, 0, 5, "B"))
        collector.report_replacement(Replacement("s1", 0, [("s2", 0)]))
        collector.report_replacement(Replacement("s2", 0, [("s1", 0)]))
        with self.assertRaises(ValueError):
            collector.merge()

    def test_duplicate_replacement_rejected(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 5, "A"))
        collector.report_replacement(Replacement("s1", 0, [("s2", 1)]))
        with self.assertRaises(ValueError):
            collector.report_replacement(Replacement("s1", 0, [("s3", 1)]))


class IdempotencyAndConflictTests(unittest.TestCase):
    def test_identical_re_report_is_idempotent(self):
        collector = Collector()
        first = attempt("s1", 0, 0, 5, "A")
        collector.report_attempt(first)
        collector.report_attempt(attempt("s1", 0, 0, 5, "A"))
        result = collector.merge()
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.used_segments, [("s1", 0)])

    def test_conflicting_interval_re_report_rejected(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 5, "A"))
        with self.assertRaises(ValueError):
            collector.report_attempt(attempt("s1", 0, 1, 6, "A"))

    def test_conflicting_success_flag_re_report_rejected(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 5, "A", success=True))
        with self.assertRaises(ValueError):
            collector.report_attempt(
                attempt("s1", 0, 0, 5, "A", success=False)
            )


class OrderingSemanticsTests(unittest.TestCase):
    def test_application_follows_interval_order_not_arrival_order(self):
        # 用非交换变换证明：即使后段先到，状态叠加仍按区间从前往后。
        collector = Collector()
        collector.report_attempt(attempt("right", 0, 5, 10, "2nd"))
        collector.report_attempt(attempt("left", 0, 0, 5, "1st"))
        result = collector.merge()
        self.assertEqual(result.used_segments, [("left", 0), ("right", 0)])
        self.assertEqual(result.full_transform([]), ["1st", "2nd"])

    def test_multi_shard_split_preserves_global_order(self):
        collector = Collector()
        # 两个根分片，各自发生一次重试拆分。
        collector.report_attempt(attempt("a", 0, 0, 6, "A0"))
        collector.report_attempt(attempt("b", 0, 6, 12, "B0"))
        collector.report_replacement(
            Replacement("a", 0, [("a1", 1), ("a2", 1)])
        )
        collector.report_replacement(
            Replacement("b", 0, [("b1", 1), ("b2", 1)])
        )
        # 乱序上报所有片段。
        collector.report_attempt(attempt("b2", 1, 9, 12, "b2"))
        collector.report_attempt(attempt("a2", 1, 3, 6, "a2"))
        collector.report_attempt(attempt("b1", 1, 6, 9, "b1"))
        collector.report_attempt(attempt("a1", 1, 0, 3, "a1"))

        result = collector.merge()
        self.assertEqual(result.status, "ready")
        self.assertEqual(
            result.used_segments,
            [("a1", 1), ("a2", 1), ("b1", 1), ("b2", 1)],
        )
        self.assertEqual(
            result.full_transform([]), ["a1", "a2", "b1", "b2"]
        )

    def test_result_is_dataclass_with_expected_fields(self):
        result = Collector().merge()
        self.assertIsInstance(result, MergeResult)
        for field_name in (
            "status",
            "full_transform",
            "used_segments",
            "missing",
            "gap",
            "failures",
        ):
            self.assertTrue(hasattr(result, field_name))


class InterleavingAndEdgeTests(unittest.TestCase):
    def test_waiting_shard_and_fallback_shard_interleave(self):
        # 三个根分片交错：A 等缺失片段、B 失败回退、C 正常。
        collector = Collector()

        # A：重试替换缺一个片段 -> 整体 waiting
        collector.report_attempt(attempt("A", 0, 0, 4, "A-OLD"))
        collector.report_replacement(
            Replacement("A", 0, [("A1", 1), ("A2", 1)])
        )
        collector.report_attempt(attempt("A1", 1, 0, 2, "A1"))

        # B：片段到齐但失败，旧分片成功 -> 回退
        collector.report_attempt(attempt("B", 0, 4, 8, "B-OLD"))
        collector.report_replacement(
            Replacement("B", 0, [("B1", 1), ("B2", 1)])
        )
        collector.report_attempt(attempt("B1", 1, 4, 6, "B1", success=False))
        collector.report_attempt(attempt("B2", 1, 6, 8, "B2"))

        # C：普通成功分片
        collector.report_attempt(attempt("C", 0, 8, 10, "C"))

        result = collector.merge()
        self.assertEqual(result.status, "waiting")
        self.assertEqual(result.missing, [Interval(2, 4)])
        # 等待期间不输出任何片段（包括已就绪的 B、C）。
        self.assertEqual(result.used_segments, [])

        # 补齐 A 的缺失片段后，三个分片全部就绪且顺序正确。
        collector.report_attempt(attempt("A2", 1, 2, 4, "A2"))
        result = collector.merge()
        self.assertEqual(result.status, "ready")
        self.assertEqual(
            result.used_segments,
            [("A1", 1), ("A2", 1), ("B", 0), ("C", 0)],
        )
        self.assertEqual(
            result.full_transform([]), ["A1", "A2", "B-OLD", "C"]
        )

    def test_retry_shard_can_be_retried_again(self):
        # 替换的目标片段自身又发生重试（两层不同分片的连续重试）。
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 10, "OLD"))
        collector.report_replacement(
            Replacement("s1", 0, [("s2", 1), ("s3", 1)])
        )
        collector.report_attempt(attempt("s2", 1, 0, 6, "MID"))
        collector.report_attempt(attempt("s3", 1, 6, 10, "TAIL"))
        collector.report_replacement(
            Replacement("s2", 1, [("s4", 2), ("s5", 2)])
        )
        collector.report_attempt(attempt("s4", 2, 0, 2, "LEFT"))
        collector.report_attempt(attempt("s5", 2, 2, 6, "RIGHT"))
        # s4 再次重试拆两段。
        collector.report_replacement(
            Replacement("s4", 2, [("s6", 3), ("s7", 3)])
        )
        collector.report_attempt(attempt("s6", 3, 0, 1, "X"))
        collector.report_attempt(attempt("s7", 3, 1, 2, "Y"))

        result = collector.merge()
        self.assertEqual(result.status, "ready")
        self.assertEqual(
            result.used_segments,
            [("s6", 3), ("s7", 3), ("s5", 2), ("s3", 1)],
        )
        self.assertEqual(
            result.full_transform([]), ["X", "Y", "RIGHT", "TAIL"]
        )

    def test_adjacent_missing_intervals_are_merged(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 2, "A"))
        collector.report_attempt(attempt("s2", 0, 8, 10, "D"))
        result = collector.merge()
        # 只有一个连续缺口 [2,8)。
        self.assertEqual(result.missing, [Interval(2, 8)])

    def test_transform_does_not_mutate_input_state(self):
        collector = Collector()
        collector.report_attempt(attempt("s1", 0, 0, 4, "A"))
        collector.report_attempt(attempt("s2", 0, 4, 8, "B"))
        result = collector.merge()
        state = ["seed"]
        snapshot = list(state)
        output = result.full_transform(state)
        self.assertEqual(state, snapshot)  # 原状态不被修改
        self.assertEqual(output, ["seed", "A", "B"])

    def test_replacement_with_no_success_anywhere_fails(self):
        # 旧分片失败、重试片段也失败、且没有更旧的成功尝试 -> failed。
        collector = Collector()
        collector.report_attempt(attempt("s1", 1, 0, 5, "R", success=False))
        collector.report_replacement(
            Replacement("s1", 1, [("s2", 2), ("s3", 2)])
        )
        collector.report_attempt(attempt("s2", 2, 0, 2, "P1", success=False))
        collector.report_attempt(attempt("s3", 2, 2, 5, "P2", success=False))
        result = collector.merge()
        self.assertEqual(result.status, "failed")
        self.assertIn(("s1", 1), result.failures)
        self.assertEqual(result.used_segments, [])


if __name__ == "__main__":
    unittest.main()
