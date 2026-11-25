"""投递包编排：在至多 12 名收件人上求安全拆投的最优解。"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Key, PackageOption, Recipient, evaluate_package

MAX_RECIPIENTS = 12


class InfeasibleDeliveryError(ValueError):
    """不存在满足全部安全约束的拆投方案。"""


@dataclass(frozen=True)
class PlannedPackage:
    """最终方案中的一个包。"""

    members: frozenset[str]
    suite: str
    envelope_key_ids: tuple[str, ...]

    @property
    def envelope_count(self) -> int:
        return len(self.envelope_key_ids)


@dataclass(frozen=True)
class DeliveryPlan:
    """完整拆投方案：每名收件人恰好进入一个包。"""

    packages: tuple[PlannedPackage, ...]

    @property
    def package_count(self) -> int:
        return len(self.packages)

    @property
    def envelope_count(self) -> int:
        return sum(pkg.envelope_count for pkg in self.packages)

    def recipient_to_package(self) -> dict[str, PlannedPackage]:
        mapping: dict[str, PlannedPackage] = {}
        for pkg in self.packages:
            for rid in pkg.members:
                mapping[rid] = pkg
        return mapping


def plan_delivery(
    recipients: dict[str, Recipient] | list[Recipient],
    keys: list[Key] | dict[str, Key],
) -> DeliveryPlan:
    """为全部收件人找包数最少、同包数下封套数最少的安全拆投。

    采用 2**n 的子集动态规划（n <= 12）：

    - 先枚举每个非空收件人子集，调用 :func:`evaluate_package` 得到
      可行性与最少封套数；
    - 再对“恰好覆盖全员”的集合划分做 DP，字典序目标为
      ``(包数, 封套数, 划分签名)``，其中划分签名只用于在成本完全
      相同时给出确定的、可复现的平局结果。

    找不到任何安全拆投时抛出 :class:`InfeasibleDeliveryError`。
    """
    recipient_map = (
        {r.recipient_id: r for r in recipients}
        if isinstance(recipients, list)
        else dict(recipients)
    )
    n = len(recipient_map)
    if n > MAX_RECIPIENTS:
        raise ValueError(
            f"at most {MAX_RECIPIENTS} recipients are supported, got {n}"
        )
    if n == 0:
        return DeliveryPlan(())

    ids = sorted(recipient_map)
    index = {rid: i for i, rid in enumerate(ids)}
    full = (1 << n) - 1

    # ---- 预处理：每个子集作为单个包的评估结果 ----
    options: dict[int, PackageOption] = {}
    for mask in range(1, full + 1):
        members = frozenset(ids[i] for i in range(n) if mask & (1 << i))
        options[mask] = evaluate_package(members, recipient_map, keys)

    # ---- 集合划分 DP：dp[mask] = 覆盖 mask 的最优 (成本, 签名, 子包掩码序列) ----
    # 成本 = (包数, 封套数)；签名为成员 id 排序后的包元组，保证平局确定。
    dp: dict[
        int,
        tuple[
            tuple[int, int],
            tuple[tuple[str, ...], ...],
            tuple[int, ...],
        ],
    ] = {0: ((0, 0), (), ())}

    for mask in range(1, full + 1):
        best: tuple[tuple[int, int], tuple[tuple[str, ...], ...], tuple[int, ...]] | None = None
        # 有序集合划分：最低位成员必须在“最后一个包”里。枚举其余成员的
        # 任意子集 sub 与最低位组合成包；prev_mask 是 mask 的真子集，
        # 数值上一定小于 mask，因此按 mask 递增时已计算完毕。
        first_bit = mask & -mask
        rest = mask ^ first_bit
        sub = rest
        while True:
            pkg_mask = first_bit | sub
            option = options[pkg_mask]
            if option.feasible:
                prev_mask = mask ^ pkg_mask  # == rest ^ sub，是 mask 的真子集
                if prev_mask in dp:
                    prev_cost, prev_sig, prev_parts = dp[prev_mask]
                    cost = (
                        prev_cost[0] + 1,
                        prev_cost[1] + len(option.envelope_key_ids),
                    )
                    signature = prev_sig + (
                        tuple(sorted(option.members)),
                    )
                    candidate = (
                        cost,
                        signature,
                        prev_parts + (pkg_mask,),
                    )
                    if best is None or (candidate[0], candidate[1]) < (best[0], best[1]):
                        best = candidate
            if sub == 0:
                break
            sub = (sub - 1) & rest
        if best is not None:
            dp[mask] = best

    if best is None:
        solo_reasons: list[str] = []
        infeasible_solos = 0
        for rid in ids:
            solo = options[1 << index[rid]]
            if not solo.feasible:
                infeasible_solos += 1
                solo_reasons.append(f"{rid}: " + "; ".join(solo.violations))
        if solo_reasons:
            detail = (
                f"{infeasible_solos} recipient(s) cannot form a solo package: "
                + " | ".join(solo_reasons)
            )
        else:
            detail = (
                "every solo package is feasible, but no combination of "
                "joint packages satisfies all security constraints"
            )
        raise InfeasibleDeliveryError(
            "no secure delivery partition exists. " + detail
        )

    _, _, part_masks = best
    packages = [
        PlannedPackage(
            members=frozenset(ids[i] for i in range(n) if part_mask & (1 << i)),
            suite=options[part_mask].suite,  # type: ignore[arg-type]
            envelope_key_ids=options[part_mask].envelope_key_ids,
        )
        for part_mask in part_masks
    ]
    # 规范化输出顺序：按成员 id 元组字典序排列，结果与输入/枚举顺序无关。
    packages.sort(key=lambda pkg: tuple(sorted(pkg.members)))
    return DeliveryPlan(tuple(packages))
