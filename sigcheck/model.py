"""输入数据模型与校验。

输入采用纯 JSON/dict 描述（与具体 PDF 解析器解耦），四类输入对应题面：

1. 按字节范围标记的修订（``revisions``）：每次增量保存保存一段字节区间。
2. 每版新增或替换的对象（``objects``）：同一对象 id 在不同 revision 再次出现
   即表示该版“替换”，digest 相同表示内容未实际变化（重写为原内容）。
3. 从页面到内容或表单值的引用（``references``）：带版本的有向边，
   ``body`` 边表示普通页面正文引用，``field`` 边表示表单字段到其值对象的引用。
4. 已验真的签章摘要（``signatures``）：摘要的真伪由调用方验真，本检查器只消费
   验真结果，并按签章创建当时的实际可达关系计算覆盖范围。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

BODY = "body"
FIELD = "field"
CERTIFYING = "certifying"
APPROVAL = "approval"


class DocumentModelError(ValueError):
    """输入模型本身不合法（结构缺失、编号/区间矛盾等）。"""


@dataclass(frozen=True)
class Revision:
    """一次增量保存。``start < end``，按 start 严格递增、区间不重叠。"""

    revision_id: str
    start: int
    end: int


@dataclass(frozen=True)
class ObjectVersion:
    """某个对象在某次修订中新增或被替换后的版本。"""

    object_id: str
    revision_id: str
    digest: str


@dataclass(frozen=True)
class ReferenceEdge:
    """一条带生命周期的引用边：源对象 -> 目标对象。

    ``added_in`` 给出边出现的修订；``removed_in`` 为空表示一直存活。
    同一条边以 (source_id, target_id, kind, field_name) 标识。
    """

    source_id: str
    target_id: str
    kind: str
    added_in: str
    removed_in: Optional[str] = None
    field_name: Optional[str] = None


@dataclass(frozen=True)
class Signature:
    """已验真的签章摘要。

    ``signature_object_id`` 是签章自身对象（它的覆盖范围不包含自身）。
    认证签章（certifying）可通过 ``allowed_fields`` 允许指定表单字段以后填值；
    认可签章（approval）不允许任何后续填值。
    """

    signature_id: str
    revision_id: str
    byte_range: Tuple[int, int]
    signature_object_id: str
    sig_type: str = CERTIFYING
    allowed_fields: Tuple[str, ...] = field(default_factory=tuple)


@dataclass
class Document:
    """完整的待检查文书模型，并提供按版本重建状态的索引。"""

    revisions: List[Revision]
    objects: List[ObjectVersion]
    references: List[ReferenceEdge]
    signatures: List[Signature]
    page_root_ids: Tuple[str, ...]

    _revision_index: Dict[str, int] = field(init=False, repr=False)
    _events: Dict[int, List[ObjectVersion]] = field(init=False, repr=False)
    _current_version: Dict[int, Dict[str, ObjectVersion]] = field(
        init=False, repr=False
    )
    _edge_events: Dict[int, List[Tuple[str, ReferenceEdge]]] = field(
        init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._validate_and_index()

    @classmethod
    def assemble_unchecked(
        cls,
        revisions: List["Revision"],
        objects: List["ObjectVersion"],
        references: List["ReferenceEdge"],
        signatures: List["Signature"],
        page_root_ids: Tuple[str, ...],
    ) -> "Document":
        """跳过结构校验装配文档（仅供检查器层测试畸形字节区间等场景）。

        生产代码应使用 :class:`Document` 的正常构造或 :func:`load_document`。
        """

        doc = object.__new__(cls)
        doc.revisions = revisions
        doc.objects = objects
        doc.references = references
        doc.signatures = signatures
        doc.page_root_ids = page_root_ids
        doc._revision_index = {
            rev.revision_id: idx for idx, rev in enumerate(revisions)
        }
        doc._events = {idx: [] for idx in range(len(revisions))}
        for ver in objects:
            doc._events[doc._revision_index[ver.revision_id]].append(ver)
        doc._edge_events = {idx: [] for idx in range(len(revisions))}
        for edge in references:
            doc._edge_events[doc._revision_index[edge.added_in]].append(
                ("add", edge)
            )
            if edge.removed_in is not None:
                doc._edge_events[doc._revision_index[edge.removed_in]].append(
                    ("remove", edge)
                )
        doc._current_version = {}
        current: Dict[str, ObjectVersion] = {}
        for idx in range(len(revisions)):
            for ver in doc._events[idx]:
                current[ver.object_id] = ver
            doc._current_version[idx] = dict(current)
        return doc

    # ------------------------------------------------------------------ #
    # 校验与索引
    # ------------------------------------------------------------------ #
    def _validate_and_index(self) -> None:
        if not self.revisions:
            raise DocumentModelError("revisions 不能为空")

        seen_ids: set[str] = set()
        for rev in self.revisions:
            if not rev.revision_id:
                raise DocumentModelError("revision_id 不能为空")
            if rev.revision_id in seen_ids:
                raise DocumentModelError(f"revision_id 重复: {rev.revision_id}")
            seen_ids.add(rev.revision_id)
            if rev.start < 0 or rev.end <= rev.start:
                raise DocumentModelError(
                    f"修订 {rev.revision_id} 的字节范围非法: "
                    f"[{rev.start}, {rev.end})"
                )

        ordered = sorted(self.revisions, key=lambda r: r.start)
        if [r.revision_id for r in ordered] != [r.revision_id for r in self.revisions]:
            raise DocumentModelError("revisions 必须按字节起点严格递增排列")
        for prev, cur in zip(ordered, ordered[1:]):
            if cur.start < prev.end:
                raise DocumentModelError(
                    f"修订字节范围重叠: {prev.revision_id}[{prev.start},{prev.end}) "
                    f"与 {cur.revision_id}[{cur.start},{cur.end})"
                )

        self._revision_index = {
            rev.revision_id: idx for idx, rev in enumerate(self.revisions)
        }

        self._events = {idx: [] for idx in range(len(self.revisions))}
        obj_seen: set[Tuple[str, str]] = set()
        obj_ids: set[str] = set()
        for ver in self.objects:
            if ver.revision_id not in self._revision_index:
                raise DocumentModelError(
                    f"对象 {ver.object_id} 引用了未知修订 {ver.revision_id}"
                )
            if not ver.object_id or not ver.digest:
                raise DocumentModelError("object_id 与 digest 均不能为空")
            key = (ver.object_id, ver.revision_id)
            if key in obj_seen:
                raise DocumentModelError(
                    f"对象 {ver.object_id} 在同一修订 {ver.revision_id} 中出现多次"
                )
            obj_seen.add(key)
            obj_ids.add(ver.object_id)
            self._events[self._revision_index[ver.revision_id]].append(ver)

        edge_keys: set[
            Tuple[str, str, str, Optional[str]]
        ] = set()
        self._edge_events = {idx: [] for idx in range(len(self.revisions))}
        for edge in self.references:
            if edge.kind not in (BODY, FIELD):
                raise DocumentModelError(
                    f"引用边类型只能是 {BODY!r} 或 {FIELD!r}: {edge.kind!r}"
                )
            if edge.kind == FIELD and not edge.field_name:
                raise DocumentModelError("field 类型引用边必须提供 field_name")
            if edge.added_in not in self._revision_index:
                raise DocumentModelError(
                    f"引用边 {edge.source_id}->{edge.target_id} 的 added_in "
                    f"引用未知修订 {edge.added_in}"
                )
            if edge.removed_in is not None:
                if edge.removed_in not in self._revision_index:
                    raise DocumentModelError(
                        f"引用边 {edge.source_id}->{edge.target_id} 的 removed_in "
                        f"引用未知修订 {edge.removed_in}"
                    )
                if (
                    self._revision_index[edge.removed_in]
                    < self._revision_index[edge.added_in]
                ):
                    raise DocumentModelError(
                        f"引用边 {edge.source_id}->{edge.target_id} 的 removed_in "
                        "早于 added_in"
                    )
            key = (edge.source_id, edge.target_id, edge.kind, edge.field_name)
            if key in edge_keys:
                raise DocumentModelError(f"引用边重复定义: {key}")
            edge_keys.add(key)
            self._edge_events[self._revision_index[edge.added_in]].append(
                ("add", edge)
            )
            if edge.removed_in is not None:
                self._edge_events[self._revision_index[edge.removed_in]].append(
                    ("remove", edge)
                )

        sig_ids: set[str] = set()
        for sig in self.signatures:
            if not sig.signature_id:
                raise DocumentModelError("signature_id 不能为空")
            if sig.signature_id in sig_ids:
                raise DocumentModelError(
                    f"signature_id 重复: {sig.signature_id}"
                )
            sig_ids.add(sig.signature_id)
            if sig.revision_id not in self._revision_index:
                raise DocumentModelError(
                    f"签章 {sig.signature_id} 引用未知修订 {sig.revision_id}"
                )
            if sig.sig_type not in (CERTIFYING, APPROVAL):
                raise DocumentModelError(
                    f"签章 {sig.signature_id} 类型非法: {sig.sig_type!r}"
                )
            if sig.sig_type == APPROVAL and sig.allowed_fields:
                raise DocumentModelError(
                    f"认可签章 {sig.signature_id} 不能声明 allowed_fields，"
                    "只有认证签章可以允许表单字段后填值"
                )
            bs, be = sig.byte_range
            if bs < 0 or be <= bs:
                raise DocumentModelError(
                    f"签章 {sig.signature_id} 的 byte_range 非法: {sig.byte_range}"
                )
            # byte_range 与修订区间使用同一套全局字节坐标。
            rev = self.revisions[self._revision_index[sig.revision_id]]
            if not (rev.start <= bs and be <= rev.end):
                raise DocumentModelError(
                    f"签章 {sig.signature_id} 的 byte_range {sig.byte_range} "
                    f"超出其所在修订 {rev.revision_id}[{rev.start},{rev.end})"
                )
            if sig.signature_object_id not in obj_ids:
                raise DocumentModelError(
                    f"签章 {sig.signature_id} 的签章对象 "
                    f"{sig.signature_object_id} 不存在"
                )

        if not self.page_root_ids:
            raise DocumentModelError("page_root_ids 不能为空")
        unknown_roots = [pid for pid in self.page_root_ids if pid not in obj_ids]
        if unknown_roots:
            raise DocumentModelError(f"页面根对象不存在: {unknown_roots}")

        # 预计算每个版本结束时各对象的当前版本。
        self._current_version = {}
        current: Dict[str, ObjectVersion] = {}
        for idx in range(len(self.revisions)):
            for ver in self._events[idx]:
                current[ver.object_id] = ver
            self._current_version[idx] = dict(current)

    # ------------------------------------------------------------------ #
    # 版本化状态查询
    # ------------------------------------------------------------------ #
    @property
    def revision_index(self) -> Dict[str, int]:
        return dict(self._revision_index)

    def object_events_at(self, rev_index: int) -> List[ObjectVersion]:
        return list(self._events[rev_index])

    def active_edges(self, rev_index: int) -> List[ReferenceEdge]:
        """返回截至 ``rev_index`` 该版结束时仍然存活的引用边。"""
        active: Dict[
            Tuple[str, str, str, Optional[str]], ReferenceEdge
        ] = {}
        for idx in range(rev_index + 1):
            for action, edge in self._edge_events[idx]:
                key = (
                    edge.source_id,
                    edge.target_id,
                    edge.kind,
                    edge.field_name,
                )
                if action == "add":
                    active[key] = edge
                else:
                    active.pop(key, None)
        return list(active.values())

    def edge_events_at(
        self, rev_index: int
    ) -> List[Tuple[str, ReferenceEdge]]:
        return list(self._edge_events[rev_index])

    def version_at(
        self, object_id: str, rev_index: int
    ) -> Optional[ObjectVersion]:
        return self._current_version[rev_index].get(object_id)

    def latest_version_at_or_before(
        self, object_id: str, rev_index: int
    ) -> Optional[ObjectVersion]:
        return self.version_at(object_id, rev_index)

    def all_object_ids(self) -> List[str]:
        ids = {ver.object_id for ver in self.objects}
        return sorted(ids)


def load_document(payload: Any) -> Document:
    """从 dict（或 JSON 字符串/字节）构建 :class:`Document`。"""

    if isinstance(payload, (str, bytes, bytearray)):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise DocumentModelError("文档模型必须是 JSON 对象")

    def require(name: str) -> Any:
        if name not in payload:
            raise DocumentModelError(f"缺少必填字段: {name}")
        return payload[name]

    revisions = [
        Revision(
            revision_id=str(item["revision_id"]),
            start=int(item["start"]),
            end=int(item["end"]),
        )
        for item in require("revisions")
    ]

    objects = [
        ObjectVersion(
            object_id=str(item["object_id"]),
            revision_id=str(item["revision_id"]),
            digest=str(item["digest"]),
        )
        for item in require("objects")
    ]

    references = []
    for item in require("references"):
        references.append(
            ReferenceEdge(
                source_id=str(item["source_id"]),
                target_id=str(item["target_id"]),
                kind=str(item["kind"]),
                added_in=str(item["added_in"]),
                removed_in=(
                    str(item["removed_in"])
                    if item.get("removed_in") is not None
                    else None
                ),
                field_name=item.get("field_name"),
            )
        )

    signatures = []
    for item in require("signatures"):
        raw_range = item["byte_range"]
        if not isinstance(raw_range, (list, tuple)) or len(raw_range) != 2:
            raise DocumentModelError(
                f"签章 {item.get('signature_id')} 的 byte_range 必须是 [start, end]"
            )
        allowed = item.get("allowed_fields", []) or []
        signatures.append(
            Signature(
                signature_id=str(item["signature_id"]),
                revision_id=str(item["revision_id"]),
                byte_range=(int(raw_range[0]), int(raw_range[1])),
                signature_object_id=str(item["signature_object_id"]),
                sig_type=str(item.get("sig_type", CERTIFYING)),
                allowed_fields=tuple(str(name) for name in allowed),
            )
        )

    page_roots = tuple(str(pid) for pid in require("page_root_ids"))

    return Document(
        revisions=revisions,
        objects=objects,
        references=references,
        signatures=signatures,
        page_root_ids=page_roots,
    )
