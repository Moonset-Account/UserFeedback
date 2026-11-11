"""裁决所用数据模型：组件标识、CycloneDX 风格清单、漏洞公告、适用性记录。

组件身份由四元组共同确定（与 CycloneDX 的 purl 坐标对齐）：
    (ecosystem, namespace, name, version)
其中 namespace 允许为空字符串，但 name / ecosystem 不允许为空。
两个“同名组件”只要生态或命名空间不同，就是不同组件，互不串用证据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

from .interval import Interval
from .versioning import Version, parse_version

# 处置状态常量（四种处置 + 内部使用的三态判定）
DISPOSITION_AFFECTED = "affected"          # 受影响
DISPOSITION_NOT_AFFECTED = "not_affected"  # 不受影响
DISPOSITION_FIXED = "fixed"                # 已修复
DISPOSITION_NEEDS_VERIFICATION = "needs_verification"  # 待核实

DISPOSITIONS = (
    DISPOSITION_AFFECTED,
    DISPOSITION_NOT_AFFECTED,
    DISPOSITION_FIXED,
    DISPOSITION_NEEDS_VERIFICATION,
)

DISPOSITION_LABELS_ZH = {
    DISPOSITION_AFFECTED: "受影响",
    DISPOSITION_NOT_AFFECTED: "不受影响",
    DISPOSITION_FIXED: "已修复",
    DISPOSITION_NEEDS_VERIFICATION: "待核实",
}

# 公告 / 记录内部的判定语义
VERDICT_AFFECTED = "affected"
VERDICT_NOT_AFFECTED = "not_affected"
VERDICT_FIXED = "fixed"
VERDICT_UNKNOWN = "unknown"
DEFINITIVE_VERDICTS = (VERDICT_AFFECTED, VERDICT_NOT_AFFECTED, VERDICT_FIXED)


ComponentKey = Tuple[str, str, str, str]


@dataclass(frozen=True)
class Component:
    """清单中的一个组件实例（bom.ref 唯一）。"""

    ref: str
    ecosystem: str
    namespace: str
    name: str
    version: str

    @property
    def key(self) -> ComponentKey:
        return (self.ecosystem, self.namespace, self.name, self.version)

    @property
    def coordinate(self) -> Tuple[str, str, str]:
        """不含版本的组件坐标，用于匹配公告 / 适用性记录。"""
        return (self.ecosystem, self.namespace, self.name)

    @property
    def parsed_version(self) -> Optional[Version]:
        return parse_version(self.version)

    @classmethod
    def from_dict(cls, data: dict) -> "Component":
        try:
            ref = str(data["ref"])
            ecosystem = str(data["ecosystem"])
            name = str(data["name"])
        except KeyError as exc:
            raise ValueError(f"组件缺少必填字段 {exc}: {data!r}") from exc
        namespace = str(data.get("namespace", ""))
        version = str(data.get("version", ""))
        if not ref or not ecosystem or not name or not version:
            raise ValueError(f"组件 ref/ecosystem/name/version 不允许为空: {data!r}")
        return cls(ref=ref, ecosystem=ecosystem, namespace=namespace,
                   name=name, version=version)


@dataclass(frozen=True)
class Bom:
    """一份 CycloneDX 风格清单，对应一个产品发行版本的快照。"""

    release_id: str
    snapshot_id: str
    components: Tuple[Component, ...]

    @classmethod
    def from_dict(cls, data: dict) -> "Bom":
        try:
            release_id = str(data["release_id"])
            snapshot_id = str(data["snapshot_id"])
        except KeyError as exc:
            raise ValueError(f"清单缺少必填字段 {exc}") from exc
        components = tuple(Component.from_dict(c) for c in data.get("components", []))
        refs = [c.ref for c in components]
        if len(refs) != len(set(refs)):
            raise ValueError(f"清单 {release_id} 中组件 ref 存在重复")
        return cls(release_id=release_id, snapshot_id=snapshot_id,
                   components=components)


@dataclass(frozen=True)
class Statement:
    """公告内的一条适用性声明。

    kind:
      - "affected"     : affected_intervals 非空，命中即受影响；
      - "not_affected" : 显式豁免，not_affected_versions 精确匹配版本，
                         或 not_affected_intervals 区间匹配。
    一条声明只描述一个组件坐标（不含版本），版本范围由区间 / 精确版本给出。
    """

    statement_id: str
    ecosystem: str
    namespace: str
    name: str
    kind: str
    affected_intervals: Tuple[Interval, ...] = ()
    not_affected_versions: Tuple[str, ...] = ()
    not_affected_intervals: Tuple[Interval, ...] = ()

    @property
    def coordinate(self) -> Tuple[str, str, str]:
        return (self.ecosystem, self.namespace, self.name)

    @classmethod
    def from_dict(cls, data: dict) -> "Statement":
        try:
            sid = str(data["statement_id"])
            kind = str(data["kind"])
            ecosystem = str(data["ecosystem"])
            name = str(data["name"])
        except KeyError as exc:
            raise ValueError(f"公告声明缺少必填字段 {exc}: {data!r}") from exc
        if kind not in ("affected", "not_affected"):
            raise ValueError(f"声明 {sid} 的 kind 非法: {kind!r}")
        namespace = str(data.get("namespace", ""))
        affected = tuple(
            Interval.from_dict(d) for d in data.get("affected_intervals", [])
        )
        not_affected = tuple(
            Interval.from_dict(d) for d in data.get("not_affected_intervals", [])
        )
        versions = tuple(str(v) for v in data.get("not_affected_versions", []))
        return cls(
            statement_id=sid, ecosystem=ecosystem, namespace=namespace, name=name,
            kind=kind, affected_intervals=affected,
            not_affected_versions=versions, not_affected_intervals=not_affected,
        )


@dataclass(frozen=True)
class Advisory:
    """合成漏洞公告（一个 vuln_id 可由多条公告共同描述，允许互相矛盾）。"""

    advisory_id: str
    vuln_id: str
    revised: str
    withdrawn: bool
    statements: Tuple[Statement, ...]
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Advisory":
        try:
            aid = str(data["advisory_id"])
            vuln_id = str(data["vuln_id"])
            revised = str(data["revised"])
        except KeyError as exc:
            raise ValueError(f"公告缺少必填字段 {exc}: {data!r}") from exc
        withdrawn = bool(data.get("withdrawn", False))
        statements = tuple(Statement.from_dict(s) for s in data.get("statements", []))
        ids = [s.statement_id for s in statements]
        if len(ids) != len(set(ids)):
            raise ValueError(f"公告 {aid} 内 statement_id 重复")
        return cls(advisory_id=aid, vuln_id=vuln_id, revised=revised,
                   withdrawn=withdrawn, statements=statements,
                   note=str(data.get("note", "")))


@dataclass(frozen=True)
class ApplicabilityRecord:
    """按产品发行版本签发的适用性记录（厂商/平台侧的人工或机器裁定）。

    记录绑定 bom_snapshot_id：只有与当前清单快照一致时才有效；
    快照不匹配说明记录针对的是旧清单，按“过期记录”处理，不得作为证据，
    但要在原因中保留 record_stale 以便审计。
    """

    record_id: str
    release_id: str
    bom_snapshot_id: str
    ecosystem: str
    namespace: str
    name: str
    version: str
    vuln_id: str
    verdict: str  # affected / not_affected
    reason: str = ""

    @property
    def coordinate(self) -> Tuple[str, str, str]:
        return (self.ecosystem, self.namespace, self.name)

    @classmethod
    def from_dict(cls, data: dict) -> "ApplicabilityRecord":
        required = ("record_id", "release_id", "bom_snapshot_id", "ecosystem",
                    "name", "version", "vuln_id", "verdict")
        try:
            kwargs = {k: str(data[k]) for k in required}
        except KeyError as exc:
            raise ValueError(f"适用性记录缺少必填字段 {exc}: {data!r}") from exc
        if kwargs["verdict"] not in ("affected", "not_affected"):
            raise ValueError(
                f"记录 {kwargs['record_id']} 的 verdict 非法: {kwargs['verdict']!r}"
            )
        return cls(namespace=str(data.get("namespace", "")),
                   reason=str(data.get("reason", "")), **kwargs)


@dataclass(frozen=True)
class Finding:
    """单条裁决结果：一个 (发行版本, 组件实例, 漏洞) 三元组。"""

    release_id: str
    bom_ref: str
    ecosystem: str
    namespace: str
    name: str
    version: str
    vuln_id: Optional[str]
    disposition: str
    evidence: Tuple[str, ...]
    reasons: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def key(self) -> Tuple[str, str, Optional[str]]:
        return (self.release_id, self.bom_ref, self.vuln_id)
