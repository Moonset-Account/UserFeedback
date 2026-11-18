"""审计输入的数据模型.

所有产物仅描述同一租户内的合成操作记录,用于离线审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple


@dataclass(frozen=True)
class FieldGrading:
    """资源的字段分级:resource -> {field_name: level}。"""

    levels: Dict[str, Dict[str, int]]

    def fields_of(self, resource: str) -> FrozenSet[str]:
        return frozenset(self.levels.get(resource, {}))

    def level_of(self, resource: str, field_name: str) -> Optional[int]:
        return self.levels.get(resource, {}).get(field_name)


def expand_level_grant(grading: FieldGrading, resource: str, max_level: int) -> FrozenSet[str]:
    """把 "可见到某一级" 的授权展开为显式字段集合(级别 <= max_level)。"""
    return frozenset(
        name
        for name, level in grading.levels.get(resource, {}).items()
        if level <= max_level
    )


@dataclass(frozen=True)
class Subject:
    """租户内的调用主体及其所属组。"""

    id: str
    groups: FrozenSet[str] = frozenset()


@dataclass(frozen=True)
class PermissionChange:
    """一次组级权限变更。

    语义:
    - 变更对组内所有成员 *同时* 生效(原子);
    - 生效时刻落在 ``window`` 闭区间内;
    - ``receipt`` 为 False 表示缺少生效回执,生效时刻只能按窗口估计;
    - ``version`` 为该次变更对应的授权版本,同租户内版本必须按
      非递减顺序生效。
    """

    id: str
    version: int
    group: str
    resource: str
    grant: FrozenSet[str] = frozenset()
    revoke: FrozenSet[str] = frozenset()
    window: Tuple[float, float] = (0.0, 0.0)
    receipt: bool = True

    def __post_init__(self) -> None:
        if self.window[0] > self.window[1]:
            raise ValueError(f"change {self.id}: window start > end")
        if self.version < 0:
            raise ValueError(f"change {self.id}: negative version")


@dataclass(frozen=True)
class ReadRecord:
    """一次已完成读取操作的合成记录。

    - ``start``/``end``: 调用起止时刻(闭区间);
    - ``returned``: 实际返回的字段集合;
    - ``authz_version``: 可选,调用方记录的授权版本。
    """

    id: str
    subject: str
    resource: str
    returned: FrozenSet[str]
    start: float
    end: float
    authz_version: Optional[int] = None

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"read {self.id}: start > end")


@dataclass(frozen=True)
class AuditInput:
    """一次离线审计的完整输入(同一租户)。"""

    tenant: str
    grading: FieldGrading
    subjects: Tuple[Subject, ...]
    # 初始授权:subject_id -> resource -> 可见字段集合
    initial_permissions: Dict[str, Dict[str, FrozenSet[str]]]
    changes: Tuple[PermissionChange, ...] = ()
    reads: Tuple[ReadRecord, ...] = ()

    def members_of(self, group: str) -> List[str]:
        return [s.id for s in self.subjects if group in s.groups]

    def subject_ids(self) -> FrozenSet[str]:
        return frozenset(s.id for s in self.subjects)
