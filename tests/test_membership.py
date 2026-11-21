"""MembershipManager 的 unittest 测试。

覆盖三类场景：
* 正常场景：题面三接收者聚合、纯 INCLUDE 合并、模式切换、本地投递；
* 边界场景：空 INCLUDE、最后一人离开导致撤销、重复提交无净变化、
  离开不存在的接收者、参数校验、多接口/多组隔离；
* 交错场景：记录级/逐源到期、同一时刻多个到期项只发一条净变化、
  到期两阶段（先移除后发布，不产生中间状态）、日志清空。

所有测试使用 int 虚拟时钟，不依赖真实系统时间。
"""

import unittest

from multicast_membership import (
    Filter,
    FilterMode,
    MembershipManager,
    SubscriptionUpdate,
)

INCLUDE = FilterMode.INCLUDE
EXCLUDE = FilterMode.EXCLUDE


class FilterDataClassTest(unittest.TestCase):
    def test_sources_converted_to_frozenset(self):
        f = Filter(INCLUDE, ["s1", "s2"])
        self.assertEqual(f.sources, frozenset({"s1", "s2"}))
        self.assertIsInstance(f.sources, frozenset)

    def test_frozen_and_wants(self):
        include = Filter(INCLUDE, {"a"})
        exclude = Filter(EXCLUDE, {"a"})
        self.assertTrue(include.wants("a"))
        self.assertFalse(include.wants("z"))
        self.assertFalse(exclude.wants("a"))
        self.assertTrue(exclude.wants("z"))
        with self.assertRaises(Exception):
            include.sources = frozenset()  # type: ignore[misc]

    def test_source_expiry_must_be_subset(self):
        with self.assertRaises(ValueError):
            Filter(INCLUDE, {"a"}, source_expiry={"b": 10})

    def test_bad_mode(self):
        with self.assertRaises(TypeError):
            Filter("include", {"a"})  # type: ignore[arg-type]


class HeadlineScenarioTest(unittest.TestCase):
    """题面：两人分别排除 {a,b} 与 {b,c}，第三人明确包含 b。"""

    def setUp(self):
        self.mgr = MembershipManager()
        self.iface = "eth0"
        self.group = "239.1.1.1"
        self.u1 = self.mgr.submit_filter(
            "r1", self.iface, self.group, EXCLUDE, {"a", "b"}
        )
        self.u2 = self.mgr.submit_filter(
            "r2", self.iface, self.group, EXCLUDE, {"b", "c"}
        )
        self.u3 = self.mgr.submit_filter(
            "r3", self.iface, self.group, INCLUDE, {"b"}
        )

    def test_each_submission_emits_when_aggregate_changes(self):
        self.assertIsNotNone(self.u1)
        self.assertIsNotNone(self.u2)
        self.assertIsNotNone(self.u3)

    def test_upstream_receives_all_sources_as_empty_exclude(self):
        # EXCLUDE 交集 {b} 减去 INCLUDE 并集 {b} -> EXCLUDE 空集 = 所有源
        upstream = self.mgr.upstream_filter(self.iface, self.group)
        self.assertEqual(
            upstream, Filter(EXCLUDE, frozenset())
        )
        for source in ("a", "b", "c", "d", "anything"):
            self.assertTrue(
                self.mgr.upstream_wants(self.iface, self.group, source)
            )

    def test_local_delivery_b_only_to_third_receiver(self):
        self.assertEqual(
            self.mgr.receivers_for(self.iface, self.group, "b"), ("r3",)
        )

    def test_local_delivery_other_sources(self):
        # a：r1 排除，r2 未排除 -> 仅 r2；r3 只含 b
        self.assertEqual(
            self.mgr.receivers_for(self.iface, self.group, "a"), ("r2",)
        )
        # c：r2 排除，r1 未排除 -> 仅 r1
        self.assertEqual(
            self.mgr.receivers_for(self.iface, self.group, "c"), ("r1",)
        )
        # 未出现在任何列表中的源 d：两名 EXCLUDE 接收者都要，r3 不要
        self.assertEqual(
            self.mgr.receivers_for(self.iface, self.group, "d"),
            ("r1", "r2"),
        )

    def test_last_update_carries_exclude_empty_mode(self):
        last = self.mgr.updates[-1]
        self.assertIsInstance(last, SubscriptionUpdate)
        self.assertEqual(last.mode, EXCLUDE)
        self.assertEqual(last.sources, frozenset())
        self.assertFalse(last.withdrawn)

    def test_third_receiver_leaving_restores_exclude_intersection(self):
        update = self.mgr.leave("r3", self.iface, self.group)
        self.assertIsNotNone(update)
        self.assertEqual(
            self.mgr.upstream_filter(self.iface, self.group),
            Filter(EXCLUDE, frozenset({"b"})),
        )
        # b 现在对两名 EXCLUDE 接收者都被排除
        self.assertEqual(
            self.mgr.receivers_for(self.iface, self.group, "b"), ()
        )
        # d 仍然两人都收
        self.assertEqual(
            self.mgr.receivers_for(self.iface, self.group, "d"),
            ("r1", "r2"),
        )


