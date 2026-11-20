"""forwarding 模块的本地合成数据测试。

所有地址均取自文档保留段（192.0.2.0/24、198.51.100.0/24、
203.0.113.0/24，RFC 5737）或私有段（10.0.0.0/8），不指向任何
真实网络。

注意：按任务约束，本测试文件仅作为源码交付，尚未实际运行。
"""

import unittest

from forwarding import ForwardingTable, LookupResult


class BasicLookupTest(unittest.TestCase):
    def setUp(self):
        self.ft = ForwardingTable()
        for name in ("eth0", "eth1", "eth2"):
            self.ft.add_interface(name, enabled=True)

    def test_direct_route_lookup(self):
        self.ft.add_route(1, "192.0.2.0/24", interface="eth0")
        result = self.ft.lookup("192.0.2.10")
        self.assertIsNotNone(result)
        self.assertEqual(result.egress_interface, "eth0")
        self.assertEqual(result.route_ids, [1])
        self.assertTrue(result.chain[0].direct)

    def test_no_matching_route_returns_none(self):
        self.ft.add_route(1, "192.0.2.0/24", interface="eth0")
        self.assertIsNone(self.ft.lookup("198.51.100.1"))

    def test_recursive_route_resolves_to_direct_egress(self):
        # 递归路由：10.0.0.0/8 via 192.0.2.1
        self.ft.add_route(1, "10.0.0.0/8", next_hop="192.0.2.1")
        # 直连路由：192.0.2.0/24 -> eth1
        self.ft.add_route(2, "192.0.2.0/24", interface="eth1")
        result = self.ft.lookup("10.1.2.3")
        self.assertIsNotNone(result)
        self.assertEqual(result.egress_interface, "eth1")
        # 完整解析链：先递归路由 1，再直连路由 2。
        self.assertEqual(result.route_ids, [1, 2])
        self.assertFalse(result.chain[0].direct)
        self.assertTrue(result.chain[1].direct)

    def test_multi_level_recursion(self):
        self.ft.add_route(1, "10.0.0.0/8", next_hop="198.51.100.1")
        self.ft.add_route(2, "198.51.100.0/24", next_hop="203.0.113.1")
        self.ft.add_route(3, "203.0.113.0/24", interface="eth2")
        result = self.ft.lookup("10.9.9.9")
        self.assertIsNotNone(result)
        self.assertEqual(result.egress_interface, "eth2")
        self.assertEqual(result.route_ids, [1, 2, 3])


class CandidateOrderingTest(unittest.TestCase):
    def setUp(self):
        self.ft = ForwardingTable()
        for name in ("eth0", "eth1", "eth2", "eth3"):
            self.ft.add_interface(name, enabled=True)

    def test_longest_prefix_wins(self):
        self.ft.add_route(1, "192.0.2.0/24", interface="eth0")
        self.ft.add_route(2, "192.0.2.128/25", interface="eth1")
        result = self.ft.lookup("192.0.2.200")
        self.assertEqual(result.egress_interface, "eth1")
        result = self.ft.lookup("192.0.2.10")
        self.assertEqual(result.egress_interface, "eth0")

    def test_lower_metric_wins_on_same_prefix(self):
        self.ft.add_route(1, "192.0.2.0/24", metric=10, interface="eth0")
        self.ft.add_route(2, "192.0.2.0/24", metric=5, interface="eth1")
        result = self.ft.lookup("192.0.2.1")
        self.assertEqual(result.egress_interface, "eth1")
        self.assertEqual(result.route_ids, [2])

    def test_smaller_route_id_wins_on_same_prefix_and_metric(self):
        self.ft.add_route(7, "192.0.2.0/24", metric=5, interface="eth0")
        self.ft.add_route(3, "192.0.2.0/24", metric=5, interface="eth1")
        result = self.ft.lookup("192.0.2.1")
        self.assertEqual(result.route_ids, [3])

    def test_fallback_to_next_candidate_when_preferred_interface_down(self):
        self.ft.add_route(1, "192.0.2.0/24", metric=5, interface="eth0")
        self.ft.add_route(2, "192.0.2.0/24", metric=10, interface="eth1")
        self.ft.set_interface_enabled("eth0", False)
        result = self.ft.lookup("192.0.2.1")
        self.assertIsNotNone(result)
        self.assertEqual(result.egress_interface, "eth1")
        self.assertEqual(result.route_ids, [2])


