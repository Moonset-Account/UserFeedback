"""可校对时钟源的三态判定测试：确定守约、确定违约、误差带内不确定。"""

import unittest

from sla_accounting.clocks import (
    ClockSource,
    Ternary,
    compare,
    interval_contains,
    interval_overlaps,
    meets_deadline,
)


class ClockSourceTest(unittest.TestCase):
    def test_negative_epsilon_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClockSource("bad", epsilon=-0.1)

    def test_observation_interval_centers_on_reading(self) -> None:
        clk = ClockSource("c", epsilon=0.5)
        obs = clk.observe(10.0)
        self.assertEqual(obs.as_interval(), (9.5, 10.5))

    def test_zero_epsilon_gives_exact_point_interval(self) -> None:
        clk = ClockSource("exact", epsilon=0.0)
        self.assertEqual(clk.observe(3.0).as_interval(), (3.0, 3.0))


class CompareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.src = ClockSource("src", epsilon=0.2)
        self.dst = ClockSource("dst", epsilon=0.5)

    def test_definitely_before(self) -> None:
        # 事件最大可能时刻 10.2 <= 目标最小可能时刻 11.5
        event = self.src.observe(10.0)
        target = self.dst.observe(12.0)
        self.assertIs(compare(event, target), Ternary.TRUE)

    def test_definitely_after(self) -> None:
        # 事件最小可能时刻 14.8 > 目标最大可能时刻 12.5
        event = self.src.observe(15.0)
        target = self.dst.observe(12.0)
        self.assertIs(compare(event, target), Ternary.FALSE)

    def test_intervals_overlap_is_uncertain(self) -> None:
        # 事件 [9.8, 10.2]，目标 [9.5, 10.5]，相交 -> 无法确定
        event = self.src.observe(10.0)
        target = self.dst.observe(10.0)
        self.assertIs(compare(event, target), Ternary.UNCERTAIN)

    def test_touching_intervals_are_definitely_before(self) -> None:
        # 事件 hi 恰好等于目标 lo，闭区间相切 -> 确定不晚于
        event = self.src.observe(9.8)   # [9.6, 10.0]
        target = self.dst.observe(10.5)  # [10.0, 11.0]
        self.assertIs(compare(event, target), Ternary.TRUE)

    def test_exact_clocks_never_uncertain(self) -> None:
        exact = ClockSource("exact", epsilon=0.0)
        self.assertIs(
            compare(exact.observe(1.0), exact.observe(2.0)), Ternary.TRUE
        )
        self.assertIs(
            compare(exact.observe(2.0), exact.observe(1.0)), Ternary.FALSE
        )


class DeadlineTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每跳起点 100，截止 102，b 时钟 epsilon=0.5
        self.clk = ClockSource("b", epsilon=0.5)
        self.deadline = self.clk.observe(102.0)  # [101.5, 102.5]

    def test_definitely_meets_deadline(self) -> None:
        # 结算 [100.8, 101.8]，hi 101.8 <= 101.5? 否；调整为 101.0 读数
        finish = self.clk.observe(101.0)  # [100.5, 101.5]
        self.assertIs(meets_deadline(finish, self.deadline), Ternary.TRUE)

    def test_definitely_misses_deadline(self) -> None:
        # 结算 [103.0, 104.0]，lo 103.0 > 102.5 -> 确定违约
        finish = self.clk.observe(103.5)
        self.assertIs(meets_deadline(finish, self.deadline), Ternary.FALSE)

    def test_uncertain_inside_error_band(self) -> None:
        # 结算 [101.7, 102.7] 与截止 [101.5, 102.5] 相交 -> 可能违约
        finish = self.clk.observe(102.2)
        self.assertIs(meets_deadline(finish, self.deadline), Ternary.UNCERTAIN)

    def test_cross_clock_sources_uncertainty_adds(self) -> None:
        # 结算来自 epsilon=0.2 的时钟，截止来自 epsilon=0.5 的时钟
        finish_clk = ClockSource("a", epsilon=0.2)
        # 读数差 0.6，总误差带 0.7 -> 相交 -> UNCERTAIN
        finish = finish_clk.observe(102.6)   # [102.4, 102.8]
        self.assertIs(meets_deadline(finish, self.deadline), Ternary.UNCERTAIN)
        # 读数差 0.8 > 0.7 -> 确定违约
        finish_late = finish_clk.observe(102.8)  # [102.6, 103.0]
        self.assertIs(meets_deadline(finish_late, self.deadline), Ternary.FALSE)


class IntervalHelperTest(unittest.TestCase):
    def test_contains_closed_boundary(self) -> None:
        self.assertTrue(interval_contains((1.0, 2.0), 1.0))
        self.assertTrue(interval_contains((1.0, 2.0), 2.0))
        self.assertFalse(interval_contains((1.0, 2.0), 2.1))

    def test_overlaps_including_touch(self) -> None:
        self.assertTrue(interval_overlaps((1.0, 2.0), (2.0, 3.0)))
        self.assertFalse(interval_overlaps((1.0, 2.0), (2.1, 3.0)))


if __name__ == "__main__":
    unittest.main()