class IncludeOnlyAggregationTest(unittest.TestCase):
    def setUp(self):
        self.mgr = MembershipManager()
        self.ig = ("eth0", "g")

    def test_union_of_include_lists(self):
        u1 = self.mgr.submit_filter("r1", *self.ig, INCLUDE, {"a", "b"})
        u2 = self.mgr.submit_filter("r2", *self.ig, INCLUDE, {"b", "c"})
        self.assertEqual(u1.mode, INCLUDE)
        self.assertEqual(u1.sources, frozenset({"a", "b"}))
        self.assertEqual(u2.sources, frozenset({"a", "b", "c"}))
        self.assertEqual(
            self.mgr.upstream_filter(*self.ig),
            Filter(INCLUDE, frozenset({"a", "b", "c"})),
        )
        self.assertTrue(self.mgr.upstream_wants(*self.ig, "a"))
        self.assertFalse(self.mgr.upstream_wants(*self.ig, "z"))
        self.assertEqual(
            self.mgr.receivers_for(*self.ig, "b"), ("r1", "r2")
        )
        self.assertEqual(
            self.mgr.receivers_for(*self.ig, "c"), ("r2",)
        )

    def test_shrinking_include_list_emits_update(self):
        self.mgr.submit_filter("r1", *self.ig, INCLUDE, {"a", "b"})
        update = self.mgr.submit_filter("r1", *self.ig, INCLUDE, {"a"})
        self.assertEqual(update.sources, frozenset({"a"}))

    def test_empty_include_is_kept_and_published(self):
        update = self.mgr.submit_filter("r1", *self.ig, INCLUDE, set())
        self.assertEqual(update.mode, INCLUDE)
        self.assertEqual(update.sources, frozenset())
        self.assertEqual(
            self.mgr.upstream_filter(*self.ig), Filter(INCLUDE, frozenset())
        )
        self.assertFalse(self.mgr.upstream_wants(*self.ig, "a"))
        self.assertEqual(self.mgr.receivers_for(*self.ig, "a"), ())


