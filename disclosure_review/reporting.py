"""Structured review results and JSON-safe serialization."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from .models import NodeRef, RecordRef
from .ranges import Range, group_numbers_into_ranges


Status = Literal["disclosable", "withheld"]


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    severity: Literal["blocking", "warning"] = "blocking"
    node: NodeRef | None = None
    citation_id: str | None = None
    repair_from: NodeRef | RecordRef | None = None
    chain: tuple[NodeRef, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == "blocking"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "blocking": self.blocking,
            "node": self.node.to_dict() if self.node else None,
            "citation_id": self.citation_id,
            "repair_from": self.repair_from.to_dict() if self.repair_from else None,
            "chain": [node.to_dict() for node in self.chain],
            "detail": self.detail,
        }


@dataclass
class NodeEvaluation:
    node: NodeRef
    approved: bool
    fingerprint: str | None = None
    findings: list[Finding] = field(default_factory=list)
    children: list["NodeEvaluation"] = field(default_factory=list)
    citation_id: str | None = None

    @property
    def reason_codes(self) -> list[str]:
        return sorted({finding.code for finding in self.findings if finding.blocking})

    def iter_findings(self):
        yield from self.findings
        for child in self.children:
            yield from child.iter_findings()

    def iter_nodes(self):
        yield self.node
        for child in self.children:
            yield from child.iter_nodes()

    def to_dict(self, *, depth: int = 0) -> dict[str, Any]:
        return {
            "node": self.node.to_dict(),
            "approved": self.approved,
            "fingerprint": self.fingerprint,
            "citation_id": self.citation_id,
            "reason_codes": self.reason_codes,
            "findings": [finding.to_dict() for finding in self.findings],
            "children": [child.to_dict(depth=depth + 1) for child in self.children],
        }


@dataclass
class ParagraphResult:
    number: int
    text: str
    status: Status
    findings: list[Finding] = field(default_factory=list)
    traces: list[NodeEvaluation] = field(default_factory=list)

    @property
    def reason_codes(self) -> list[str]:
        codes = {finding.code for finding in self.all_findings() if finding.blocking}
        return sorted(codes)

    @property
    def warning_codes(self) -> list[str]:
        codes = {finding.code for finding in self.all_findings() if not finding.blocking}
        return sorted(codes)

    def all_findings(self) -> list[Finding]:
        findings = list(self.findings)
        for trace in self.traces:
            findings.extend(trace.iter_findings())
        return findings

    def source_nodes(self) -> list[NodeRef]:
        nodes: list[NodeRef] = []
        seen: set[tuple[str, str, int]] = set()
        for trace in self.traces:
            for node in trace.iter_nodes():
                key = (node.record_id, node.version, node.paragraph)
                if key not in seen:
                    seen.add(key)
                    nodes.append(node)
        return nodes

    def to_dict(self, *, include_trace: bool = True) -> dict[str, Any]:
        return {
            "paragraph": self.number,
            "status": self.status,
            "text": self.text,
            "reason_codes": self.reason_codes,
            "warning_codes": self.warning_codes,
            "findings": [finding.to_dict() for finding in self.all_findings()],
            "source_nodes": [node.to_dict() for node in self.source_nodes()],
            "traces": [trace.to_dict() for trace in self.traces] if include_trace else [],
        }


@dataclass
class Segment:
    paragraph_range: Range
    status: Status
    paragraphs: list[int]
    text: str
    reason_codes: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "range": self.paragraph_range.to_list(),
            "start": self.paragraph_range.start,
            "end": self.paragraph_range.end,
            "status": self.status,
            "paragraphs": self.paragraphs,
            "text": self.text,
            "reason_codes": self.reason_codes,
            "reasons": [finding.to_dict() for finding in self.findings],
        }


@dataclass
class Remediation:
    code: str
    repair_from: NodeRef | RecordRef
    action: str
    chain: list[NodeRef] = field(default_factory=list)
    citation_id: str | None = None
    affected_submission_paragraphs: list[int] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "repair_from": self.repair_from.to_dict(),
            "action": self.action,
            "chain": [node.to_dict() for node in self.chain],
            "citation_id": self.citation_id,
            "affected_submission_paragraphs": sorted(
                self.affected_submission_paragraphs
            ),
            "detail": self.detail,
        }


@dataclass
class ReviewReport:
    submission: RecordRef
    paragraph_results: list[ParagraphResult]
    segments: list[Segment]
    remediations: list[Remediation] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)

    @property
    def disclosable_segments(self) -> list[Segment]:
        return [segment for segment in self.segments if segment.status == "disclosable"]

    @property
    def withheld_segments(self) -> list[Segment]:
        return [segment for segment in self.segments if segment.status == "withheld"]

    @property
    def disclosable_ranges(self) -> list[list[int]]:
        return [segment.paragraph_range.to_list() for segment in self.disclosable_segments]

    @property
    def withheld_ranges(self) -> list[dict[str, Any]]:
        return [
            {
                "range": segment.paragraph_range.to_list(),
                "reason_codes": segment.reason_codes,
                "reasons": [finding.to_dict() for finding in segment.findings],
            }
            for segment in self.withheld_segments
        ]

    def status_for(self, paragraph: int) -> Status:
        return self.paragraph_results[paragraph - 1].status

    def reason_codes_for(self, paragraph: int) -> list[str]:
        return self.paragraph_results[paragraph - 1].reason_codes

    def disclosable_paragraphs(self) -> list[int]:
        return [result.number for result in self.paragraph_results if result.status == "disclosable"]

    def withheld_paragraphs(self) -> list[int]:
        return [result.number for result in self.paragraph_results if result.status == "withheld"]

    def to_dict(self, *, include_trace: bool = True) -> dict[str, Any]:
        disclosed = self.disclosable_paragraphs()
        withheld = self.withheld_paragraphs()
        return {
            "submission": self.submission.to_dict(),
            "summary": {
                "total_paragraphs": len(self.paragraph_results),
                "disclosable_paragraphs": disclosed,
                "withheld_paragraphs": withheld,
                "disclosable_ranges": [r.to_list() for r in group_numbers_into_ranges(disclosed)],
                "withheld_ranges": self.withheld_ranges,
                "remediation_count": len(self.remediations),
            },
            "disclosable_ranges": self.disclosable_ranges,
            "withheld_ranges": self.withheld_ranges,
            "disclosable_segments": [segment.to_dict() for segment in self.disclosable_segments],
            "withheld_segments": [segment.to_dict() for segment in self.withheld_segments],
            "segments": [segment.to_dict() for segment in self.segments],
            "paragraphs": [
                result.to_dict(include_trace=include_trace)
                for result in self.paragraph_results
            ],
            "remediations": [item.to_dict() for item in self.remediations],
            "validation_errors": self.validation_errors,
        }

    def to_json(self, *, indent: int = 2, include_trace: bool = True) -> str:
        return json.dumps(
            self.to_dict(include_trace=include_trace),
            ensure_ascii=False,
            indent=indent,
        )
