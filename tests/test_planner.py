"""auth_planner 验收测试（unittest）。

覆盖题面交叉约束：
- 可接受结果：能力覆盖 + 相互独立 + 步骤最少；
- 应被阻止的结果：同设备/同恢复渠道无法凑齐独立证明、能力完全缺失；
- 边界与交错场景：单因素多能力、同设备共存只算一个独立来源、
  min_independent_proofs=1 边界、并列方案确定性、停用后重新规划。

直接运行：python3 -I -m unittest -v tests.test_planner
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth_planner import (  # noqa: E402
    CAPABILITY_FIDO2,
    CAPABILITY_HARDWARE_OTP,
    CAPABILITY_MOBILE_PUSH,
    CAPABILITY_PASSWORD,
    CAPABILITY_TOTP,
    AuthFactor,
    AuthenticationPlanner,
    OperationRequirement,
)
from auth_planner.planner import _independent_components  # noqa: E402


def make_factor(
    factor_id: str,
    capabilities,
    device_id: str,
    recovery_channel_id=None,
    step_count: int = 1,
    enabled: bool = True,
) -> AuthFactor:
    return AuthFactor(
        factor_id=factor_id,
        capabilities=frozenset(capabilities),
        device_id=device_id,
        recovery_channel_id=recovery_channel_id,
        step_count=step_count,
        enabled=enabled,
    )


class AcceptablePlanTests(unittest.TestCase):
    """可接受结果：独立因素覆盖全部能力，并选步骤最少的组合。"""

    def test_independent_factors_cover_requirement(self):
        planner = AuthenticationPlanner(
            [
                make_factor("pw", [CAPABILITY_PASSWORD], "terminal_a"),
                make_factor("key", [CAPABILITY_FIDO2], "security_key"),
            ]
        )
        req = OperationRequirement(
            "pay",
            {CAPABILITY_PASSWORD, CAPABILITY_FIDO2},
            min_independent_proofs=2,
        )
        result = planner.plan(req)

        self.assertTrue(result.feasible)
        self.assertEqual(result.factor_ids, ("key", "pw"))
        self.assertEqual(result.total_steps, 2)
        self.assertEqual(result.independent_proofs, 2)
        self.assertEqual(
            result.covered_capabilities,
            {CAPABILITY_PASSWORD, CAPABILITY_FIDO2},
        )

    def test_minimum_step_plan_chosen_among_feasible_sets(self):
        # password 有两个来源：步骤 5 的终端密码与步骤 1 的手机推送。
        planner = AuthenticationPlanner(
            [
                make_factor("pw_terminal", [CAPABILITY_PASSWORD], "d1", step_count=5),
                make_factor("pw_phone", [CAPABILITY_PASSWORD, CAPABILITY_MOBILE_PUSH], "d3", step_count=1),
                make_factor("key", [CAPABILITY_FIDO2], "d2", step_count=2),
            ]
        )
        req = OperationRequirement(
            "pay",
            {CAPABILITY_PASSWORD, CAPABILITY_FIDO2},
            min_independent_proofs=2,
        )
        result = planner.plan(req)

        self.assertTrue(result.feasible)
        self.assertEqual(result.factor_ids, ("key", "pw_phone"))
        self.assertEqual(result.total_steps, 3)
        self.assertEqual(result.independent_proofs, 2)

    def test_single_factor_providing_multiple_capabilities(self):
        # 一个因素同时声明两种能力：min=1 时单独即可成案。
        combo = make_factor("combo_key", [CAPABILITY_FIDO2, CAPABILITY_HARDWARE_OTP], "key1")
        planner = AuthenticationPlanner([combo])
        req = OperationRequirement(
            "pay",
            {CAPABILITY_FIDO2, CAPABILITY_HARDWARE_OTP},
            min_independent_proofs=1,
        )
        result = planner.plan(req)

        self.assertTrue(result.feasible)
        self.assertEqual(result.factor_ids, ("combo_key",))
        self.assertEqual(result.independent_proofs, 1)
        self.assertEqual(result.total_steps, 1)

    def test_tie_break_is_deterministic(self):
        # 两个 fido2 来源步骤相同，总成本并列时按因素 ID 序列取靠前方案。
        planner = AuthenticationPlanner(
            [
                make_factor("pw_x", [CAPABILITY_PASSWORD], "d1", step_count=1),
                make_factor("fido_y", [CAPABILITY_FIDO2], "d2", step_count=2),
                make_factor("fido_z", [CAPABILITY_FIDO2], "d3", step_count=2),
            ]
        )
        req = OperationRequirement(
            "pay",
            {CAPABILITY_PASSWORD, CAPABILITY_FIDO2},
            min_independent_proofs=2,
        )
        result = planner.plan(req)

        self.assertTrue(result.feasible)
        self.assertEqual(result.factor_ids, ("fido_y", "pw_x"))
        self.assertEqual(result.total_steps, 3)
        # 重复规划结果稳定。
        self.assertEqual(planner.plan(req).factor_ids, ("fido_y", "pw_x"))


class IndependenceConstraintTests(unittest.TestCase):
    """应被阻止/拒绝的结果：独立性交叉约束。"""

    def test_same_device_two_confirmations_not_two_independent_proofs(self):
        # 同一物理设备提供密码与 FIDO2：可以共存，但只算一个独立来源。
        planner = AuthenticationPlanner(
            [
                make_factor("pw", [CAPABILITY_PASSWORD], "terminal_a"),
                make_factor("fido_onboard", [CAPABILITY_FIDO2], "terminal_a"),
            ]
        )
        req = OperationRequirement(
            "pay",
            {CAPABILITY_PASSWORD, CAPABILITY_FIDO2},
            min_independent_proofs=2,
        )
        result = planner.plan(req)

        self.assertFalse(result.feasible)
        self.assertEqual(result.independence_deficit, 1)
        self.assertEqual(len(result.missing), 2)
        for item in result.missing:
            self.assertTrue(item.available)
            self.assertTrue(item.single_source)
            self.assertEqual(item.max_independent_sources, 1)

    def test_shared_recovery_channel_links_cross_device_factors(self):
        # 不同设备但共用恢复渠道：重置路径重合，不相互独立。
        linked = [
            make_factor("pw", [CAPABILITY_PASSWORD], "d1", recovery_channel_id="sms"),
            make_factor("key", [CAPABILITY_FIDO2], "d2", recovery_channel_id="sms"),
        ]
        self.assertEqual(_independent_components(linked), 1)

        planner = AuthenticationPlanner(linked)
        req = OperationRequirement(
            "pay",
            {CAPABILITY_PASSWORD, CAPABILITY_FIDO2},
            min_independent_proofs=2,
        )
        self.assertFalse(planner.plan(req).feasible)

        # 解除恢复渠道关联后（硬件凭据无恢复渠道）即独立。
        planner2 = AuthenticationPlanner(
            [
                make_factor("pw", [CAPABILITY_PASSWORD], "d1", recovery_channel_id="sms"),
                make_factor("key", [CAPABILITY_FIDO2], "d2", recovery_channel_id=None),
            ]
        )
        result = planner2.plan(req)
        self.assertTrue(result.feasible)
        self.assertEqual(result.independent_proofs, 2)

    def test_none_recovery_channels_do_not_link_factors(self):
        factors = [
            make_factor("pw", [CAPABILITY_PASSWORD], "d1", recovery_channel_id=None),
            make_factor("key", [CAPABILITY_FIDO2], "d2", recovery_channel_id=None),
        ]
        self.assertEqual(_independent_components(factors), 2)

    def test_capability_completely_missing_is_reported(self):
        planner = AuthenticationPlanner(
            [make_factor("pw", [CAPABILITY_PASSWORD], "d1")]
        )
        req = OperationRequirement(
            "pay",
            {CAPABILITY_PASSWORD, CAPABILITY_FIDO2},
            min_independent_proofs=1,
        )
        result = planner.plan(req)

        self.assertFalse(result.feasible)
        fido_diag = next(m for m in result.missing if m.capability == CAPABILITY_FIDO2)
        self.assertFalse(fido_diag.available)
        self.assertEqual(fido_diag.max_independent_sources, 0)
        pw_diag = next(m for m in result.missing if m.capability == CAPABILITY_PASSWORD)
        self.assertTrue(pw_diag.available)
        self.assertIn(CAPABILITY_FIDO2, result.reason)

    def test_three_independent_proofs_unreachable_blocks_plan(self):
        # 三个因素中两个同设备：最多只有 2 个独立来源，要求 3 个必须被阻止。
        planner = AuthenticationPlanner(
            [
                make_factor("pw", [CAPABILITY_PASSWORD], "d1"),
                make_factor("push", [CAPABILITY_MOBILE_PUSH], "d2"),
                make_factor("totp", [CAPABILITY_TOTP], "d2"),
            ]
        )
        req = OperationRequirement(
            "pay",
            {CAPABILITY_PASSWORD, CAPABILITY_MOBILE_PUSH, CAPABILITY_TOTP},
            min_independent_proofs=3,
        )
        result = planner.plan(req)

        self.assertFalse(result.feasible)
        self.assertEqual(result.independence_deficit, 1)


class BoundaryAndInterleavingTests(unittest.TestCase):
    """边界与交错场景。"""

    def test_same_device_coexistence_allowed_when_one_proof_suffices(self):
        # 边界：min_independent_proofs=1 时同设备两因素可以共存于方案。
        planner = AuthenticationPlanner(
            [
                make_factor("pw", [CAPABILITY_PASSWORD], "terminal_a"),
                make_factor("fido_onboard", [CAPABILITY_FIDO2], "terminal_a"),
            ]
        )
        req = OperationRequirement(
            "pay",
            {CAPABILITY_PASSWORD, CAPABILITY_FIDO2},
            min_independent_proofs=1,
        )
        result = planner.plan(req)

        self.assertTrue(result.feasible)
        self.assertEqual(set(result.factor_ids), {"pw", "fido_onboard"})
        self.assertEqual(result.independent_proofs, 1)

    def test_disabled_factor_excluded_and_replan_after_disable(self):
        planner = AuthenticationPlanner(
            [
                make_factor("pw", [CAPABILITY_PASSWORD], "d1", step_count=1),
                make_factor("key", [CAPABILITY_FIDO2], "d2", step_count=1),
                make_factor(
                    "phone_fido", [CAPABILITY_FIDO2], "d3", step_count=4
                ),
            ]
        )
        req = OperationRequirement(
            "pay",
            {CAPABILITY_PASSWORD, CAPABILITY_FIDO2},
            min_independent_proofs=2,
        )

        first = planner.plan(req)
        self.assertEqual(first.factor_ids, ("key", "pw"))

        # 停用首选安全密钥：自动改选步骤更多的手机 FIDO。
        planner.disable_factor("key")
        second = planner.plan(req)
        self.assertTrue(second.feasible)
        self.assertEqual(second.factor_ids, ("phone_fido", "pw"))
        self.assertEqual(second.total_steps, 5)

        # 再停用手机：FIDO2 能力彻底缺失，方案被阻止。
        planner.disable_factor("phone_fido")
        third = planner.plan(req)
        self.assertFalse(third.feasible)
        fido_diag = next(m for m in third.missing if m.capability == CAPABILITY_FIDO2)
        self.assertFalse(fido_diag.available)

        # 重新启用后恢复可行。
        planner.enable_factor("key")
        self.assertTrue(planner.plan(req).feasible)

    def test_factor_registered_disabled_is_ignored(self):
        planner = AuthenticationPlanner(
            [
                make_factor("pw", [CAPABILITY_PASSWORD], "d1"),
                make_factor("key", [CAPABILITY_FIDO2], "d2", enabled=False),
            ]
        )
        req = OperationRequirement(
            "pay",
            {CAPABILITY_PASSWORD, CAPABILITY_FIDO2},
            min_independent_proofs=1,
        )
        result = planner.plan(req)
        self.assertFalse(result.feasible)

        planner.enable_factor("key")
        self.assertTrue(planner.plan(req).feasible)

    def test_recovery_channel_and_device_chain_counts_as_one_source(self):
        # 交错关联：A 与 B 同设备，B 与 C 共享恢复渠道，三个因素经传递性
        # 全部落在同一连通分量，只算一个独立来源。
        factors = [
            make_factor("a", [CAPABILITY_PASSWORD], "dev1", recovery_channel_id="r1"),
            make_factor("b", [CAPABILITY_TOTP], "dev1", recovery_channel_id="r2"),
            make_factor("c", [CAPABILITY_FIDO2], "dev2", recovery_channel_id="r2"),
        ]
        self.assertEqual(_independent_components(factors), 1)

        planner = AuthenticationPlanner(factors)
        req = OperationRequirement(
            "pay",
            {CAPABILITY_PASSWORD, CAPABILITY_TOTP, CAPABILITY_FIDO2},
            min_independent_proofs=2,
        )
        result = planner.plan(req)
        self.assertFalse(result.feasible)
        self.assertEqual(result.independence_deficit, 1)
        for item in result.missing:
            self.assertEqual(item.max_independent_sources, 1)

    def test_no_factors_at_all(self):
        planner = AuthenticationPlanner()
        req = OperationRequirement("pay", {CAPABILITY_PASSWORD}, min_independent_proofs=1)
        result = planner.plan(req)
        self.assertFalse(result.feasible)
        self.assertEqual(result.independence_deficit, 1)
        self.assertFalse(result.missing[0].available)


class ValidationTests(unittest.TestCase):
    """输入校验：非法构造与重复登记必须被拒绝。"""

    def test_invalid_factor_construction(self):
        with self.assertRaises(ValueError):
            AuthFactor("", frozenset([CAPABILITY_PASSWORD]), "d1")
        with self.assertRaises(ValueError):
            AuthFactor("f", frozenset(), "d1")
        with self.assertRaises(ValueError):
            AuthFactor("f", frozenset([CAPABILITY_PASSWORD]), "")
        with self.assertRaises(ValueError):
            AuthFactor("f", frozenset([CAPABILITY_PASSWORD]), "d1", step_count=0)

    def test_invalid_requirement_construction(self):
        with self.assertRaises(ValueError):
            OperationRequirement("", frozenset([CAPABILITY_PASSWORD]))
        with self.assertRaises(ValueError):
            OperationRequirement("op", frozenset())
        with self.assertRaises(ValueError):
            OperationRequirement(
                "op", frozenset([CAPABILITY_PASSWORD]), min_independent_proofs=0
            )

    def test_duplicate_registration_and_unknown_factor_rejected(self):
        planner = AuthenticationPlanner(
            [make_factor("pw", [CAPABILITY_PASSWORD], "d1")]
        )
        with self.assertRaises(ValueError):
            planner.register_factor(make_factor("pw", [CAPABILITY_TOTP], "d2"))
        with self.assertRaises(KeyError):
            planner.disable_factor("not-exist")
        with self.assertRaises(KeyError):
            planner.enable_factor("not-exist")


if __name__ == "__main__":
    unittest.main(verbosity=2)
