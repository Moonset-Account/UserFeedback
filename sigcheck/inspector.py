"""签章修订检查核心逻辑。

判定模型（与具体 PDF 语法解耦，全部基于 model 层的版本化对象与引用边）：

* 每次修订结束时，根据当时存活的引用边重建引用图，从页面根对象出发做
  BFS 可达性分析：
    - ``body`` 模式只沿普通正文引用（body 边）；
    - ``full`` 模式同时沿正文引用与表单字段引用（body + field 边）。
* 签章创建于修订 s 时，其**签章覆盖范围**固定为 s 当时 body 可达的对象
  （不含任何签章自身对象）。之后的检查始终对照这份固定覆盖范围，因此
  “后来把文字改回原样”不会消除早先的越界记录。
* 认证签章允许的后填表单字段：s 当时通过允许字段的 field 边可达、且不在
  签章覆盖范围内的对象。若某对象同时被 body 边可达（即也是已签正文的一
  部分），它不在允许集合内，修改即越界。
* 对 s 之后的每次修订 t 检查四类事件：
    1. t 的字节区间与签章 byte_range 重叠 -> byte_range_tamper 越界；
    2. 覆盖范围内的对象在 t 被替换且 digest 变化：
       内容重写为原样（digest 与上一版相同）记 benign_rewrite 信息；
       当版仍只经允许字段可达时记 allowed_field_fill 信息；
       其余记 object_modified 越界，并给出一条从页面根到该对象的
       body 引用链；
    3. s 当时已存在、源对象处于覆盖范围内的引用边在 t 被移除
       -> removed_reference 越界（链路由签章当时的引用图给出）；
    4. 仅经允许字段可达的对象在某版“升格”为正文对象（新增 body 边使其
       body 可达），且该版其内容发生替换 -> object_modified 越界，链路由
       该版实际引用图给出；升格之后的后续替换继续按正文规则逐版留痕。
  覆盖范围之外的新增对象、以及纯新增引用边不构成越界（增量保存允许追加）。
* 后来新增的签章从自己所在修订重新计算覆盖范围，各自独立评估。
* 所有发现按修订顺序累积，不随后续修订（包括“改回原样”）而删除，因此
  最早越界修订始终可查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .model import (
    APPROVAL,
    BODY,
    CERTIFYING,
    FIELD,
    Document,
    ReferenceEdge,
    Signature,
)

# 规则编号
R_BYTE_RANGE_TAMPER = "byte_range_tamper"
R_OBJECT_MODIFIED = "object_modified"
R_REMOVED_REFERENCE = "removed_reference"
R_BENIGN_REWRITE = "benign_rewrite"
R_ALLOWED_FIELD_FILL = "allowed_field_fill"
R_INEFFECTIVE_ALLOWANCE = "ineffective_allowance"

VIOLATION = "violation"
INFO = "info"


@dataclass(frozen=True)
class Hop:
    """引用链上的一跳。"""

    source_id: str
    target_id: str
    kind: str
    field_name: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        data: Dict[str, object] = {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "kind": self.kind,
        }
        if self.field_name is not None:
            data["field_name"] = self.field_name
        return data


@dataclass(frozen=True)
class Finding:
    """一次检查发现。severity 为 violation 或 info。"""

    rule_id: str
    severity: str
    revision_id: str
    signature_id: str
    message: str
    chain: Tuple[Hop, ...] = field(default_factory=tuple)
    object_id: Optional[str] = None
    field_name: Optional[str] = None
    byte_range: Optional[Tuple[int, int]] = None

    def to_dict(self) -> Dict[str, object]:
        data: Dict[str, object] = {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "revision_id": self.revision_id,
            "signature_id": self.signature_id,
            "message": self.message,
            "chain": [hop.to_dict() for hop in self.chain],
        }
        if self.object_id is not None:
            data["object_id"] = self.object_id
        if self.field_name is not None:
            data["field_name"] = self.field_name
        if self.byte_range is not None:
            data["byte_range"] = list(self.byte_range)
        return data


@dataclass
class SignatureAssessment:
    """单个签章相对于其后所有修订的评估结果。"""

    signature_id: str
    signed_revision_id: str
    sig_type: str
    covered_object_ids: Tuple[str, ...]
    allowed_fill_object_ids: Tuple[str, ...]
    findings: List[Finding] = field(default_factory=list)

    @property
    def violated(self) -> bool:
        return any(item.severity == VIOLATION for item in self.findings)

    @property
    def earliest_violation(self) -> Optional[Finding]:
        for item in self.findings:
            if item.severity == VIOLATION:
                return item
        return None

    def to_dict(self) -> Dict[str, object]:
        return {
            "signature_id": self.signature_id,
            "signed_revision_id": self.signed_revision_id,
            "sig_type": self.sig_type,
            "violated": self.violated,
            "covered_object_ids": list(self.covered_object_ids),
            "allowed_fill_object_ids": list(self.allowed_fill_object_ids),
            "earliest_violation": (
                self.earliest_violation.to_dict()
                if self.earliest_violation is not None
                else None
            ),
            "findings": [item.to_dict() for item in self.findings],
        }


@dataclass
class InspectionReport:
    """整份文书的检查报告。"""

    assessments: List[SignatureAssessment]
    revision_order: Tuple[str, ...]

    @property
    def violated(self) -> bool:
        return any(assessment.violated for assessment in self.assessments)

    @property
    def earliest_violation(self) -> Optional[Finding]:
        """全文书最早的越界发现（按修订先后；同修订按签章输入顺序）。"""

        position = {rev_id: idx for idx, rev_id in enumerate(self.revision_order)}
        best: Optional[Finding] = None
        for assessment in self.assessments:
            finding = assessment.earliest_violation
            if finding is None:
                continue
            if best is None or position[finding.revision_id] < position[
                best.revision_id
            ]:
                best = finding
        return best

    def assessment_for(self, signature_id: str) -> SignatureAssessment:
        for assessment in self.assessments:
            if assessment.signature_id == signature_id:
                return assessment
        raise KeyError(f"未知签章: {signature_id}")

    def to_dict(self) -> Dict[str, object]:
        earliest = self.earliest_violation
        return {
            "violated": self.violated,
            "earliest_violation": earliest.to_dict() if earliest else None,
            "assessments": [item.to_dict() for item in self.assessments],
        }

    def to_json(self, indent: int = 2) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ---------------------------------------------------------------------- #
# 可达性分析
# ---------------------------------------------------------------------- #
def _adjacency(
    edges: Sequence[ReferenceEdge], modes: Tuple[str, ...]
) -> Dict[str, List[ReferenceEdge]]:
    graph: Dict[str, List[ReferenceEdge]] = {}
    for edge in edges:
        if edge.kind in modes:
            graph.setdefault(edge.source_id, []).append(edge)
    for out_edges in graph.values():
        out_edges.sort(key=lambda e: (e.target_id, e.kind, e.field_name or ""))
    return graph


def reachable_chains(
    document: Document,
    rev_index: int,
    roots: Sequence[str],
    modes: Tuple[str, ...],
    excluded: Optional[set[str]] = None,
) -> Dict[str, Tuple[Hop, ...]]:
    """从 ``roots`` 沿指定类型的边做 BFS，返回每个可达对象到根的一条引用链。

    链以 Hop 元组表示，根对象自身对应空链。``excluded`` 中的对象（如签章
    自身对象）既不入队也不会出现在结果中。引用边指向一个在该版本尚不存在
    的对象时，该目标视为悬空，不入链、不继续遍历。
    """

    excluded = excluded or set()
    graph = _adjacency(document.active_edges(rev_index), modes)
    existing = set(document._current_version[rev_index].keys())

    chains: Dict[str, Tuple[Hop, ...]] = {}
    queue: List[str] = []
    for root in roots:
        if root in excluded or root in chains:
            continue
        chains[root] = ()
        queue.append(root)

    head = 0
    while head < len(queue):
        node = queue[head]
        head += 1
        for edge in graph.get(node, []):
            target = edge.target_id
            if target in excluded or target in chains:
                continue
            if target not in existing:
                # 悬空引用：目标对象在该版本不存在。
                continue
            chains[target] = chains[node] + (
                Hop(
                    source_id=edge.source_id,
                    target_id=edge.target_id,
                    kind=edge.kind,
                    field_name=edge.field_name,
                ),
            )
            queue.append(target)
    return chains


class _ReachabilityCache:
    def __init__(self, document: Document) -> None:
        self._doc = document
        self._body: Dict[int, Dict[str, Tuple[Hop, ...]]] = {}
        self._full: Dict[int, Dict[str, Tuple[Hop, ...]]] = {}

    def body(self, rev_index: int, excluded: set[str]) -> Dict[str, Tuple[Hop, ...]]:
        key = rev_index
        if key not in self._body:
            self._body[key] = reachable_chains(
                self._doc,
                rev_index,
                self._doc.page_root_ids,
                (BODY,),
            )
        return {
            oid: chain
            for oid, chain in self._body[key].items()
            if oid not in excluded
        }

    def full(self, rev_index: int, excluded: set[str]) -> Dict[str, Tuple[Hop, ...]]:
        key = rev_index
        if key not in self._full:
            self._full[key] = reachable_chains(
                self._doc,
                rev_index,
                self._doc.page_root_ids,
                (BODY, FIELD),
            )
        return {
            oid: chain
            for oid, chain in self._full[key].items()
            if oid not in excluded
        }


def _ranges_overlap(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


# ---------------------------------------------------------------------- #
# 主入口
# ---------------------------------------------------------------------- #
def inspect_document(document: Document) -> InspectionReport:
    """按各次修订当时实际可达的对象和签章覆盖范围执行越界检查。"""

    revisions = document.revisions
    rev_count = len(revisions)
    cache = _ReachabilityCache(document)
    all_signature_objects = {
        sig.signature_object_id for sig in document.signatures
    }

    assessments: List[SignatureAssessment] = []

    for sig in document.signatures:
        sign_idx = document.revision_index[sig.revision_id]
        body_at_sign = cache.body(sign_idx, all_signature_objects)
        full_at_sign = cache.full(sign_idx, all_signature_objects)

        # 签章覆盖范围：签章当时从页面根沿 body 边可达的已签正文对象。
        # 页面根（空链）天然在内；只有 field 链可达的表单字典/值对象不在内。
        covered = {
            oid
            for oid, chain in body_at_sign.items()
            if not chain or any(hop.kind == BODY for hop in chain)
        }

        # 签章当时已经通过允许字段可达、且不属于已签正文的对象，
        # 作为“静态可见”的允许后填集合输出；真正的豁免在每次替换事件发生时
        # 再按当时的 field 引用动态判定（允许字段可以在签章之后才接上值）。
        allowed_at_sign: set[str] = set()
        ineffective_at_sign: list[ReferenceEdge] = []
        if sig.sig_type == CERTIFYING and sig.allowed_fields:
            for edge in document.active_edges(sign_idx):
                if edge.kind != FIELD:
                    continue
                if edge.field_name not in sig.allowed_fields:
                    continue
                target = edge.target_id
                if target in all_signature_objects or target not in full_at_sign:
                    continue
                if target in covered:
                    ineffective_at_sign.append(edge)
                else:
                    allowed_at_sign.add(target)

        findings: List[Finding] = []
        # 记录允许后填对象“升格”为正文对象的首版；升格之后该对象的每次
        # 真实替换仍按正文规则逐版留痕，此映射只用于让后续版本持续命中
        # 正文判定分支（该对象不在签章当时的固定覆盖快照里）。
        promoted_revisions: Dict[str, str] = {}

        # 对允许字段指向已签正文对象的情形给出提示，便于审核岗理解
        # 为什么某个“允许字段”没有豁免效果。
        for edge in ineffective_at_sign:
            findings.append(
                Finding(
                    rule_id=R_INEFFECTIVE_ALLOWANCE,
                    severity=INFO,
                    revision_id=sig.revision_id,
                    signature_id=sig.signature_id,
                    message=(
                        f"允许字段 {edge.field_name} 指向的对象 "
                        f"{edge.target_id} 同时被正文引用，属于签章覆盖 "
                        "范围，允许后填对该对象无效"
                    ),
                    chain=body_at_sign[edge.target_id],
                    object_id=edge.target_id,
                    field_name=edge.field_name,
                )
            )

        for rev_idx in range(sign_idx + 1, rev_count):
            revision = revisions[rev_idx]

            # 1) 字节范围重叠：直接改写了签章摘要所覆盖的字节。
            if _ranges_overlap(
                (revision.start, revision.end), sig.byte_range
            ):
                findings.append(
                    Finding(
                        rule_id=R_BYTE_RANGE_TAMPER,
                        severity=VIOLATION,
                        revision_id=revision.revision_id,
                        signature_id=sig.signature_id,
                        message=(
                            f"修订 {revision.revision_id} 的字节区间 "
                            f"[{revision.start}, {revision.end}) 侵入签章 "
                            f"{sig.signature_id} 的已验真字节范围 "
                            f"[{sig.byte_range[0]}, {sig.byte_range[1]})"
                        ),
                        byte_range=(revision.start, revision.end),
                    )
                )

            # 该版结束时的实际可达关系：判断字段豁免是否仍有效，必须以
            # “修订当时”的正文可达性为准，而不是只看签章当时的固定快照。
            body_now = cache.body(rev_idx, all_signature_objects)
            full_now = cache.full(rev_idx, all_signature_objects)

            # 2) 对象的新增/替换事件分两路处理：
            #    - 覆盖范围内对象：与上一版 digest 相同记 benign；当版仍只
            #      经允许字段可达时记 allowed_field_fill；当版已被正文引用
            #      则一律按 object_modified 越界（题面：同一对象被普通页面
            #      正文引用，改它会改变已签正文）。
            #    - 覆盖范围外对象：纯新增不报告；已存在对象的替换仅在属于
            #      允许字段后填时记 info；若该对象已升格为正文对象则按越界。
            for version in document.object_events_at(rev_idx):
                obj_id = version.object_id
                prior = document.version_at(obj_id, rev_idx - 1)
                existed_before = prior is not None

                if obj_id not in covered:
                    if not existed_before:
                        # 覆盖范围外的全新对象：增量追加，不报告。
                        continue
                    if obj_id in body_now and (
                        obj_id in allowed_at_sign
                        or obj_id in promoted_revisions
                    ):
                        # 允许字段对象在本版同时成为正文对象且发生了替换：
                        # 字段豁免在本版失效，按正文越界处理；升格首版之外，
                        # 后续每个真实替换版本继续按正文规则留痕。
                        if obj_id not in promoted_revisions:
                            promoted_revisions[obj_id] = revision.revision_id
                        findings.append(
                            Finding(
                                rule_id=R_OBJECT_MODIFIED,
                                severity=VIOLATION,
                                revision_id=revision.revision_id,
                                signature_id=sig.signature_id,
                                message=(
                                    f"对象 {obj_id} 在修订 "
                                    f"{revision.revision_id} 已被普通页面正文"
                                    f"引用，其变化（{prior.digest} -> "
                                    f"{version.digest}）会改变已签正文，"
                                    "表单字段豁免对正文对象无效"
                                ),
                                chain=body_now[obj_id],
                                object_id=obj_id,
                            )
                        )
                        continue
                    allowed_field = _dynamic_allowed_field(
                        document=document,
                        rev_idx=rev_idx,
                        target_id=obj_id,
                        sig=sig,
                        body_now=body_now,
                    )
                    if allowed_field is not None:
                        findings.append(
                            Finding(
                                rule_id=R_ALLOWED_FIELD_FILL,
                                severity=INFO,
                                revision_id=revision.revision_id,
                                signature_id=sig.signature_id,
                                message=(
                                    f"对象 {obj_id} 在修订 "
                                    f"{revision.revision_id} 的变化属于签章允许"
                                    f"的表单字段后填（字段 {allowed_field}），"
                                    "不构成越界"
                                ),
                                chain=full_now.get(obj_id, ()),
                                object_id=obj_id,
                                field_name=allowed_field,
                            )
                        )
                    continue

                if prior is None:
                    # 理论上不可达：covered 中的对象在签章当时必然已存在。
                    continue
                if version.digest == prior.digest:
                    findings.append(
                        Finding(
                            rule_id=R_BENIGN_REWRITE,
                            severity=INFO,
                            revision_id=revision.revision_id,
                            signature_id=sig.signature_id,
                            message=(
                                f"覆盖对象 {obj_id} 在修订 "
                                f"{revision.revision_id} 被重写，但摘要 "
                                f"{version.digest} 与原内容一致，正文未变化"
                            ),
                            chain=body_at_sign[obj_id],
                            object_id=obj_id,
                        )
                    )
                    continue

                allowed_field = _dynamic_allowed_field(
                    document=document,
                    rev_idx=rev_idx,
                    target_id=obj_id,
                    sig=sig,
                    body_now=body_now,
                )
                if allowed_field is not None:
                    findings.append(
                        Finding(
                            rule_id=R_ALLOWED_FIELD_FILL,
                            severity=INFO,
                            revision_id=revision.revision_id,
                            signature_id=sig.signature_id,
                            message=(
                                f"对象 {obj_id} 在修订 {revision.revision_id} "
                                f"的变化属于签章允许的表单字段后填"
                                f"（字段 {allowed_field}），不构成越界"
                            ),
                            chain=full_now.get(obj_id, full_at_sign.get(obj_id, ())),
                            object_id=obj_id,
                            field_name=allowed_field,
                        )
                    )
                else:
                    reason = _violation_reason(sig, obj_id, body_now)
                    findings.append(
                        Finding(
                            rule_id=R_OBJECT_MODIFIED,
                            severity=VIOLATION,
                            revision_id=revision.revision_id,
                            signature_id=sig.signature_id,
                            message=(
                                f"签章覆盖对象 {obj_id} 在修订 "
                                f"{revision.revision_id} 被替换且摘要由 "
                                f"{prior.digest} 变为 {version.digest}：{reason}"
                            ),
                            chain=body_at_sign[obj_id],
                            object_id=obj_id,
                        )
                    )

            # 3) 引用边移除：签章当时已存在、源对象在覆盖范围内的边被删除。
            for action, edge in document.edge_events_at(rev_idx):
                if action != "remove":
                    continue
                if edge.source_id not in covered:
                    continue
                # 签章当时该边必须存活（added_in <= sign_idx，且此刻才移除）。
                added_idx = document.revision_index[edge.added_in]
                if added_idx > sign_idx:
                    continue
                base_chain = body_at_sign.get(edge.source_id, ())
                removed_hop = Hop(
                    source_id=edge.source_id,
                    target_id=edge.target_id,
                    kind=edge.kind,
                    field_name=edge.field_name,
                )
                findings.append(
                    Finding(
                        rule_id=R_REMOVED_REFERENCE,
                        severity=VIOLATION,
                        revision_id=revision.revision_id,
                        signature_id=sig.signature_id,
                        message=(
                            f"修订 {revision.revision_id} 移除了签章 "
                            f"{sig.signature_id} 覆盖范围内的引用 "
                            f"{edge.source_id} -> {edge.target_id}（{edge.kind}），"
                            "已签正文结构被改变"
                        ),
                        chain=base_chain + (removed_hop,),
                        object_id=edge.target_id,
                        field_name=edge.field_name,
                    )
                )

        assessments.append(
            SignatureAssessment(
                signature_id=sig.signature_id,
                signed_revision_id=sig.revision_id,
                sig_type=sig.sig_type,
                covered_object_ids=tuple(sorted(covered)),
                allowed_fill_object_ids=tuple(sorted(allowed_at_sign)),
                findings=findings,
            )
        )

    return InspectionReport(
        assessments=assessments,
        revision_order=tuple(rev.revision_id for rev in revisions),
    )


def _dynamic_allowed_field(
    document: Document,
    rev_idx: int,
    target_id: str,
    sig: Signature,
    body_now: Dict[str, Tuple[Hop, ...]],
) -> Optional[str]:
    """判定替换事件发生当时，目标对象是否属于认证签章允许的后填字段。

    条件（全部满足才豁免）：
      1. 认证签章且声明了 allowed_fields；
      2. 该修订当时存在一条指向目标对象的存活 field 边，且字段名在允许名单内；
      3. 目标对象在该修订当时**不**经 body 边可达 —— 一旦它同时是普通页面
         正文的一部分，改它就是改已签正文，豁免失效。
    """

    if sig.sig_type != CERTIFYING or not sig.allowed_fields:
        return None
    if target_id in body_now:
        return None
    for edge in document.active_edges(rev_idx):
        if (
            edge.kind == FIELD
            and edge.target_id == target_id
            and edge.field_name in sig.allowed_fields
        ):
            return edge.field_name
    return None


def _violation_reason(
    sig: Signature,
    obj_id: str,
    body_now: Dict[str, Tuple[Hop, ...]],
) -> str:
    if obj_id in body_now:
        return (
            "该对象被普通页面正文引用，修改会改变已签正文；"
            "表单字段豁免不适用于正文对象"
        )
    if sig.sig_type == APPROVAL:
        return "认可签章不允许任何后续表单填值或对象修改"
    return "该对象不属于签章允许后填的表单字段范围"
