"""认证规划核心算法。

策略：
1. 只考虑启用因素；
2. 枚举所有因素子集（因素规模通常为个位数，穷举语义最直接、可验证）；
3. 可行性 = 能力全覆盖 + 冲突图连通分量数 >= 要求的独立证明数；
4. 在可行子集中选总步骤最少者，步骤相同时选因素数更少、ID 序列更靠前的方案，
   保证结果确定；
5. 无可行方案时输出每项所需能力的缺失诊断与独立性缺口；
6. 因素停用/启用后再次调用 :meth:`AuthenticationPlanner.plan` 即重新选择。
"""

from __future__ import annotations

from itertools import combinations
from typing import Iterable, Sequence

from .models import (
    AuthFactor,
    MissingCapability,
    OperationRequirement,
    PlanResult,
)


def _independent_components(factors: Sequence[AuthFactor]) -> int:
    """计算因素集合在"同设备或同恢复渠道"冲突图上的连通分量数。

    每个连通分量代表一个相互独立的证明来源：
    - 同设备的因素必然连通（同一物理设备的两种确认不算两项独立证明）；
    - 共享恢复渠道的因素也连通（重置路径重合）；
    - 恢复渠道为 None 的因素不通过该维度与任何因素相连。
    """
    n = len(factors)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    for i in range(n):
        for j in range(i + 1, n):
            same_device = factors[i].device_id == factors[j].device_id
            same_recovery = (
                factors[i].recovery_channel_id is not None
                and factors[i].recovery_channel_id
                == factors[j].recovery_channel_id
            )
            if same_device or same_recovery:
                union(i, j)

    return len({find(i) for i in range(n)})


class AuthenticationPlanner:
    """认证规划组件。

    因素登记后可通过 :meth:`disable_factor` / :meth:`enable_factor` 切换状态；
    每次 :meth:`plan` 都基于当前启用状态重新计算，因此停用后自动重新选择。
    """

    def __init__(self, factors: Iterable[AuthFactor] = ()) -> None:
        self._factors: dict[str, AuthFactor] = {}
        for factor in factors:
            self.register_factor(factor)

    # ------------------------------------------------------------------ 登记
    def register_factor(self, factor: AuthFactor) -> None:
        if factor.factor_id in self._factors:
            raise ValueError(f"因素 {factor.factor_id!r} 已登记")
        self._factors[factor.factor_id] = factor

    def disable_factor(self, factor_id: str) -> None:
        """停用因素：后续规划不再考虑它。"""
        if factor_id not in self._factors:
            raise KeyError(factor_id)
        current = self._factors[factor_id]
        if current.enabled:
            self._factors[factor_id] = AuthFactor(
                factor_id=current.factor_id,
                capabilities=current.capabilities,
                device_id=current.device_id,
                recovery_channel_id=current.recovery_channel_id,
                step_count=current.step_count,
                enabled=False,
            )

    def enable_factor(self, factor_id: str) -> None:
        """重新启用因素。"""
        if factor_id not in self._factors:
            raise KeyError(factor_id)
        current = self._factors[factor_id]
        if not current.enabled:
            self._factors[factor_id] = AuthFactor(
                factor_id=current.factor_id,
                capabilities=current.capabilities,
                device_id=current.device_id,
                recovery_channel_id=current.recovery_channel_id,
                step_count=current.step_count,
                enabled=True,
            )

    def get_factor(self, factor_id: str) -> AuthFactor:
        return self._factors[factor_id]

    @property
    def factors(self) -> tuple[AuthFactor, ...]:
        return tuple(self._factors.values())

    # ------------------------------------------------------------------ 规划
    def plan(self, requirement: OperationRequirement) -> PlanResult:
        active = tuple(
            sorted(
                (f for f in self._factors.values() if f.enabled),
                key=lambda f: f.factor_id,
            )
        )

        # 边界：空要求不会出现（模型层已拒绝），空因素集必然不可行。
        best: tuple[AuthFactor, ...] | None = None
        best_steps: int | None = None
        best_components = 0

        # 按子集规模递增枚举；同规模内按因素 ID 排序，保证并列时结果确定。
        for size in range(1, len(active) + 1):
            for subset in combinations(active, size):
                covered = set()
                for f in subset:
                    covered.update(f.capabilities)
                if not requirement.required_capabilities.issubset(covered):
                    continue

                components = _independent_components(subset)
                if components < requirement.min_independent_proofs:
                    continue

                steps = sum(f.step_count for f in subset)
                if (
                    best is None
                    or steps < best_steps  # type: ignore[operator]
                    or (
                        steps == best_steps
                        and (
                            len(subset) < len(best)
                            or (
                                len(subset) == len(best)
                                and tuple(f.factor_id for f in subset)
                                < tuple(f.factor_id for f in best)
                            )
                        )
                    )
                ):
                    best = subset
                    best_steps = steps
                    best_components = components

        if best is not None:
            covered = set()
            for f in best:
                covered.update(f.capabilities)
            return PlanResult(
                feasible=True,
                selected_factors=tuple(best),
                total_steps=int(best_steps),
                independent_proofs=best_components,
                covered_capabilities=frozenset(covered),
            )

        return self._build_infeasible(requirement, active)

    # ------------------------------------------------------------------ 诊断
    def _build_infeasible(
        self,
        requirement: OperationRequirement,
        active: Sequence[AuthFactor],
    ) -> PlanResult:
        missing: list[MissingCapability] = []

        for capability in sorted(requirement.required_capabilities):
            providers = tuple(
                f for f in active if capability in f.capabilities
            )
            if not providers:
                missing.append(
                    MissingCapability(
                        capability=capability,
                        available=False,
                        max_independent_sources=0,
                        single_source=False,
                    )
                )
                continue

            sources = _independent_components(providers)
            missing.append(
                MissingCapability(
                    capability=capability,
                    available=True,
                    max_independent_sources=sources,
                    single_source=sources < requirement.min_independent_proofs,
                )
            )

        # 独立性缺口：在全部启用因素上可达到的最大独立证明数与要求之差。
        max_overall = _independent_components(active) if active else 0
        deficit = max(0, requirement.min_independent_proofs - max_overall)

        unavailable = [m.capability for m in missing if not m.available]
        single_source = [m.capability for m in missing if m.single_source]
        reason_parts = []
        if unavailable:
            reason_parts.append(
                "缺少能提供能力的已启用因素: " + ", ".join(unavailable)
            )
        if single_source:
            reason_parts.append(
                "以下能力无法凑齐 "
                f"{requirement.min_independent_proofs} 个相互独立来源: "
                + ", ".join(single_source)
            )
        if not reason_parts:
            reason_parts.append(
                f"全部启用因素最多提供 {max_overall} 个独立证明，"
                f"低于要求的 {requirement.min_independent_proofs} 个"
            )

        return PlanResult(
            feasible=False,
            missing=tuple(missing),
            independence_deficit=deficit,
            reason="；".join(reason_parts),
        )
