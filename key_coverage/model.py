"""组成员、别名、密钥与目录版本的数据模型。

目录（通讯簿）按版本保存：每个 :class:`DirectorySnapshot` 是某一时刻不可变的
快照，包含该时刻的组、别名、人员及其可用密钥。检查时固定一个目录版本，因此
遍历过程中看到的成员关系不会随版本变化。

密级用字符串表示并按固定顺序比较高低：
``public < internal < confidential < secret < top_secret``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping, Optional

#: 密级从低到高的固定顺序。
CLASSIFICATION_ORDER: tuple[str, ...] = (
    "public",
    "internal",
    "confidential",
    "secret",
    "top_secret",
)


def classification_rank(level: str) -> int:
    """返回密级的排序值；未知密级直接报错，避免被静默当成最低密级。"""
    try:
        return CLASSIFICATION_ORDER.index(level)
    except ValueError as exc:  # pragma: no cover - 仅由输入错误触发
        raise ValueError(f"未知密级: {level!r}") from exc


class MemberKind(str, Enum):
    """成员引用的目标类型。"""

    PERSON = "person"
    GROUP = "group"
    ALIAS = "alias"


@dataclass(frozen=True)
class MemberRef:
    """组内的一条成员关系。

    :param target_id: 目标（人员 / 组 / 别名）的 ID。
    :param kind: 目标类型。
    :param min_level: 经过这条成员边所要求的最低密级；``None`` 表示不限制。
        通知密级必须达到该要求，这条路径上的人员才算有资格。
    """

    target_id: str
    kind: MemberKind
    min_level: Optional[str] = None


@dataclass(frozen=True)
class Group:
    """通讯组。``members`` 为有序成员列表，允许组嵌套和自引用。"""

    id: str
    name: str
    members: tuple[MemberRef, ...] = ()

    def display_name(self) -> str:
        return self.name or self.id


@dataclass(frozen=True)
class Alias:
    """别名：仅做名字到人员 / 组 / 另一个别名的解析，本身不携带密级约束。"""

    id: str
    target_id: str
    kind: MemberKind


@dataclass(frozen=True)
class KeyRecord:
    """收件人可用的一把加密密钥及其有效时段。

    有效时段按半开区间 ``[valid_from, valid_to)`` 判定：

    * ``valid_from is None`` 表示起始方向无限早；
    * ``valid_to is None`` 表示结束方向无限晚；
    * 投递时刻恰好等于 ``valid_to`` 时密钥已失效（边界在区间外）。
    """

    key_id: str
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None

    def is_valid_at(self, when: datetime) -> bool:
        if self.valid_from is not None and when < self.valid_from:
            return False
        if self.valid_to is not None and when >= self.valid_to:
            return False
        return True


@dataclass(frozen=True)
class Person:
    """真实收件人；``keys`` 是该人当前可用的全部密钥。"""

    id: str
    name: str = ""
    keys: tuple[KeyRecord, ...] = ()

    def display_name(self) -> str:
        return self.name or self.id


@dataclass(frozen=True)
class DirectorySnapshot:
    """一个目录版本的完整不可变快照。"""

    version: str
    groups: Mapping[str, Group] = field(default_factory=dict)
    aliases: Mapping[str, Alias] = field(default_factory=dict)
    persons: Mapping[str, Person] = field(default_factory=dict)

    def group(self, group_id: str) -> Group:
        try:
            return self.groups[group_id]
        except KeyError as exc:
            raise KeyError(f"目录版本 {self.version!r} 中不存在组 {group_id!r}") from exc


class DirectoryStore:
    """按版本保存的目录集合，检查时固定取出某一个快照。"""

    def __init__(self, snapshots: Mapping[str, DirectorySnapshot]) -> None:
        self._snapshots: dict[str, DirectorySnapshot] = dict(snapshots)

    def snapshot(self, version: str) -> DirectorySnapshot:
        try:
            return self._snapshots[version]
        except KeyError as exc:
            raise KeyError(f"不存在目录版本 {version!r}") from exc

    @property
    def versions(self) -> tuple[str, ...]:
        return tuple(self._snapshots)


class Status(str, Enum):
    """整封邮件的检查结论。"""

    #: 全部有资格的人都有投递时刻有效的密钥，可以进入发送。
    READY = "ready"
    #: 存在没有有效密钥的有资格者（或结构性错误），邮件停在准备状态。
    PREPARING = "preparing"


@dataclass(frozen=True)
class PathNode:
    """原因链 / 到达路径上的一跳。

    :param ref: 该跳指向的对象 ID（组、别名或人员）。
    :param kind: 对象类型。
    :param name: 展示名，缺失时退回 ID。
    :param min_level: 进入这一跳的成员边所要求的最低密级（别名边为 ``None``）。
    """

    ref: str
    kind: MemberKind
    name: str
    min_level: Optional[str] = None


@dataclass(frozen=True)
class KeyDiagnostic:
    """单把密钥在投递时刻的诊断信息。"""

    key_id: str
    valid: bool
    valid_from: Optional[datetime]
    valid_to: Optional[datetime]
    reason: str


@dataclass(frozen=True)
class MissingKeyFailure:
    """一名有资格但没有有效密钥的收件人的失败明细。

    :param person_id: 收件人 ID。
    :param person_name: 收件人展示名。
    :param eligible_paths: 证明其有资格的到达路径（组/别名链，含根组与本人）。
    :param key_diagnostics: 该人全部密钥在投递时刻的诊断；没有密钥时为空。
    :param reason_chain: 可一路回到原始组名的可读原因链。
    """

    person_id: str
    person_name: str
    eligible_paths: tuple[tuple[PathNode, ...], ...]
    key_diagnostics: tuple[KeyDiagnostic, ...]
    reason_chain: str


@dataclass(frozen=True)
class RecipientCoverage:
    """最终覆盖结果中的一名收件人。"""

    person_id: str
    person_name: str
    key_id: str
    eligible_paths: tuple[tuple[PathNode, ...], ...]


@dataclass(frozen=True)
class CoverageResult:
    """整封邮件的密钥覆盖检查结论。"""

    status: Status
    notification_level: str
    directory_version: str
    scheduled_at: datetime
    #: 按实际加密密钥去重后的最终收件人（READY 时非空与否取决于组成员）。
    recipients: tuple[RecipientCoverage, ...]
    #: 没有有效密钥的有资格者；非空时状态必为 PREPARING。
    missing_key_failures: tuple[MissingKeyFailure, ...]
    #: 所有到达过的人员 ID（含因密级受限被排除者），便于核对遍历完整性。
    reached_person_ids: tuple[str, ...]
    #: 因密级不满足而被全部路径排除的人员，及被阻断的路径。
    restricted: Mapping[str, tuple[tuple[PathNode, ...], ...]]
    #: 遍历中被截断的环（自引用 / 互引用），记录截断前的路径。
    cycle_cuts: tuple[tuple[PathNode, ...], ...]
    #: 悬空引用等结构性问题，非空时状态为 PREPARING。
    structural_errors: tuple[str, ...]
