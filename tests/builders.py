"""测试用文书模型构造器：用链式小工具快速拼出版本化模型。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from sigcheck.model import (
    APPROVAL,
    BODY,
    CERTIFYING,
    FIELD,
    Document,
    ObjectVersion,
    ReferenceEdge,
    Revision,
    Signature,
    load_document,
)


class DocBuilder:
    """按修订顺序累积对象、引用与签章，最后生成 :class:`Document`。"""

    def __init__(self, revision_spans: Sequence[Tuple[str, int, int]]) -> None:
        self.revisions = [
            Revision(revision_id=rid, start=start, end=end)
            for rid, start, end in revision_spans
        ]
        self.rev_ids = [rid for rid, _, _ in revision_spans]
        self.objects: List[ObjectVersion] = []
        self.edges: List[ReferenceEdge] = []
        self.signatures: List[Signature] = []
        self.page_roots: List[str] = []

    def add_object(self, object_id: str, revision_id: str, digest: str) -> "DocBuilder":
        self.objects.append(
            ObjectVersion(
                object_id=object_id, revision_id=revision_id, digest=digest
            )
        )
        return self

    def add_body_edge(
        self,
        source_id: str,
        target_id: str,
        added_in: str,
        removed_in: Optional[str] = None,
    ) -> "DocBuilder":
        self.edges.append(
            ReferenceEdge(
                source_id=source_id,
                target_id=target_id,
                kind=BODY,
                added_in=added_in,
                removed_in=removed_in,
            )
        )
        return self

    def add_field_edge(
        self,
        source_id: str,
        target_id: str,
        field_name: str,
        added_in: str,
        removed_in: Optional[str] = None,
    ) -> "DocBuilder":
        self.edges.append(
            ReferenceEdge(
                source_id=source_id,
                target_id=target_id,
                kind=FIELD,
                added_in=added_in,
                removed_in=removed_in,
                field_name=field_name,
            )
        )
        return self

    def set_page_roots(self, *object_ids: str) -> "DocBuilder":
        self.page_roots = list(object_ids)
        return self

    def add_signature(
        self,
        signature_id: str,
        revision_id: str,
        byte_range: Tuple[int, int],
        signature_object_id: str,
        sig_type: str = CERTIFYING,
        allowed_fields: Sequence[str] = (),
    ) -> "DocBuilder":
        self.signatures.append(
            Signature(
                signature_id=signature_id,
                revision_id=revision_id,
                byte_range=byte_range,
                signature_object_id=signature_object_id,
                sig_type=sig_type,
                allowed_fields=tuple(allowed_fields),
            )
        )
        return self

    def build(self) -> Document:
        return Document(
            revisions=list(self.revisions),
            objects=list(self.objects),
            references=list(self.edges),
            signatures=list(self.signatures),
            page_root_ids=tuple(self.page_roots),
        )

    def build_with_revisions(
        self, revisions: List[Revision]
    ) -> Document:
        """用外部给定的修订列表装配，绕过构造期的区间校验。

        仅供检查器层测试“重叠修订应被判为字节篡改”这类模型层拒绝、
        但检查器需要能识别的场景。
        """

        return Document.assemble_unchecked(
            revisions=revisions,
            objects=list(self.objects),
            references=list(self.edges),
            signatures=list(self.signatures),
            page_root_ids=tuple(self.page_roots),
        )

    def build_via_dict(self) -> Document:
        """走一遍 load_document 序列化路径，验证 JSON 形态同样可用。"""

        payload: Dict[str, Any] = {
            "revisions": [
                {"revision_id": r.revision_id, "start": r.start, "end": r.end}
                for r in self.revisions
            ],
            "objects": [
                {
                    "object_id": o.object_id,
                    "revision_id": o.revision_id,
                    "digest": o.digest,
                }
                for o in self.objects
            ],
            "references": [
                {
                    "source_id": e.source_id,
                    "target_id": e.target_id,
                    "kind": e.kind,
                    "added_in": e.added_in,
                    "removed_in": e.removed_in,
                    "field_name": e.field_name,
                }
                for e in self.edges
            ],
            "signatures": [
                {
                    "signature_id": s.signature_id,
                    "revision_id": s.revision_id,
                    "byte_range": list(s.byte_range),
                    "signature_object_id": s.signature_object_id,
                    "sig_type": s.sig_type,
                    "allowed_fields": list(s.allowed_fields),
                }
                for s in self.signatures
            ],
            "page_root_ids": list(self.page_roots),
        }
        return load_document(payload)


def standard_revisions(count: int = 5, step: int = 100) -> List[Tuple[str, int, int]]:
    """生成 r0..r(count-1) 互不重叠、连续排列的字节区间。"""

    return [
        (f"r{i}", i * step, (i + 1) * step) for i in range(count)
    ]
