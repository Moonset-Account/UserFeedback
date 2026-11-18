"""基线引擎数据模型。

所有输入均为普通 Python 对象，可由 ``load_input`` 从 JSON 结构载入。
列表型覆盖项支持三种操作：

* ``merge``（默认）：去重合并到上层列表；
* ``replace``：显式清除上层列表，以本层内容为准；
* ``remove``：从上层列表中剔除指定元素。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# 不符合项来源
SOURCE_CONFIG = "config"
SOURCE_RUNTIME = "runtime"

# 服务判定状态
STATUS_COMPLIANT = "compliant"          # 配置与运行身份均符合预期
STATUS_REMEDIATE = "pending_remediation"  # 配置符合预期但进程身份/现场不符
STATUS_NONCOMPLIANT = "noncompliant"    # 最终配置本身不符合低权限要求
STATUS_NO_SNAPSHOT = "no_snapshot"      # 尚无运行快照
STATUS_SKIPPED = "skipped"              # 未要求低权限运行，不纳入核对

# 检查维度
CHECK_IDENTITY = "startup_identity"
CHECK_WRITE_SCOPE = "write_scope"
CHECK_INHERITANCE = "privilege_inheritance"

_SEVERITY_CRITICAL = "critical"
_SEVERITY_HIGH = "high"
_SEVERITY_MEDIUM = "medium"


class ListOp:
    """列表覆盖操作描述。

    既支持字符串简写（``"merge"`` / ``"replace"`` / ``"remove"``），
    也支持显式对象 ``{"op": "replace", "items": [...]}``。
    """

    MERGE = "merge"
    REPLACE = "replace"
    REMOVE = "remove"
    _OPS = (MERGE, REPLACE, REMOVE)

    def __init__(self, op: str = MERGE, items: Iterable[Any] | None = None) -> None:
        if op not in self._OPS:
            raise ValueError(f"未知列表覆盖操作: {op!r}，允许 {self._OPS}")
        self.op = op
        self.items: list[Any] = list(items or [])

    @classmethod
    def coerce(cls, value: Any) -> "ListOp":
        """把简写/对象形式统一为 :class:`ListOp`。"""
        if isinstance(value, ListOp):
            return value
        if isinstance(value, str):
            return cls(value)
        if isinstance(value, Mapping):
            op = value.get("op", cls.MERGE)
            return cls(op, value.get("items", []))
        if isinstance(value, Iterable):
            # 普通列表视为合并
            return cls(cls.MERGE, value)
        raise TypeError(f"无法解释的列表覆盖项: {value!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "items": list(self.items)}

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ListOp):
            return NotImplemented
        return self.op == other.op and self.items == other.items

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"ListOp(op={self.op!r}, items={self.items!r})"


@dataclass
class ServiceDef:
    """管控端导出的单个服务定义。"""

    name: str
    user: str | None = None
    group: str | None = None
    # 允许写入的路径（配置口径）
    write_paths: ListOp | list[str] = field(default_factory=list)
    # 禁止继承/使用的高权限身份
    forbidden_identities: ListOp | list[str] = field(default_factory=list)
    # 该服务是否要求低权限运行；None 表示跟随平台默认值
    low_privilege_required: bool | None = None
    # 启动别名：该服务对外暴露的其他名字
    aliases: list[str] = field(default_factory=list)
    # 别名定义指向的规范服务名；非 None 时本定义仅作别名转发
    alias_of: str | None = None
    description: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ServiceDef":
        return cls(
            name=raw["name"],
            user=raw.get("user"),
            group=raw.get("group"),
            write_paths=_coerce_list_field(raw.get("write_paths", [])),
            forbidden_identities=_coerce_list_field(
                raw.get("forbidden_identities", [])
            ),
            low_privilege_required=raw.get("low_privilege_required"),
            aliases=list(raw.get("aliases", [])),
            alias_of=raw.get("alias_of"),
            description=raw.get("description", ""),
        )


@dataclass
class Overlay:
    """覆盖层：按服务名定向修改上层定义。"""

    target: str
    user: str | None = None
    group: str | None = None
    write_paths: ListOp | None = None
    forbidden_identities: ListOp | None = None
    low_privilege_required: bool | None = None
    description: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Overlay":
        return cls(
            target=raw["target"],
            user=raw.get("user"),
            group=raw.get("group"),
            write_paths=(
                ListOp.coerce(raw["write_paths"])
                if "write_paths" in raw
                else None
            ),
            forbidden_identities=(
                ListOp.coerce(raw["forbidden_identities"])
                if "forbidden_identities" in raw
                else None
            ),
            low_privilege_required=raw.get("low_privilege_required"),
            description=raw.get("description", ""),
        )


@dataclass
class PlatformDefaults:
    """平台默认值：所有服务的基础层与低权限策略。"""

    user: str | None = None
    group: str | None = None
    write_paths: list[str] = field(default_factory=list)
    forbidden_identities: list[str] = field(default_factory=list)
    # 平台是否默认要求低权限运行
    low_privilege_required: bool = True

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "PlatformDefaults":
        raw = raw or {}
        return cls(
            user=raw.get("user"),
            group=raw.get("group"),
            write_paths=list(raw.get("write_paths", [])),
            forbidden_identities=list(raw.get("forbidden_identities", [])),
            low_privilege_required=raw.get("low_privilege_required", True),
        )


@dataclass
class ProcessSnapshot:
    """进程实测身份快照。"""

    service: str
    user: str
    group: str | None = None
    # 进程实际发生写入的路径
    actual_write_paths: list[str] = field(default_factory=list)
    pid: int | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProcessSnapshot":
        return cls(
            service=raw["service"],
            user=raw["user"],
            group=raw.get("group"),
            actual_write_paths=list(raw.get("actual_write_paths", [])),
            pid=raw.get("pid"),
        )


@dataclass
class Finding:
    """单个不符合项。"""

    check: str
    source: str  # SOURCE_CONFIG / SOURCE_RUNTIME
    message: str
    severity: str = _SEVERITY_HIGH
    expected: Any = None
    actual: Any = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "source": self.source,
            "severity": self.severity,
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
            "detail": dict(self.detail),
        }


@dataclass
class ServiceReport:
    """单个服务的基线核对结果。"""

    name: str
    canonical_name: str
    status: str
    low_privilege_required: bool
    resolved: Mapping[str, Any]
    findings: list[Finding] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    has_snapshot: bool = False
    snapshot_service: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "canonical_name": self.canonical_name,
            "status": self.status,
            "low_privilege_required": self.low_privilege_required,
            "has_snapshot": self.has_snapshot,
            "snapshot_service": self.snapshot_service,
            "aliases": list(self.aliases),
            "resolved": dict(self.resolved),
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class BaselineReport:
    """整批服务的基线报告。"""

    services: list[ServiceReport] = field(default_factory=list)

    def by_status(self, status: str) -> list[ServiceReport]:
        return [s for s in self.services if s.status == status]

    @property
    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {
            STATUS_COMPLIANT: 0,
            STATUS_REMEDIATE: 0,
            STATUS_NONCOMPLIANT: 0,
            STATUS_NO_SNAPSHOT: 0,
            STATUS_SKIPPED: 0,
        }
        for svc in self.services:
            counts[svc.status] = counts.get(svc.status, 0) + 1
        counts["total"] = len(self.services)
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "services": [s.to_dict() for s in self.services],
        }


def _coerce_list_field(value: Any) -> ListOp | list[str]:
    if value is None:
        return []
    if isinstance(value, Mapping) or isinstance(value, str):
        return ListOp.coerce(value)
    if isinstance(value, ListOp):
        return value
    return list(value)