class ModeTransitionTest(unittest.TestCase):
    def setUp(self):
        self.mgr = MembershipManager()
        self.ig = ("eth0", "g")

    def test_include_to_exclude_and_back(self):
        self.mgr.submit_filter("r1", *self.ig, INCLUDE, {"a"})
        to_exclude = self.mgr.submit_filter(
            "r1", *self.ig, EXCLUDE, {"b", "c"}
        )
        self.assertEqual(to_exclude.mode, EXCLUDE)
        self.assertEqual(to_exclude.sources, frozenset({"b", "c"}))
        self.assertTrue(self.mgr.upstream_wants(*self.ig, "a"))
        self.assertFalse(self.mgr.upstream_wants(*self.ig, "b"))

        back = self.mgr.submit_filter("r1", *self.ig, INCLUDE, {"a"})
        self.assertEqual(back.mode, INCLUDE)
        self.assertEqual(back.sources, frozenset({"a"}))

    def test_mixed_modes_exclusion_removes_included_sources(self):
        self.mgr.submit_filter("r1", *self.ig, EXCLUDE, {"a", "b"})
        update = self.mgr.submit_filter("r2", *self.ig, INCLUDE, {"a"})
        # 排除集合 {a,b} - 包含并集 {a} = {b}
        self.assertEqual(update.mode, EXCLUDE)
        self.assertEqual(update.sources, frozenset({"b"}))
        # a 虽被 r1 排除，但 r2 明确包含 -> 上游仍收 a
        self.assertTrue(self.mgr.upstream_wants(*self.ig, "a"))
        self.assertFalse(self.mgr.upstream_wants(*self.ig, "b"))
        self.assertEqual(self.mgr.receivers_for(*self.ig, "a"), ("r2",))


class NoNetChangeTest(unittest.TestCase):
    def setUp(self):
        self.mgr = MembershipManager()
        self.ig = ("eth0", "g")

    def test_identical_resubmission_emits_nothing(self):
        first = self.mgr.submit_filter("r1", *self.ig, INCLUDE, {"a"})
        self.assertIsNotNone(first)
        again = self.mgr.submit_filter("r1", *self.ig, INCLUDE, {"a"})
        self.assertIsNone(again)
        self.assertEqual(len(self.mgr.updates), 1)

    def test_identical_resubmission_including_timers(self):
        self.mgr.submit_filter(
            "r1", *self.ig, INCLUDE, {"a"}, expires_at=10,
            source_expiry={"a": 7},
        )
        again = self.mgr.submit_filter(
            "r1", *self.ig, INCLUDE, {"a"}, expires_at=10,
            source_expiry={"a": 7},
        )
        self.assertIsNone(again)
        # 定时器变化即使聚合集合相同，也属于状态变化，重新提交后重新发布
        changed = self.mgr.submit_filter(
            "r1", *self.ig, INCLUDE, {"a"}, expires_at=20,
            source_expiry={"a": 7},
        )
        self.assertIsNone(changed)  # 聚合结果本身未变 -> 无订阅更新
        self.assertEqual(
            self.mgr.record_filter("r1", *self.ig).expires_at, 20
        )

    def test_leave_with_unchanged_aggregate_emits_nothing(self):
        self.mgr.submit_filter("r1", *self.ig, INCLUDE, {"a"})
        # r2 与 r1 的聚合效果完全相同（INCLUDE {a}），不产生新更新
        self.assertIsNone(
            self.mgr.submit_filter("r2", *self.ig, INCLUDE, {"a"})
        )
        self.assertEqual(len(self.mgr.updates), 1)
        # r2 离开前，本地投递能区分两名接收者
        self.assertEqual(
            self.mgr.receivers_for(*self.ig, "a"), ("r1", "r2")
        )
        # r2 离开后，r1 仍包含 a，聚合结果 INCLUDE {a} 不变
        update = self.mgr.leave("r2", *self.ig)
        self.assertIsNone(update)
        self.assertEqual(len(self.mgr.updates), 1)
        self.assertEqual(
            self.mgr.upstream_filter(*self.ig),
            Filter(INCLUDE, frozenset({"a"})),
        )
        self.assertEqual(
            self.mgr.receivers_for(*self.ig, "a"), ("r1",)
        )
        self.assertIsNone(self.mgr.leave("r2", *self.ig))  # 已离开过：幂等
        self.assertEqual(
            self.mgr.receivers_for(*self.ig, "a"), ("r1",)
        )

    def test_leave_unknown_receiver_is_noop(self):
        self.assertIsNone(self.mgr.leave("nobody", *self.ig))
        self.assertEqual(self.mgr.updates, ())


