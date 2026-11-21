"""核心数据模型。

所有模型均为不可变值对象（frozen dataclass），可安全作为字典键、
用于集合去重，也方便在测试中做相等性断言。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Mapping, Optional, Tuple


@dataclass(frozen=True)
class Backup:
    """单个候选备份。

    Attributes:
        backup_id: 备份在所属服务内的唯一标识。
        position: 本备份的提交位置（非负整数，越大越新）。
        bytes_required: 恢复该备份需要的字节数（非负）。
        requirements: 依赖表，键为被引用服务的标识，值为该服务必须达到
            的最低提交位置；依赖只约束被选备份的位置，不要求指向某个
            具体备份，也不要求引用关系无环。
    """

    backup_id: str
    position: int
    bytes_required: int
    requirements: FrozenSet[Tuple[str, int]] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.backup_id, str) or not self.backup_id:
            raise ValueError("backup_id 必须是非空字符串")
        if isinstance(self.position, bool) or not isinstance(self.position, int):
            raise ValueError("position 必须是整数")
        if self.position < 0:
            raise ValueError("position 不能为负数")
        if isinstance(self.bytes_required, bool) or not isinstance(
            self.bytes_required, int
        ):
            raise ValueError("bytes_required 必须是整数")
        if self.bytes_required < 0:
            raise ValueError("bytes_required 不能为负数")
        normalized: dict = {}
        # 直接遍历 (服务, 最低位置) 对：不能先转 dict，否则重复服务键
        # 会在合并逻辑运行前就被折叠。
        for ref_service, min_position in self.requirements:
            if not isinstance(ref_service, str) or not ref_service:
                raise ValueError("requirements 的服务标识必须是非空字符串")
            if isinstance(min_position, bool) or not isinstance(min_position, int):
                raise ValueError("requirements 的最低位置必须是整数")
            if min_position < 0:
                raise ValueError("requirements 的最低位置不能为负数")
            # 同一服务出现多次时取最严格（最大）的位置约束。
            normalized[ref_service] = max(
                min_position, normalized.get(ref_service, min_position)
            )
        object.__setattr__(self, "requirements", frozenset(normalized.items()))


@dataclass(frozen=True)
class Service:
    """一个参与灾备演练的服务及其候选备份集合。"""

    service_id: str
    weight: int
    backups: Tuple[Backup, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.service_id, str) or not self.service_id:
            raise ValueError("service_id 必须是非空字符串")
        if isinstance(self.weight, bool) or not isinstance(self.weight, int):
            raise ValueError("weight 必须是整数")
        if self.weight < 0:
            raise ValueError("weight 不能为负数")
        if not isinstance(self.backups, tuple):
            object.__setattr__(self, "backups", tuple(self.backups))
        seen = set()
        for backup in self.backups:
            if not isinstance(backup, Backup):
                raise ValueError("backups 的每个元素必须是 Backup 实例")
            if backup.backup_id in seen:
                raise ValueError(
                    f"服务 {self.service_id} 内备份标识重复: {backup.backup_id}"
                )
            seen.add(backup.backup_id)

    @property
    def latest_position(self) -> int:
        """该服务所有候选备份中的最新提交位置。"""

        return max((backup.position for backup in self.backups), default=0)


@dataclass(frozen=True)
class RecoveryResult:
    """恢复点选择结果。

    有解与无解共用同一结构，通过 ``feasible`` 区分：

    - 有解：``selected`` 给出每个服务选中的备份，``total_bytes`` 与
      ``loss`` 为实际总量和加权损失，``budget_can_fix`` 为 True，
      ``required_budget`` 等于 ``total_bytes``。
    - 无解且放宽字节上限可解决：``feasible`` 为 False，``reason`` 为
      ``REASON_BUDGET``，``budget_can_fix`` 为 True，``selected`` 为
      放宽到 ``required_budget`` 时的最优选择。
    - 结构性无解（依赖冲突 / 服务无备份）：``budget_can_fix`` 为 False，
      ``required_budget`` 与放宽选择均为 None。
    """

    feasible: bool
    selected: Optional[Tuple[Backup, ...]]
    total_bytes: Optional[int]
    loss: Optional[int]
    budget_can_fix: bool
    required_budget: Optional[int]
    reason: Optional[str]
