"""taint_ledger 验收测试（unittest）。

覆盖：
  * 可接受结果：合法流水下的上下界与逐笔分配（含分配可行性交叉校验）；
  * 应被阻止 / 拒绝的结果：透支、非法金额、重复标记、非法查询等；
  * 边界与交错场景：两类资金混合时的部分边界、转账后回流按先后处理、
    涉案入账之前的转账不得携带涉案资金、逐笔最大值不可直接相加。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from taint_ledger import Bounds, Ledger, LedgerError  # noqa: E402


def assert_allocation_valid(tc, ledger, allocation, selected, expected_sum):
    """交叉校验一份逐笔分配：重放流水，验证余额约束与逐笔守恒。

    逐笔检查：转账的涉案部分不超过面额、转出方涉案余额足够、
    转出方普通余额足够（普通资金不透支）、总余额不透支；
    最后核对所选组的涉案金额总和等于期望边界值。
    """
    balances = dict(ledger.initial_balances)
    tainted = {a: 0 for a in ledger.accounts}
    for a in ledger.accounts:
        balances.setdefault(a, 0)
    total = 0
    for rec in ledger.records:
        if rec.kind == "deposit":
            balances[rec.target] += rec.amount
            if rec.marked:
                tainted[rec.target] += rec.amount
            continue
        tc.assertIn(rec.tx_id, allocation, "分配必须覆盖每一笔转账记录")
        t = allocation[rec.tx_id]
        tc.assertIsInstance(t, int)
        tc.assertGreaterEqual(t, 0, "涉案金额不能为负")
        tc.assertLessEqual(t, rec.amount, "涉案金额不能超过转账面额")
        tc.assertLessEqual(t, tainted[rec.source], "转出方涉案余额不足")
        tc.assertLessEqual(
            rec.amount - t,
            balances[rec.source] - tainted[rec.source],
            "转出方普通资金余额不足",
        )
        tainted[rec.source] -= t
        tainted[rec.target] += t
        balances[rec.source] -= rec.amount
        balances[rec.target] += rec.amount
        tc.assertGreaterEqual(balances[rec.source], 0, "不允许透支")
        if rec.tx_id in selected:
            total += t
    tc.assertEqual(total, expected_sum, "所选组涉案金额总和应等于边界值")


class TestAcceptableResults(unittest.TestCase):
    """可接受结果：合法流水下的上下界核算。"""

    def test_forced_full_taint_single_transfer(self):
        lg = Ledger()
        lg.set_initial_balance("A", 0)
        lg.set_initial_balance("B", 0)
        lg.deposit("A", 100, marked=True)
        t1 = lg.transfer("A", "B", 100)  # A 只有涉案资金，被迫全额携带
        bounds = lg.analyze([t1])
        self.assertIsInstance(bounds, Bounds)
        self.assertEqual(bounds.minimum, 100)
        self.assertEqual(bounds.maximum, 100)
        self.assertEqual(bounds.min_allocation[t1], 100)
        self.assertEqual(bounds.max_allocation[t1], 100)
        assert_allocation_valid(self, lg, bounds.min_allocation, {t1}, 100)
        assert_allocation_valid(self, lg, bounds.max_allocation, {t1}, 100)

    def test_taint_can_stay_put(self):
        # 普通资金足够时，涉案资金可以一分不动
        lg = Ledger()
        lg.set_initial_balance("A", 500)
        lg.set_initial_balance("B", 0)
        lg.deposit("A", 100, marked=True)
        t1 = lg.transfer("A", "B", 200)
        bounds = lg.analyze([t1])
        self.assertEqual(bounds.minimum, 0)
        self.assertEqual(bounds.maximum, 100)
        assert_allocation_valid(self, lg, bounds.min_allocation, {t1}, 0)
        assert_allocation_valid(self, lg, bounds.max_allocation, {t1}, 100)

    def test_allocation_covers_every_transfer(self):
        lg = Ledger()
        lg.set_initial_balance("A", 50)
        lg.set_initial_balance("B", 10)
        lg.deposit("A", 100, marked=True)
        t1 = lg.transfer("A", "B", 60)
        t2 = lg.transfer("B", "A", 20)
        t3 = lg.transfer("A", "B", 30)
        bounds = lg.analyze([t1, t3])
        self.assertEqual(set(bounds.min_allocation), {t1, t2, t3})
        self.assertEqual(set(bounds.max_allocation), {t1, t2, t3})


class TestCrossConstraints(unittest.TestCase):
    """题面交叉约束：联合边界、混合边界、回流次序。"""

    def test_joint_max_is_not_sum_of_individual_maxes(self):
        # A 持有 200 普通 + 100 涉案；两笔各 100 的出账，逐笔最大都是 100，
        # 但涉案资金只有一份，联合上界是 100 而不是 100+100。
        lg = Ledger()
        lg.set_initial_balance("A", 200)
        lg.set_initial_balance("B", 0)
        lg.set_initial_balance("C", 0)
        lg.deposit("A", 100, marked=True)
        t1 = lg.transfer("A", "B", 100)
        t2 = lg.transfer("A", "C", 100)
        self.assertEqual(lg.analyze([t1]).maximum, 100)
        self.assertEqual(lg.analyze([t2]).maximum, 100)
        both = lg.analyze([t1, t2])
        self.assertEqual(both.maximum, 100)  # 不能直接相加得 200
        self.assertEqual(both.minimum, 0)  # 普通资金足以覆盖两笔
        assert_allocation_valid(self, lg, both.max_allocation, {t1, t2}, 100)
        assert_allocation_valid(self, lg, both.min_allocation, {t1, t2}, 0)

    def test_partial_mix_bounds(self):
        # A：50 普通 + 100 涉案；转出 120 ⇒ 涉案部分被夹在 [70, 100]；
        # B 全额再转出 ⇒ 涉案资金被迫全部前移，整组边界为 [140, 200]。
        lg = Ledger()
        lg.set_initial_balance("A", 50)
        lg.set_initial_balance("B", 0)
        lg.set_initial_balance("C", 0)
        lg.deposit("A", 100, marked=True)
        t1 = lg.transfer("A", "B", 120)
        t2 = lg.transfer("B", "C", 120)
        first = lg.analyze([t1])
        self.assertEqual((first.minimum, first.maximum), (70, 100))
        both = lg.analyze([t1, t2])
        self.assertEqual((both.minimum, both.maximum), (140, 200))
        self.assertEqual(both.min_allocation[t1], 70)
        self.assertEqual(both.min_allocation[t2], 70)
        self.assertEqual(both.max_allocation[t1], 100)
        self.assertEqual(both.max_allocation[t2], 100)
        assert_allocation_valid(self, lg, both.min_allocation, {t1, t2}, 140)
        assert_allocation_valid(self, lg, both.max_allocation, {t1, t2}, 200)

    def test_flow_back_respects_order(self):
        # 涉案 100 经 A->B 后部分回流 B->A，再由 A->C 转出，仍按先后处理。
        lg = Ledger()
        lg.set_initial_balance("A", 0)
        lg.set_initial_balance("B", 100)  # B 自有普通资金，回流可不含涉案
        lg.set_initial_balance("C", 0)
        lg.deposit("A", 100, marked=True)
        t1 = lg.transfer("A", "B", 100)  # 被迫全额涉案
        t2 = lg.transfer("B", "A", 60)  # 回流：涉案 ∈ [0, 60]
        t3 = lg.transfer("A", "C", 60)  # 回流后再次转出
        only_t3 = lg.analyze([t3])
        self.assertEqual((only_t3.minimum, only_t3.maximum), (0, 60))
        all_three = lg.analyze([t1, t2, t3])
        self.assertEqual(all_three.minimum, 100)  # t1 被迫 100，t2/t3 可为 0
        self.assertEqual(all_three.maximum, 220)  # 100 + 60 + 60
        assert_allocation_valid(self, lg, all_three.min_allocation, {t1, t2, t3}, 100)
        assert_allocation_valid(self, lg, all_three.max_allocation, {t1, t2, t3}, 220)

    def test_taint_cannot_move_before_marked_deposit(self):
        # 严格次序：涉案入账发生之前的转账不可能携带涉案资金。
        lg = Ledger()
        lg.set_initial_balance("A", 200)
        lg.set_initial_balance("B", 0)
        t1 = lg.transfer("A", "B", 100)  # 涉案入账尚未发生，被迫为 0
        lg.deposit("A", 100, marked=True)
        t2 = lg.transfer("A", "B", 100)  # A 尚有 100 普通 + 100 涉案
        first = lg.analyze([t1])
        self.assertEqual((first.minimum, first.maximum), (0, 0))
        both = lg.analyze([t1, t2])
        self.assertEqual((both.minimum, both.maximum), (0, 100))
        assert_allocation_valid(self, lg, both.max_allocation, {t1, t2}, 100)


class TestRejectedResults(unittest.TestCase):
    """应被阻止或拒绝的输入与查询。"""

    def test_overdraft_rejected(self):
        lg = Ledger()
        lg.set_initial_balance("A", 50)
        lg.set_initial_balance("B", 0)
        lg.transfer("A", "B", 50)  # 恰好全部转出：边界允许
        self.assertEqual(lg.balance("A"), 0)
        with self.assertRaises(LedgerError):
            lg.transfer("A", "B", 1)  # 透支：拒绝

    def test_invalid_amounts_rejected(self):
        lg = Ledger()
        lg.set_initial_balance("A", 100)
        lg.set_initial_balance("B", 0)
        for bad in (0, -1, -100, 1.5, "10", True, None):
            with self.subTest(amount=bad):
                with self.assertRaises(LedgerError):
                    lg.transfer("A", "B", bad)
                with self.assertRaises(LedgerError):
                    lg.deposit("A", bad)
        with self.assertRaises(LedgerError):
            lg.set_initial_balance("C", -1)  # 初始余额不能为负

    def test_unknown_and_same_account_rejected(self):
        lg = Ledger()
        lg.set_initial_balance("A", 100)
        with self.assertRaises(LedgerError):
            lg.transfer("A", "Ghost", 10)  # 转入方不存在
        with self.assertRaises(LedgerError):
            lg.transfer("Ghost", "A", 10)  # 转出方不存在
        with self.assertRaises(LedgerError):
            lg.transfer("A", "A", 10)  # 自转
        with self.assertRaises(LedgerError):
            lg.set_initial_balance("A", 1)  # 重复设置初始余额

    def test_second_marked_deposit_rejected(self):
        lg = Ledger()
        lg.set_initial_balance("A", 0)
        lg.deposit("A", 100, marked=True)
        with self.assertRaises(LedgerError):
            lg.deposit("A", 50, marked=True)  # 只能标记一笔涉案入账

    def test_invalid_query_rejected(self):
        lg = Ledger()
        lg.set_initial_balance("A", 100)
        lg.set_initial_balance("B", 0)
        d = lg.deposit("A", 100, marked=True)
        t = lg.transfer("A", "B", 50)
        with self.assertRaises(LedgerError):
            lg.analyze([d])  # 入账记录不是出账记录
        with self.assertRaises(LedgerError):
            lg.analyze([999])  # 交易不存在
        with self.assertRaises(LedgerError):
            lg.analyze(["1"])  # 编号必须是整数
        with self.assertRaises(LedgerError):
            lg.analyze(t)  # 必须传入一组编号而非单个整数
        # 空查询组合法，边界为 0
        empty = lg.analyze([])
        self.assertEqual((empty.minimum, empty.maximum), (0, 0))

    def test_analyze_without_marked_deposit_rejected(self):
        lg = Ledger()
        lg.set_initial_balance("A", 100)
        lg.set_initial_balance("B", 0)
        t = lg.transfer("A", "B", 50)
        with self.assertRaises(LedgerError):
            lg.analyze([t])


if __name__ == "__main__":
    unittest.main()