class WithdrawalTest(unittest.TestCase):
    def test_last_receiver_leave_withdraws_upstream(self):
        mgr = MembershipManager()
        mgr.submit_filter("r1", "eth0", "g", INCLUDE, {"a"})
        update = mgr.leave("r1", "eth0", "g")
        self.assertIsNotNone(update)
        self.assertTrue(update.withdrawn)
        self.assertEqual(update.mode, INCLUDE)
        self.assertEqual(update.sources, frozenset({"a"}))
        self.assertIsNone(mgr.upstream_filter("eth0", "g"))
        self.assertFalse(mgr.upstream_wants("eth0", "g", "a"))

    def test_withdraw_then_rejoin_publishes_new_update(self):
        mgr = MembershipManager()
        mgr.submit_filter("r1", "eth0", "g", EXCLUDE, {"x"})
        mgr.leave("r1", "eth0", "g")
        rejoin = mgr.submit_filter("r1", "eth0", "g", INCLUDE, {"y"})
        self.assertFalse(rejoin.withdrawn)
        self.assertEqual(rejoin.mode, INCLUDE)
        self.assertEqual(rejoin.sources, frozenset({"y"}))
        self.assertEqual(len(mgr.updates), 3)


class RecordExpiryTest(unittest.TestCase):
    def test_expired_record_removed_then_net_change_published(self):
        mgr = MembershipManager()
        mgr.submit_filter(
            "r1", "eth0", "g", INCLUDE, {"a", "b"}, expires_at=10
        )
        mgr.submit_filter("r2", "eth0", "g", INCLUDE, {"b", "c"})
        mgr.pop_updates()

        # 未到期：什么都不发生
        self.assertEqual(mgr.advance(9), [])
        self.assertIsNotNone(mgr.record_filter("r1", "eth0", "g"))

        updates = mgr.advance(10)  # 边界：expires_at == now 即到期
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].mode, INCLUDE)
        self.assertEqual(updates[0].sources, frozenset({"b", "c"}))
        self.assertIsNone(mgr.record_filter("r1", "eth0", "g"))
        # r1 已移除，b 只剩 r2
        self.assertEqual(mgr.receivers_for("eth0", "g", "a"), ())
        self.assertEqual(mgr.receivers_for("eth0", "g", "b"), ("r2",))

    def test_expiry_without_net_change_emits_nothing(self):
        mgr = MembershipManager()
        mgr.submit_filter("r1", "eth0", "g", INCLUDE, {"a"}, expires_at=5)
        mgr.submit_filter("r2", "eth0", "g", INCLUDE, {"a"})
        mgr.pop_updates()
        # r1 到期，但 r2 的 INCLUDE {a} 使聚合结果不变
        self.assertEqual(mgr.advance(5), [])
        self.assertEqual(
            mgr.upstream_filter("eth0", "g"),
            Filter(INCLUDE, frozenset({"a"})),
        )

    def test_expiry_of_last_record_withdraws(self):
        mgr = MembershipManager()
        mgr.submit_filter("r1", "eth0", "g", EXCLUDE, {"x"}, expires_at=3)
        mgr.pop_updates()
        updates = mgr.advance(4)
        self.assertEqual(len(updates), 1)
        self.assertTrue(updates[0].withdrawn)
        self.assertIsNone(mgr.upstream_filter("eth0", "g"))

    def test_multiple_records_same_instant_single_net_update(self):
        mgr = MembershipManager()
        # r1、r2 同时到期，中间状态不发布，(eth0, g) 只产生一条净变化
        mgr.submit_filter("r1", "eth0", "g", INCLUDE, {"a"}, expires_at=8)
        mgr.submit_filter("r2", "eth0", "g", INCLUDE, {"b"}, expires_at=8)
        mgr.pop_updates()
        updates = mgr.advance(8)
        self.assertEqual(len(updates), 1)
        self.assertTrue(updates[0].withdrawn)
        self.assertEqual(len(mgr.updates), 1)

    def test_two_groups_expire_together_each_gets_one_update(self):
        mgr = MembershipManager()
        mgr.submit_filter("r1", "eth0", "g1", INCLUDE, {"a"}, expires_at=8)
        mgr.submit_filter("r1", "eth0", "g2", INCLUDE, {"b"}, expires_at=8)
        mgr.pop_updates()
        updates = mgr.advance(8)
        self.assertEqual(len(updates), 2)
        self.assertEqual(
            sorted((u.group for u in updates), key=str), ["g1", "g2"]
        )
        self.assertTrue(all(u.withdrawn for u in updates))


