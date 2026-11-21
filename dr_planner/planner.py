"""恢复点选择规划器。

规模约束（题面）：最多 6 个服务、每个服务最多 6 份候选备份，
组合空间至多 6**6 = 46656，直接穷举即可在毫秒级完成，且能保证
得到全局最优解，无需引入启发式近似。

选择目标（按优先级）：
    1. 每个被选备份的全部依赖都被满足（被引用服务的恢复位置
       不低于其要求的最低提交位置，允许自引用与循环引用）；
    2. 总恢复字节数不超过字节上限；
    3. 加权损失最小：sum((latest_position_i - chosen_position_i) * weight_i)；
    4. 损失相同则总字节更少；再相同则按备份标识字典序确定确定性结果。
"""

from __future__ import annotations

from itertools import product
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .model import Backup, RecoveryResult, Service

#: 字节上限约束导致无解（放宽上限可解决）。
REASON_BUDGET = "budget_exceeded"
#: 依赖本身无法满足，增加字节也无法解决。
REASON_INFEASIBLE_DEPS = "infeasible_dependencies"
#: 至少一个服务没有任何候选备份。
REASON_EMPTY_SERVICE = "empty_backup_set"

#: 题面规定的规模上限：服务数量与单服务候选备份数。
MAX_SERVICES = 6
MAX_BACKUPS_PER_SERVICE = 6


def _validate_services(services: Sequence[Service]) -> None:
    if not isinstance(services, (list, tuple)) or not services:
        raise ValueError("services 必须是非空的 Service 序列")
    if len(services) > MAX_SERVICES:
        raise ValueError(f"服务数量不能超过 {MAX_SERVICES} 个")
    seen = set()
    for service in services:
        if not isinstance(service, Service):
            raise ValueError("services 的每个元素必须是 Service 实例")
        if service.service_id in seen:
            raise ValueError(f"服务标识重复: {service.service_id}")
        seen.add(service.service_id)
        if len(service.backups) > MAX_BACKUPS_PER_SERVICE:
            raise ValueError(
                f"服务 {service.service_id} 的候选备份不能超过 "
                f"{MAX_BACKUPS_PER_SERVICE} 份"
            )


def _validate_budget(budget: int) -> None:
    if isinstance(budget, bool) or not isinstance(budget, int):
        raise ValueError("byte_budget 必须是整数")
    if budget < 0:
        raise ValueError("byte_budget 不能为负数")


def _dependencies_satisfied(
    services: Sequence[Service], chosen: Tuple[Backup, ...]
) -> bool:
    """校验一组备份选择是否满足全部依赖。

    依赖允许指向服务自身，也允许服务间循环引用；只比较最终恢复位置。
    若依赖指向不在规划集合中的服务，则永远无法满足。
    """

    positions: Dict[str, int] = {
        service.service_id: backup.position
        for service, backup in zip(services, chosen)
    }
    for backup in chosen:
        for ref_service, min_position in backup.requirements:
            if positions.get(ref_service, -1) < min_position:
                return False
    return True


def _evaluate(
    services: Sequence[Service], chosen: Tuple[Backup, ...]
) -> Tuple[int, int]:
    """返回 (总恢复字节数, 加权损失)。"""

    total_bytes = sum(backup.bytes_required for backup in chosen)
    loss = sum(
        (service.latest_position - backup.position) * service.weight
        for service, backup in zip(services, chosen)
    )
    return total_bytes, loss


def _lex_key(chosen: Tuple[Backup, ...]) -> Tuple[str, ...]:
    return tuple(backup.backup_id for backup in chosen)


def plan_recovery(
    services: Sequence[Service], byte_budget: int
) -> RecoveryResult:
    """执行恢复点选择。

    Args:
        services: 参与演练的服务（1~6 个），顺序即结果中的服务顺序。
        byte_budget: 总恢复字节上限。

    Returns:
        RecoveryResult：有解时给出各服务备份、字节总量、损失值；
        无解时说明原因，并判断增加可恢复字节数是否能解决问题；
        若可以，同时给出所需的最低字节上限及对应选择。

    Raises:
        ValueError: 入参结构非法（空服务集合、重复标识、负数等）。
    """

    _validate_services(services)
    _validate_budget(byte_budget)

    if any(not service.backups for service in services):
        return RecoveryResult(
            feasible=False,
            selected=None,
            total_bytes=None,
            loss=None,
            budget_can_fix=False,
            required_budget=None,
            reason=REASON_EMPTY_SERVICE,
        )

    # 预算内最优：键 (损失, 字节, 字典序)。
    best_budgeted: Optional[Tuple[Tuple[int, int, Tuple[str, ...]], Tuple[Backup, ...]]] = None
    # 仅依赖可行时的最低字节选择：键 (字节, 损失, 字典序)。
    best_any: Optional[Tuple[Tuple[int, int, Tuple[str, ...]], Tuple[Backup, ...]]] = None

    backup_lists: List[Iterable[Backup]] = [
        list(service.backups) for service in services
    ]
    for chosen in product(*backup_lists):
        if not _dependencies_satisfied(services, chosen):
            continue
        total_bytes, loss = _evaluate(services, chosen)
        lex = _lex_key(chosen)

        any_key = (total_bytes, loss, lex)
        if best_any is None or any_key < best_any[0]:
            best_any = (any_key, chosen)

        if total_bytes <= byte_budget:
            budgeted_key = (loss, total_bytes, lex)
            if best_budgeted is None or budgeted_key < best_budgeted[0]:
                best_budgeted = (budgeted_key, chosen)

    if best_budgeted is not None:
        (loss, total_bytes, _), chosen = best_budgeted
        return RecoveryResult(
            feasible=True,
            selected=chosen,
            total_bytes=total_bytes,
            loss=loss,
            budget_can_fix=True,
            required_budget=total_bytes,
            reason=None,
        )

    # 预算内无解：判断是预算问题还是结构性依赖问题。
    if best_any is None:
        return RecoveryResult(
            feasible=False,
            selected=None,
            total_bytes=None,
            loss=None,
            budget_can_fix=False,
            required_budget=None,
            reason=REASON_INFEASIBLE_DEPS,
        )

    (required_budget, loss, _), chosen = best_any
    return RecoveryResult(
        feasible=False,
        selected=chosen,
        total_bytes=required_budget,
        loss=loss,
        budget_can_fix=True,
        required_budget=required_budget,
        reason=REASON_BUDGET,
    )
