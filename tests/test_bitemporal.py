"""BitemporalTable 的 unittest 测试。

覆盖三类场景：

* 正常场景：整周录入、区间补录、区间撤销、提交视图回看、片段合并；
* 边界场景：半开区间端点、空表、as_of=0 / 未来提交号、非法参数；
* 交错场景：多主键交错提交、层层嵌套更正、撤销后再录入、相邻空档合并。

可从工作区根目录直接发现：

    python3.11 -m unittest discover -s tests
    python3.11 -m unittest discover
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from bitemporal import BitemporalTable, Segment


class CommitAndPointQueryTests(unittest.TestCase):
    """提交号分配与两点查值的正常/边界场景。"""

    def test_commit_numbers_increment_from_one(self) -> None:
        table = BitemporalTable()
        self.assertEqual(table.head, 0)
        self.assertEqual(table.commit("k", 0, 5, "A"), 1)
        self.assertEqual(table.commit("k", 5, 9, "B"), 2)
        self.assertEqual(table.retract("k", 1, 2), 3)
        self.assertEqual(table.head, 3)

    def test_half_open_interval_includes_begin_excludes_end(self) -> None:
        table = BitemporalTable()
        table.commit("k", 0, 5, "A")
        self.assertEqual(table.value_at("k", 0), "A")
        self.assertEqual(table.value_at("k", 4), "A")
        # 终点 5 不在 [0, 5) 内。
        self.assertIsNone(table.value_at("k", 5))
        self.assertIsNone(table.value_at("k", -1))

    def test_latest_visible_record_wins_inside_interval(self) -> None:
        table = BitemporalTable()
        table.commit("k", 0, 10, "A")
        table.commit("k", 2, 8, "B")
        table.commit("k", 4, 6, "C")
        self.assertEqual(table.value_at("k", 1), "A")
        self.assertEqual(table.value_at("k", 2), "B")
        self.assertEqual(table.value_at("k", 4), "C")
        self.assertEqual(table.value_at("k", 5), "C")
        self.assertEqual(table.value_at("k", 6), "B")  # C 的终点，半开
        self.assertEqual(table.value_at("k", 8), "A")  # B 的终点，半开
        self.assertEqual(table.value_at("k", 9), "A")

    def test_correction_keeps_original_value_on_uncovered_sides(self) -> None:
        table = BitemporalTable()
        table.commit("k", 0, 10, "A")
        table.commit("k", 3, 7, "B")
        # 区间左侧、右侧沿用原值 A。
        self.assertEqual(table.value_at("k", 2), "A")
        self.assertEqual(table.value_at("k", 7), "A")
        self.assertEqual(table.value_at("k", 3), "B")

    def test_value_at_without_any_fact_returns_none(self) -> None:
        table = BitemporalTable()
        self.assertIsNone(table.value_at("missing", 0))
        table.commit("k", 0, 5, "A")
        self.assertIsNone(table.value_at("other", 0))


class RetractionTests(unittest.TestCase):
    """区间撤销（墓碑）语义。"""

    def test_retract_makes_gap_even_when_older_fact_exists(self) -> None:
        # 最近一笔为撤销时该点即无事实；更早的录入只在未被后续
        # 录入（含撤销）覆盖的位置沿用。
        table = BitemporalTable()
        table.commit("k", 0, 10, "A")
        table.commit("k", 3, 7, "B")
        table.retract("k", 4, 6)
        self.assertIsNone(table.value_at("k", 4))
        self.assertIsNone(table.value_at("k", 5))
        # 撤销区间之外，最近一笔普通录入仍然有效。
        self.assertEqual(table.value_at("k", 3), "B")
        self.assertEqual(table.value_at("k", 6), "B")  # 撤销终点，半开
        self.assertEqual(table.value_at("k", 7), "A")

    def test_retract_only_affects_its_own_interval(self) -> None:
        table = BitemporalTable()
        table.commit("k", 0, 10, "A")
        table.retract("k", 2, 4)
        self.assertEqual(table.value_at("k", 1), "A")
        self.assertIsNone(table.value_at("k", 2))
        self.assertIsNone(table.value_at("k", 3))
        self.assertEqual(table.value_at("k", 4), "A")

    def test_reinsert_after_retraction_overrides_tombstone(self) -> None:
        table = BitemporalTable()
        table.commit("k", 0, 10, "A")
        table.retract("k", 2, 5)
        self.assertIsNone(table.value_at("k", 3))
        table.commit("k", 2, 5, "C")
        self.assertEqual(table.value_at("k", 3), "C")
        # 新录入区间之外，A 从未被覆盖，继续沿用。
        self.assertEqual(table.value_at("k", 1), "A")
        self.assertEqual(table.value_at("k", 5), "A")

    def test_retract_over_empty_history_creates_gap_only_inside(self) -> None:
        table = BitemporalTable()
        table.retract("k", 0, 4)
        self.assertIsNone(table.value_at("k", 0))
        self.assertIsNone(table.value_at("k", 3))
        self.assertIsNone(table.value_at("k", 4))


class SliceTests(unittest.TestCase):
    """指定提交视图下的连续片段查询、空档与相邻等值合并。"""

    def test_slice_returns_contiguous_segments_covering_whole_range(self) -> None:
        table = BitemporalTable()
        table.commit("k", 0, 10, "A")
        table.commit("k", 2, 8, "B")
        table.commit("k", 4, 6, "C")
        segments = table.slice("k", 0, 10)
        self.assertEqual(
            segments,
            [
                Segment(0, 2, "A", False),
                Segment(2, 4, "B", False),
                Segment(4, 6, "C", False),
                Segment(6, 8, "B", False),
                Segment(8, 10, "A", False),
            ],
        )
        # 首尾相接，完整覆盖查询区间。
        self.assertEqual(segments[0].begin, 0)
        self.assertEqual(segments[-1].end, 10)
        for left, right in zip(segments, segments[1:]):
            self.assertEqual(left.end, right.begin)

    def test_slice_merges_adjacent_equal_value_segments(self) -> None:
        table = BitemporalTable()
        table.commit("k", 0, 5, "A")
        table.commit("k", 5, 10, "A")  # 与前段相邻且等值
        table.commit("k", 3, 7, "A")  # 再补一笔同值，边界更多
        self.assertEqual(
            table.slice("k", 0, 10),
            [Segment(0, 10, "A", False)],
        )

    def test_slice_reports_gaps_and_merges_adjacent_gaps(self) -> None:
        table = BitemporalTable()
        table.commit("k", 2, 8, "B")
        table.retract("k", 4, 6)
        self.assertEqual(
            table.slice("k", 0, 10),
            [
                Segment(0, 2, None, True),
                Segment(2, 4, "B", False),
                Segment(4, 6, None, True),
                Segment(6, 8, "B", False),
                Segment(8, 10, None, True),
            ],
        )
        # 两笔相邻撤销产生的空档应合并为一段。
        other = BitemporalTable()
        other.retract("k", 0, 2)
        other.retract("k", 2, 4)
        self.assertEqual(other.slice("k", 0, 4), [Segment(0, 4, None, True)])

    def test_slice_empty_key_returns_single_gap_segment(self) -> None:
        table = BitemporalTable()
        self.assertEqual(table.slice("k", 0, 5), [Segment(0, 5, None, True)])

    def test_slice_subrange_clips_to_query_window(self) -> None:
        table = BitemporalTable()
        table.commit("k", 0, 10, "A")
        table.commit("k", 2, 8, "B")
        # 查询窗口只取更正区间的一部分，且跨在其边界上。
        self.assertEqual(
            table.slice("k", 3, 9),
            [Segment(3, 8, "B", False), Segment(8, 9, "A", False)],
        )
        # 窗口完全落在空档中。
        self.assertEqual(
            table.slice("k", 10, 12),
            [Segment(10, 12, None, True)],
        )


class CommitViewTests(unittest.TestCase):
    """提交视图（as_of）的时间旅行与不可变性。"""

    def test_historical_view_ignores_later_commits(self) -> None:
        table = BitemporalTable()
        table.commit("k", 0, 10, "A")  # c1
        table.commit("k", 2, 8, "B")  # c2
        table.retract("k", 4, 6)  # c3

        self.assertEqual(table.value_at("k", 5, as_of=1), "A")
        self.assertEqual(table.value_at("k", 5, as_of=2), "B")
        self.assertIsNone(table.value_at("k", 5, as_of=3))
        # 默认视图即最新视图。
        self.assertIsNone(table.value_at("k", 5))

        self.assertEqual(
            table.slice("k", 0, 10, as_of=1),
            [Segment(0, 10, "A", False)],
        )
        self.assertEqual(
            table.slice("k", 0, 10, as_of=2),
            [
                Segment(0, 2, "A", False),
                Segment(2, 8, "B", False),
                Segment(8, 10, "A", False),
            ],
        )

    def test_view_zero_and_future_commit_number(self) -> None:
        table = BitemporalTable()
        table.commit("k", 0, 5, "A")
        # as_of=0：尚无任何录入可见。
        self.assertIsNone(table.value_at("k", 0, as_of=0))
        self.assertEqual(
            table.slice("k", 0, 5, as_of=0),
            [Segment(0, 5, None, True)],
        )
        # 指向未来的提交号按当前 head 截断。
        self.assertEqual(table.value_at("k", 0, as_of=99), "A")
        self.assertEqual(
            table.slice("k", 0, 5, as_of=99),
            [Segment(0, 5, "A", False)],
        )

    def test_old_views_remain_stable_after_more_commits(self) -> None:
        table = BitemporalTable()
        table.commit("k", 0, 10, "A")
        snapshot = table.slice("k", 0, 10, as_of=1)
        for i in range(2, 8):
            table.commit("k", i, i + 1, f"v{i}")
        table.retract("k", 0, 10)
        self.assertEqual(snapshot, [Segment(0, 10, "A", False)])
        self.assertEqual(table.value_at("k", 5, as_of=1), "A")
        self.assertIsNone(table.value_at("k", 5))  # 最新视图已整段撤销


class NarrativeWeekScenarioTests(unittest.TestCase):
    """题面叙事：周一整周值、周五补录更正并撤销其中一半。

    业务周：2026-11-16（周一）至 2026-11-23（下周一），半开
    ``[MON, MON+7)``。周三时尚只有周一的录入，故“周三的录入视图”
    即 ``as_of=1``。
    """

    def setUp(self) -> None:
        self.mon = date(2026, 11, 16)
        self.tue = self.mon + timedelta(days=1)
        self.wed = self.mon + timedelta(days=2)
        self.thu = self.mon + timedelta(days=3)
        self.fri = self.mon + timedelta(days=4)
        self.next_mon = self.mon + timedelta(days=7)

        self.table = BitemporalTable()
        # 周一：录入覆盖整周的值 A。
        self.c1 = self.table.commit("k", self.mon, self.next_mon, "A")
        # 周五：补录周二生效、周四结束的更正 B（半开 [Tue, Thu)）。
        self.c2 = self.table.commit("k", self.tue, self.thu, "B")
        # 周五：再撤销更正区间的一半 [Tue, Wed)。
        self.c3 = self.table.retract("k", self.tue, self.wed)

    def test_commit_sequence(self) -> None:
        self.assertEqual((self.c1, self.c2, self.c3), (1, 2, 3))
        self.assertEqual(self.table.head, 3)

    def test_latest_view_point_values(self) -> None:
        self.assertEqual(self.table.value_at("k", self.mon), "A")
        self.assertIsNone(self.table.value_at("k", self.tue))  # 撤销 → 空档
        self.assertEqual(self.table.value_at("k", self.wed), "B")  # 撤销终点
        self.assertEqual(self.table.value_at("k", self.thu), "A")  # 更正终点
        self.assertEqual(self.table.value_at("k", self.fri), "A")
        self.assertIsNone(self.table.value_at("k", self.next_mon))  # 整周终点

    def test_latest_view_slice_of_full_week(self) -> None:
        self.assertEqual(
            self.table.slice("k", self.mon, self.next_mon),
            [
                Segment(self.mon, self.tue, "A", False),
                Segment(self.tue, self.wed, None, True),
                Segment(self.wed, self.thu, "B", False),
                Segment(self.thu, self.next_mon, "A", False),
            ],
        )

    def test_wednesday_view_still_shows_entire_old_value(self) -> None:
        # 周三的录入视图：更正与撤销都尚未发生，整周仍是连续旧值 A。
        self.assertEqual(
            self.table.slice("k", self.mon, self.next_mon, as_of=self.c1),
            [Segment(self.mon, self.next_mon, "A", False)],
        )
        for day_offset in range(7):
            day = self.mon + timedelta(days=day_offset)
            self.assertEqual(
                self.table.value_at("k", day, as_of=self.c1),
                "A",
            )

    def test_friday_pre_retraction_view_shows_full_correction(self) -> None:
        # 更正已录入、撤销尚未录入时（as_of=2），更正区间完整为 B。
        self.assertEqual(
            self.table.slice("k", self.mon, self.next_mon, as_of=self.c2),
            [
                Segment(self.mon, self.tue, "A", False),
                Segment(self.tue, self.thu, "B", False),
                Segment(self.thu, self.next_mon, "A", False),
            ],
        )
        self.assertEqual(self.table.value_at("k", self.tue, as_of=self.c2), "B")

    def test_retracted_half_is_a_gap_even_against_historical_a(self) -> None:
        # 被撤销的一半在最新视图中是空档，而不是回退成 A；
        # 另一半更正 B 保持不变。
        self.assertEqual(
            self.table.slice("k", self.tue, self.thu),
            [
                Segment(self.tue, self.wed, None, True),
                Segment(self.wed, self.thu, "B", False),
            ],
        )


class MultiKeyAndInterleavingTests(unittest.TestCase):
    """多主键隔离与交错补录/撤销。"""

    def test_keys_are_isolated_under_interleaved_commits(self) -> None:
        table = BitemporalTable()
        table.commit("k1", 0, 10, "A1")
        table.commit("k2", 0, 10, "A2")
        table.commit("k1", 2, 8, "B1")
        table.retract("k2", 4, 6)
        self.assertEqual(table.value_at("k1", 5), "B1")
        self.assertIsNone(table.value_at("k2", 5))
        self.assertEqual(table.value_at("k2", 6), "A2")
        self.assertEqual(
            table.slice("k2", 0, 10),
            [
                Segment(0, 4, "A2", False),
                Segment(4, 6, None, True),
                Segment(6, 10, "A2", False),
            ],
        )

    def test_nested_corrections_then_partial_retraction(self) -> None:
        table = BitemporalTable()
        table.commit("k", 0, 10, "A")
        table.commit("k", 2, 8, "B")
        table.commit("k", 4, 6, "C")
        table.retract("k", 3, 7)
        self.assertEqual(
            table.slice("k", 0, 10),
            [
                Segment(0, 2, "A", False),
                Segment(2, 3, "B", False),
                Segment(3, 7, None, True),
                Segment(7, 8, "B", False),
                Segment(8, 10, "A", False),
            ],
        )

    def test_records_audit_trail_is_append_only(self) -> None:
        table = BitemporalTable()
        table.commit("k", 0, 5, "A")
        table.commit("other", 0, 5, "X")
        table.retract("k", 1, 2)
        records = table.records("k")
        self.assertEqual([r.commit for r in records], [1, 3])
        self.assertFalse(records[0].retracted)
        self.assertTrue(records[1].retracted)
        self.assertEqual([r.commit for r in table.records()], [1, 2, 3])
        # 返回的是副本，外部无法篡改内部状态。
        self.assertIsInstance(table.records(), tuple)


class ValidationTests(unittest.TestCase):
    """非法参数的边界校验。"""

    def test_empty_or_inverted_interval_rejected(self) -> None:
        table = BitemporalTable()
        with self.assertRaises(ValueError):
            table.commit("k", 5, 5, "A")
        with self.assertRaises(ValueError):
            table.commit("k", 6, 5, "A")
        with self.assertRaises(ValueError):
            table.retract("k", 5, 5)
        with self.assertRaises(ValueError):
            table.retract("k", 6, 5)
        with self.assertRaises(ValueError):
            table.slice("k", 5, 5)

    def test_none_value_rejected(self) -> None:
        table = BitemporalTable()
        with self.assertRaises(ValueError):
            table.commit("k", 0, 5, None)

    def test_invalid_as_of_rejected(self) -> None:
        table = BitemporalTable()
        table.commit("k", 0, 5, "A")
        with self.assertRaises(ValueError):
            table.value_at("k", 0, as_of=-1)
        with self.assertRaises(ValueError):
            table.slice("k", 0, 5, as_of=-1)
        with self.assertRaises(TypeError):
            table.value_at("k", 0, as_of="1")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            table.value_at("k", 0, as_of=True)  # bool 不接受


if __name__ == "__main__":
    unittest.main()