class PerSourceExpiryTest(unittest.TestCase):
    def test_include_sources_expire_individually(self):
        mgr = MembershipManager()
        mgr.submit_filter(
            "r1", "eth0", "g",
            INCLUDE, {"a", "b", "c"},
            source_expiry={"a": 5, "b": 10},
        )
        mgr.pop_updates()

        updates = mgr.advance(5)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].sources, frozenset({"b", "c"}))
        record = mgr.record_filter("r1", "eth0", "g")
        self.assertEqual(record.sources, frozenset({"b", "c"}))
        self.assertNotIn("a", record.source_expiry)
        self.assertEqual(mgr.receivers_for("eth0", "g", "a"), ())
        self.assertEqual(mgr.receivers_for("eth0", "g", "b"), ("r1",))

        updates = mgr.advance(10)
        self.assertEqual(updates[0].sources, frozenset({"c"}))
        # 记录本身仍保留为空不了的 INCLUDE {c}
        self.assertIsNotNone(mgr.record_filter("r1", "eth0", "g"))

    def test_include_shrinks_to_empty_set_but_record_remains(self):
        mgr = MembershipManager()
        mgr.submit_filter(
            "r1", "eth0", "g", INCLUDE, {"a"}, source_expiry={"a": 2}
        )
        mgr.pop_updates()
        updates = mgr.advance(2)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].mode, INCLUDE)
        self.assertEqual(updates[0].sources, frozenset())
        self.assertIsNotNone(mgr.record_filter("r1", "eth0", "g"))
        self.assertFalse(mgr.upstream_wants("eth0", "g", "a"))

    def test_exclude_source_expiry_widens_reception(self):
        mgr = MembershipManager()
        mgr.submit_filter(
            "r1", "eth0", "g", EXCLUDE, {"a", "b"},
            source_expiry={"a": 4},
        )
        mgr.pop_updates()
        updates = mgr.advance(4)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].mode, EXCLUDE)
        self.assertEqual(updates[0].sources, frozenset({"b"}))
        # a 的排除项到期后，r1 开始接收 a
        self.assertEqual(mgr.receivers_for("eth0", "g", "a"), ("r1",))
        self.assertEqual(mgr.receivers_for("eth0", "g", "b"), ())

    def test_record_timer_and_source_timer_interleaved(self):
        mgr = MembershipManager()
        # 逐源定时器先到期（t=3），记录级定时器后到期（t=9）
        mgr.submit_filter(
            "r1", "eth0", "g", INCLUDE, {"a", "b"},
            expires_at=9, source_expiry={"a": 3},
        )
        first = mgr.advance(3)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].sources, frozenset({"b"}))

        second = mgr.advance(9)
        self.assertEqual(len(second), 1)
        self.assertTrue(second[0].withdrawn)
        self.assertIsNone(mgr.record_filter("r1", "eth0", "g"))

    def test_no_firing_before_deadline_and_repeated_advance(self):
        mgr = MembershipManager()
        mgr.submit_filter(
            "r1", "eth0", "g", INCLUDE, {"a"}, source_expiry={"a": 6}
        )
        mgr.pop_updates()
        self.assertEqual(mgr.advance(5), [])
        self.assertEqual(mgr.advance(5), [])  # 重复推进无副作用
        self.assertEqual(len(mgr.advance(6)), 1)
        self.assertEqual(mgr.advance(100), [])  # 已无源可到期