class TopologyChangeTest(unittest.TestCase):
    def setUp(self):
        self.ft = ForwardingTable()
        for name in ("eth0", "eth1"):
            self.ft.add_interface(name, enabled=True)
        self.ft.add_route(1, "10.0.0.0/8", next_hop="192.0.2.1")
        self.ft.add_route(2, "192.0.2.0/24", interface="eth0")

    def test_interface_down_breaks_recursive_resolution(self):
        self.ft.set_interface_enabled("eth0", False)
        self.assertIsNone(self.ft.lookup("10.1.1.1"))

    def test_interface_up_restores_resolution(self):
        self.ft.set_interface_enabled("eth0", False)
        self.assertIsNone(self.ft.lookup("10.1.1.1"))
        self.ft.set_interface_enabled("eth0", True)
        result = self.ft.lookup("10.1.1.1")
        self.assertIsNotNone(result)
        self.assertEqual(result.egress_interface, "eth0")
        self.assertEqual(result.route_ids, [1, 2])

    def test_route_withdrawal_changes_result(self):
        self.ft.remove_route(2)
        self.assertIsNone(self.ft.lookup("10.1.1.1"))

    def test_route_insertion_changes_result(self):
        self.assertIsNone(self.ft.lookup("198.51.100.7"))
        self.ft.add_route(3, "198.51.100.0/24", interface="eth1")
        result = self.ft.lookup("198.51.100.7")
        self.assertIsNotNone(result)
        self.assertEqual(result.egress_interface, "eth1")

    def test_new_egress_reflected_in_chain_after_change(self):
        self.ft.set_interface_enabled("eth0", False)
        self.ft.add_route(3, "192.0.2.0/24", metric=20, interface="eth1")
        result = self.ft.lookup("10.1.1.1")
        self.assertIsNotNone(result)
        self.assertEqual(result.egress_interface, "eth1")
        self.assertEqual(result.route_ids, [1, 3])


class MutualRecursionLoopTest(unittest.TestCase):
    """两条优先路由互指成环，其中一端另有可用的较短前缀直连路由。

    拓扑（全部合成地址）：

    * 路由 1：10.0.0.0/8      via 192.0.2.1   （metric 5，递归）
    * 路由 2：192.0.2.0/24    via 10.1.2.3    （metric 5，递归）
    * 路由 3：192.0.0.0/16    -> eth9         （metric 5，直连，较短前缀）

    从 10.x 查询：1 -> 2 ->（回到 10.x 时 1 已在链上被排除）失败，
    回溯后在 192.0.2.1 这一层改试路由 3，落到 eth9。
    从 192.0.2.x 查询：2 -> 1 ->（回到 192.0.2.1 时 2 已被排除）改试
    路由 3，同样落到 eth9。
    """

    def setUp(self):
        self.ft = ForwardingTable()
        self.ft.add_interface("eth9", enabled=True)
        self.ft.add_route(1, "10.0.0.0/8", metric=5, next_hop="192.0.2.1")
        self.ft.add_route(2, "192.0.2.0/24", metric=5, next_hop="10.1.2.3")
        self.ft.add_route(3, "192.0.0.0/16", metric=5, interface="eth9")

    def test_query_from_first_end_finds_egress(self):
        result = self.ft.lookup("10.7.7.7")
        self.assertIsNotNone(result)
        self.assertEqual(result.egress_interface, "eth9")
        # 链：1（递归）-> 2（递归）-> 3（直连兜底）。
        self.assertEqual(result.route_ids, [1, 2, 3])

    def test_query_from_second_end_finds_egress(self):
        result = self.ft.lookup("192.0.2.55")
        self.assertIsNotNone(result)
        self.assertEqual(result.egress_interface, "eth9")
        # 链：2（递归）-> 1（递归）-> 3（直连兜底）。
        self.assertEqual(result.route_ids, [2, 1, 3])

    def test_loop_in_previous_query_does_not_blacklist_route(self):
        # 先构造一次必然失败的查询：把兜底接口关掉，两端都成死环。
        self.ft.set_interface_enabled("eth9", False)
        self.assertIsNone(self.ft.lookup("10.7.7.7"))
        self.assertIsNone(self.ft.lookup("192.0.2.55"))
        # 恢复接口后再次查询：路由 1、2 虽在上一次查询中参与成环，
        # 但不得被永久屏蔽，两端都应重新找到出口。
        self.ft.set_interface_enabled("eth9", True)
        r1 = self.ft.lookup("10.7.7.7")
        r2 = self.ft.lookup("192.0.2.55")
        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)
        self.assertEqual(r1.egress_interface, "eth9")
        self.assertEqual(r2.egress_interface, "eth9")
        self.assertEqual(r1.route_ids, [1, 2, 3])
        self.assertEqual(r2.route_ids, [2, 1, 3])

    def test_pure_loop_without_escape_returns_none(self):
        self.ft.remove_route(3)
        self.assertIsNone(self.ft.lookup("10.7.7.7"))
        self.assertIsNone(self.ft.lookup("192.0.2.55"))


class ValidationTest(unittest.TestCase):
    def test_route_must_be_direct_or_recursive(self):
        ft = ForwardingTable()
        with self.assertRaises(ValueError):
            ft.add_route(1, "192.0.2.0/24")
        with self.assertRaises(ValueError):
            ft.add_route(2, "192.0.2.0/24", interface="eth0",
                         next_hop="192.0.2.1")

    def test_unregistered_interface_treated_as_down(self):
        ft = ForwardingTable()
        ft.add_route(1, "192.0.2.0/24", interface="ghost0")
        self.assertIsNone(ft.lookup("192.0.2.1"))


if __name__ == "__main__":
    unittest.main()