class IsolationAndValidationTest(unittest.TestCase):
    def test_interfaces_and_groups_are_independent(self):
        mgr = MembershipManager()
        mgr.submit_filter("r1", "eth0", "g1", INCLUDE, {"a"})
        mgr.submit_filter("r1", "eth1", "g1", INCLUDE, {"b"})
        mgr.submit_filter("r1", "eth0", "g2", EXCLUDE, {"c"})
        self.assertEqual(
            mgr.upstream_filter("eth0", "g1"),
            Filter(INCLUDE, frozenset({"a"})),
        )
        self.assertEqual(
            mgr.upstream_filter("eth1", "g1"),
            Filter(INCLUDE, frozenset({"b"})),
        )
        self.assertEqual(
            mgr.upstream_filter("eth0", "g2"),
            Filter(EXCLUDE, frozenset({"c"})),
        )
        self.assertEqual(mgr.receivers_for("eth0", "g1", "a"), ("r1",))
        self.assertEqual(mgr.receivers_for("eth1", "g1", "a"), ())
        update = mgr.leave("r1", "eth0", "g1")
        self.assertTrue(update.withdrawn)
        # 其他 (接口, 组) 不受影响
        self.assertIsNotNone(mgr.upstream_filter("eth1", "g1"))
        self.assertIsNotNone(mgr.upstream_filter("eth0", "g2"))

    def test_same_receiver_on_two_interfaces(self):
        mgr = MembershipManager()
        mgr.submit_filter("r1", "eth0", "g", EXCLUDE, {"x"})
        mgr.submit_filter("r1", "eth1", "g", EXCLUDE, {"x"})
        mgr.leave("r1", "eth0", "g")
        self.assertIsNone(mgr.upstream_filter("eth0", "g"))
        self.assertIsNotNone(mgr.upstream_filter("eth1", "g"))

    def test_inline_source_expiry_validation(self):
        mgr = MembershipManager()
        with self.assertRaises(ValueError):
            mgr.submit_filter(
                "r1", "eth0", "g", INCLUDE, {"a"},
                source_expiry={"b": 1},
            )

    def test_inline_bad_mode(self):
        mgr = MembershipManager()
        with self.assertRaises(TypeError):
            mgr.submit_filter("r1", "eth0", "g", "include", {"a"})  # type: ignore[arg-type]

    def test_sources_default_to_source_expiry_keys(self):
        mgr = MembershipManager()
        update = mgr.submit_filter(
            "r1", "eth0", "g", INCLUDE,
            source_expiry={"a": 1, "b": 2},
        )
        self.assertEqual(update.sources, frozenset({"a", "b"}))

    def test_non_hashable_receiver_rejected_naturally(self):
        mgr = MembershipManager()
        with self.assertRaises(TypeError):
            mgr.submit_filter(["r1"], "eth0", "g", INCLUDE, {"a"})  # type: ignore[list-item]


class UpdateLogTest(unittest.TestCase):
    def test_log_accumulates_and_pops(self):
        mgr = MembershipManager()
        mgr.submit_filter("r1", "eth0", "g", INCLUDE, {"a"})
        mgr.submit_filter("r1", "eth0", "g", INCLUDE, {"a", "b"})
        mgr.submit_filter("r1", "eth0", "g", INCLUDE, {"a", "b"})  # 无净变化
        self.assertEqual(len(mgr.updates), 2)
        drained = mgr.pop_updates()
        self.assertEqual(len(drained), 2)
        self.assertEqual(mgr.updates, ())
        # 清空后新变化继续记录
        mgr.leave("r1", "eth0", "g")
        self.assertEqual(len(mgr.updates), 1)
        self.assertTrue(mgr.updates[0].withdrawn)

    def test_update_fields_identify_interface_and_group(self):
        mgr = MembershipManager()
        update = mgr.submit_filter(
            "r1", "eth7", "g7", EXCLUDE, {"z"}
        )
        self.assertEqual(update.interface, "eth7")
        self.assertEqual(update.group, "g7")
        self.assertFalse(update.withdrawn)


if __name__ == "__main__":
    unittest.main()
