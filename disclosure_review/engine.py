"""Adjudication engine for nested citations and content-bound approvals."""

from __future__ import annotations

from typing import Any

from .hashing import fingerprint_text
from .models import (
    CarryForward,
    NodeRef,
    RecordRef,
    ReviewCase,
    ReviewPolicy,
    VersionedRecord,
)
from .registry import MaterialRegistry
from .reporting import (
    Finding,
    NodeEvaluation,
    ParagraphResult,
    Remediation,
    ReviewReport,
    Segment,
)
from .ranges import Range


_ACTION_BY_CODE = {
    "MATERIAL_NOT_FOUND": "先补入 repair_from 所指的材料及实际使用版本，再沿引用链重新核查。",
    "VERSION_NOT_FOUND": "确认调查员实际使用的版本，并补入该精确版本；不要用其他版本替代裁决。",
    "PARAGRAPH_NOT_FOUND": "校正引用范围，或从 repair_from 所指记录补入缺失段落后再核查。",
    "SOURCE_PARAGRAPH_NOT_FOUND": "校正引用范围，或从 repair_from 所指记录补入缺失段落后再核查。",
    "CITATION_CYCLE": "从 repair_from 所指的循环入口记录开始，断开循环引用或补入独立的原始来源。",
    "CARRY_FORWARD_CYCLE": "从 repair_from 所指的循环入口版本开始，修正原样承接声明链。",
    "CARRY_FORWARD_SOURCE_MISSING": "补入承接声明指向的先前复核版本，或把该段作为改写内容重新确认。",
    "BASELINE_RECORD_MISSING": "补入修订基线版本后再核对原样承接声明。",
    "INVALID_CITATION_MAP": "修正引用范围或段落映射，使每个映射目标都位于声明的来源区间内。",
}


class ReviewEngine:
    def __init__(
        self,
        registry: MaterialRegistry | None = None,
        policy: ReviewPolicy | None = None,
    ) -> None:
        self.registry = registry or MaterialRegistry()
        self.policy = policy or ReviewPolicy()
        self.baseline_submission: RecordRef | None = None
        self.submission_ref: RecordRef | None = None

    @classmethod
    def from_case(cls, case: ReviewCase) -> "ReviewEngine":
        engine = cls(policy=case.policy)
        for record in case.records:
            engine.registry.add_record(record)
        for approval in case.approvals:
            engine.registry.add_approval(approval)
        for carry in case.carry_forwards:
            engine.registry.add_carry_forward(carry)
        # The reviewed submission is also addressable so carry-forward links and
        # content fingerprints can be verified through one lookup path.
        engine.registry.records[case.submission.ref.key] = case.submission
        engine.baseline_submission = case.baseline_submission
        engine.submission_ref = case.submission.ref
        return engine

    def review_case(self, case: ReviewCase) -> ReviewReport:
        engine = self.from_case(case)
        return engine.review_current(case.submission)

    def review_current(self, submission: VersionedRecord) -> ReviewReport:
        self.submission_ref = submission.ref
        self.registry.records[submission.ref.key] = submission
        validation_errors = self._input_validation_errors()

        results: list[ParagraphResult] = []
        for number in range(1, submission.paragraph_count + 1):
            text = submission.paragraphs.get(number, "")
            root = NodeRef.from_record(submission.ref, number)
            trace = self._visit(root, (), content_rule="submission_root")
            status = "disclosable" if trace.approved else "withheld"
            results.append(
                ParagraphResult(
                    number=number,
                    text=text,
                    status=status,  # type: ignore[arg-type]
                    traces=[trace],
                )
            )

        segments = self._build_segments(results)
        remediations = self._build_remediations(results)
        return ReviewReport(
            submission=submission.ref,
            paragraph_results=results,
            segments=segments,
            remediations=remediations,
            validation_errors=validation_errors,
        )

    # ------------------------------------------------------------------
    # Graph traversal
    # ------------------------------------------------------------------

    def _visit(
        self,
        node: NodeRef,
        ancestors: tuple[NodeRef, ...],
        *,
        incoming_kind: str | None = None,
        incoming_id: str | None = None,
        content_rule: str = "material",
    ) -> NodeEvaluation:
        if node in ancestors:
            chain = ancestors + (node,)
            if incoming_kind == "carry":
                code = "CARRY_FORWARD_CYCLE"
                message = "原样承接声明形成循环，无法确定最初已复核内容。"
            else:
                code = "CITATION_CYCLE"
                message = "引用链形成循环，无法追到独立的已批准来源。"
            finding = Finding(
                code=code,
                message=message,
                node=node,
                citation_id=incoming_id or None,
                repair_from=node,
                chain=chain,
                detail={"cycle": [str(item) for item in chain]},
            )
            return NodeEvaluation(
                node=node,
                approved=False,
                findings=[finding],
                citation_id=incoming_id or None,
            )

        path = ancestors + (node,)
        record = self.registry.get(node.record)
        if record is None:
            versions = self.registry.versions_for(node.record_id)
            if versions:
                code = "VERSION_NOT_FOUND"
                message = (
                    f"材料 {node.record_id} 存在，但缺少实际引用的版本 {node.version}；"
                    f"可用版本为 {', '.join(versions)}。"
                )
            else:
                code = "MATERIAL_NOT_FOUND"
                message = f"引用的材料 {node.record_id}@{node.version} 未提供。"
            finding = Finding(
                code=code,
                message=message,
                node=node,
                repair_from=node.record,
                chain=path,
                detail={"available_versions": versions},
            )
            return NodeEvaluation(node=node, approved=False, findings=[finding])

        if node.paragraph not in record.paragraphs:
            finding = Finding(
                code="PARAGRAPH_NOT_FOUND",
                message=(
                    f"{node.record} 没有第 {node.paragraph} 段；引用范围可能越界，"
                    "或该段落尚未补入。"
                ),
                node=node,
                repair_from=node,
                chain=path,
                detail={"paragraph_count": record.paragraph_count},
            )
            return NodeEvaluation(node=node, approved=False, findings=[finding])

        actual_hash = fingerprint_text(record.paragraphs[node.paragraph])
        findings: list[Finding] = []
        children: list[NodeEvaluation] = []

        direct_ok, direct_covered, direct_withheld, direct_findings = self._direct_approval(
            node, actual_hash, path
        )
        findings.extend(direct_findings)

        for citation in record.citation_for_paragraph(node.paragraph):
            try:
                target_numbers = citation.targets_for(node.paragraph)
            except ValueError as exc:
                findings.append(
                    Finding(
                        code="INVALID_CITATION_MAP",
                        message=str(exc),
                        node=node,
                        citation_id=citation.citation_id,
                        repair_from=node,
                        chain=path,
                    )
                )
                continue

            if citation.verbatim and self.policy.verify_verbatim_citations:
                mismatch = self._verbatim_mismatch_finding(
                    node, record.paragraphs[node.paragraph], citation, target_numbers, path
                )
                if mismatch is not None:
                    findings.append(mismatch)

            for target_number in target_numbers:
                target_node = NodeRef.from_record(citation.source, target_number)
                child = self._visit(
                    target_node,
                    path,
                    incoming_kind="citation",
                    incoming_id=citation.citation_id,
                )
                children.append(child)

        carry_ok = False
        carry_attempted = False
        if not direct_ok and not direct_withheld:
            carry_findings, carry_children, carry_ok, carry_attempted = (
                self._carry_entitlement(node, path, content_rule=content_rule)
            )
            findings.extend(carry_findings)
            children.extend(carry_children)

        own_blocking = any(finding.blocking for finding in findings)

        if content_rule == "material":
            if not direct_ok and not carry_ok and not own_blocking:
                if direct_covered:
                    code = "APPROVAL_SCOPE_NOT_EFFECTIVE"
                    message = "该段虽出现在批准记录中，但当前无法形成有效的内容绑定批准。"
                else:
                    code = "NOT_IN_APPROVED_SCOPE"
                    message = "该材料段落不在任何已批准公开范围内。"
                findings.append(
                    Finding(code=code, message=message, node=node, chain=path)
                )
        elif content_rule == "submission_root" and not direct_ok and not carry_ok:
            if not carry_attempted:
                revision_finding = self._submission_without_entitlement(node, record, path)
                if revision_finding is not None:
                    findings.append(revision_finding)

        child_blocking = any(not child.approved for child in children)
        final_blocking = any(finding.blocking for finding in findings) or child_blocking
        return NodeEvaluation(
            node=node,
            approved=not final_blocking,
            fingerprint=actual_hash,
            findings=findings,
            children=children,
            citation_id=incoming_id or None,
        )

    # ------------------------------------------------------------------
    # Approval and revision rules
    # ------------------------------------------------------------------

    def _direct_approval(
        self, node: NodeRef, actual_hash: str, path: tuple[NodeRef, ...]
    ) -> tuple[bool, bool, bool, list[Finding]]:
        findings: list[Finding] = []
        covered = False
        withheld = False
        candidate = False

        for approval in self.registry.approvals_for(node.record):
            approved_numbers = approval.approved_numbers()
            withheld_numbers = approval.withheld_numbers()

            if node.paragraph in withheld_numbers:
                covered = True
                withheld = True
                expected_hash = (
                    approval.reviewed_hashes.get(node.paragraph)
                    if approval.reviewed_hashes is not None
                    else None
                )
                if expected_hash is not None and expected_hash != actual_hash:
                    findings.append(
                        Finding(
                            code="WITHHELD_SCOPE_CONTENT_CHANGED",
                            message=(
                                "留置范围绑定的内容指纹与当前段落不一致；改写后必须重新审查，"
                                "在新决定前继续留置。"
                            ),
                            node=node,
                            chain=path,
                            detail={
                                "approval_id": approval.approval_id,
                                "expected_fingerprint": expected_hash,
                                "actual_fingerprint": actual_hash,
                            },
                        )
                    )
                else:
                    findings.append(
                        Finding(
                            code="EXPLICITLY_WITHHELD",
                            message="该段属于材料批准决定中明确留置/不予公开的范围。",
                            node=node,
                            chain=path,
                            detail={"approval_id": approval.approval_id},
                        )
                    )

            if node.paragraph in approved_numbers:
                covered = True
                if approval.reviewed_hashes is None:
                    candidate = True
                    findings.append(
                        Finding(
                            code="APPROVAL_NOT_CONTENT_BOUND",
                            severity="warning",
                            message=(
                                "批准记录未提供内容指纹；只能按精确版本和段落范围裁决，"
                                "建议补做内容绑定。"
                            ),
                            node=node,
                            chain=path,
                            detail={"approval_id": approval.approval_id},
                        )
                    )
                    continue

                expected_hash = approval.reviewed_hashes.get(node.paragraph)
                if expected_hash is None:
                    findings.append(
                        Finding(
                            code="APPROVAL_BINDING_INCOMPLETE",
                            message="批准范围包含该段，但批准记录缺少该段的内容指纹。",
                            node=node,
                            chain=path,
                            detail={"approval_id": approval.approval_id},
                        )
                    )
                elif expected_hash != actual_hash:
                    findings.append(
                        Finding(
                            code="APPROVAL_CONTENT_MISMATCH",
                            message=(
                                "当前段落内容与已复核批准时的内容指纹不一致；"
                                "批准不跟随改写后的内容，必须重新确认。"
                            ),
                            node=node,
                            chain=path,
                            detail={
                                "approval_id": approval.approval_id,
                                "expected_fingerprint": expected_hash,
                                "actual_fingerprint": actual_hash,
                            },
                        )
                    )
                else:
                    candidate = True

        return candidate and not withheld, covered, withheld, findings

    def _carry_entitlement(
        self,
        node: NodeRef,
        path: tuple[NodeRef, ...],
        *,
        content_rule: str,
    ) -> tuple[list[Finding], list[NodeEvaluation], bool, bool]:
        findings: list[Finding] = []
        children: list[NodeEvaluation] = []
        inherited_ok = False
        record = self.registry.require(node.record)
        target_text = record.paragraphs[node.paragraph]
        target_hash = fingerprint_text(target_text)

        declarations = [
            declaration
            for declaration in self.registry.carry_forwards_to(node.record)
            if node.paragraph in declaration.paragraph_map
        ]
        attempted = bool(declarations)

        # Optional legacy behavior: if explicit declarations are not required,
        # compare the same-number baseline paragraph and inherit only when the
        # exact content fingerprint matches and the baseline was cleared.
        if (
            not declarations
            and content_rule == "submission_root"
            and self.baseline_submission is not None
            and not self.policy.require_carry_declaration_for_revision
        ):
            declarations = [
                CarryForward(
                    target=node.record,
                    source=self.baseline_submission,
                    paragraph_map={node.paragraph: node.paragraph},
                    declaration_id="implicit-baseline",
                )
            ]
            attempted = True

        for declaration in declarations:
            source_paragraph = declaration.paragraph_map[node.paragraph]
            source_node = NodeRef.from_record(declaration.source, source_paragraph)
            source_record = self.registry.get(declaration.source)
            declaration_label = declaration.declaration_id or "carry-forward"
            implicit = declaration_label == "implicit-baseline"

            content_mismatch = False
            if source_record is not None and source_paragraph in source_record.paragraphs:
                source_hash = fingerprint_text(source_record.paragraphs[source_paragraph])
                if source_hash != target_hash:
                    content_mismatch = True
                    if implicit:
                        message = (
                            "按策略自动比对基线时发现该段已经改写；"
                            "改写部分不能沿用原批准，必须重新确认。"
                        )
                    else:
                        message = (
                            "虽然声明该段原样承接，但新旧段落内容指纹不一致；"
                            "改写部分不能沿用原批准，必须重新确认。"
                        )
                    findings.append(
                        Finding(
                            code="REWRITTEN_NEEDS_CONFIRMATION",
                            message=message,
                            node=node,
                            citation_id=declaration_label,
                            chain=path + (source_node,),
                            detail={
                                "source": source_node.to_dict(),
                                "target_fingerprint": target_hash,
                                "source_fingerprint": source_hash,
                                "implicit": implicit,
                            },
                        )
                    )

            child = self._visit(
                source_node,
                path,
                incoming_kind="carry",
                incoming_id=declaration_label,
            )
            children.append(child)
            if not content_mismatch and child.approved:
                inherited_ok = True
            elif source_record is None:
                # The child already carries the missing-record remediation, but
                # add a carry-specific code for easier revision reporting.
                findings.append(
                    Finding(
                        code="CARRY_FORWARD_SOURCE_MISSING",
                        message="原样承接声明指向的先前版本缺失，无法验证承接和原批准。",
                        node=node,
                        citation_id=declaration_label,
                        repair_from=source_node,
                        chain=path + (source_node,),
                    )
                )

        return findings, children, inherited_ok, attempted

    def _submission_without_entitlement(
        self, node: NodeRef, record: VersionedRecord, path: tuple[NodeRef, ...]
    ) -> Finding | None:
        # If an explicit carry link exists, _carry_entitlement and its child
        # trace provide the precise reason. Do not second-guess it by comparing
        # same-number baseline paragraphs.
        has_carry_declaration = any(
            node.paragraph in declaration.paragraph_map
            for declaration in self.registry.carry_forwards_to(node.record)
        )
        if has_carry_declaration:
            return None

        citations = record.citation_for_paragraph(node.paragraph)
        if self.baseline_submission is not None:
            baseline = self.registry.get(self.baseline_submission)
            baseline_node = NodeRef.from_record(
                self.baseline_submission, node.paragraph
            )
            if baseline is None:
                return Finding(
                    code="BASELINE_RECORD_MISSING",
                    message=(
                        "本次提交被声明为修订版，但作为复核基线的原提交版本缺失；"
                        "应先补入基线记录。"
                    ),
                    node=node,
                    repair_from=self.baseline_submission,
                    chain=path + (baseline_node,),
                )

            if node.paragraph not in baseline.paragraphs:
                return Finding(
                    code="REWRITTEN_NEEDS_CONFIRMATION",
                    message="该段在基线版本中不存在，属于新增/重写内容，必须重新确认。",
                    node=node,
                    chain=path,
                    detail={"baseline": self.baseline_submission.to_dict()},
                )

            current_hash = fingerprint_text(record.paragraphs[node.paragraph])
            baseline_hash = fingerprint_text(baseline.paragraphs[node.paragraph])
            if current_hash == baseline_hash:
                return Finding(
                    code="CARRY_FORWARD_DECLARATION_REQUIRED",
                    message=(
                        "该段内容与基线同号段完全一致，但未明确声明原样承接；"
                        "需补声明后才能沿用上一轮批准。"
                    ),
                    node=node,
                    chain=path,
                    detail={
                        "baseline_node": baseline_node.to_dict(),
                        "fingerprint": current_hash,
                    },
                )
            return Finding(
                code="REWRITTEN_NEEDS_CONFIRMATION",
                message="该段相对基线已经改写，不能沿用原批准，必须重新确认。",
                node=node,
                chain=path,
                detail={
                    "baseline_node": baseline_node.to_dict(),
                    "current_fingerprint": current_hash,
                    "baseline_fingerprint": baseline_hash,
                },
            )

        if not citations:
            if self.policy.uncited_paragraph_status == "disclose":
                return None
            if self.policy.uncited_paragraph_status == "withhold":
                return Finding(
                    code="ORIGINAL_TEXT_NOT_CLEARED",
                    message="无引用的调查员原创段落按策略未获开示确认。",
                    node=node,
                    chain=path,
                )
            return Finding(
                code="SUBMISSION_REVIEW_REQUIRED",
                message="该段属于调查员文字，需要本次开示复核明确确认。",
                node=node,
                chain=path,
            )

        # Without a revision baseline, cited paragraphs are allowed at the
        # investigator-wording layer; all quoted dependencies are adjudicated by
        # the citation children.
        return None

    def _verbatim_mismatch_finding(
        self,
        node: NodeRef,
        actual_text: str,
        citation: Any,
        target_numbers: tuple[int, ...],
        path: tuple[NodeRef, ...],
    ) -> Finding | None:
        source_record = self.registry.get(citation.source)
        if source_record is None:
            return None
        source_texts: list[str] = []
        for target_number in target_numbers:
            source_text = source_record.text(target_number)
            if source_text is None:
                return None
            source_texts.append(source_text)
        expected_text = "\n".join(source_texts)
        if actual_text == expected_text:
            return None
        return Finding(
            code="VERBATIM_EXCERPT_MISMATCH",
            message=(
                "引用声明为原样摘录，但当前摘录文本与实际使用材料版本中的对应段落不一致；"
                "改写或误引部分必须重新确认。"
            ),
            node=node,
            citation_id=citation.citation_id,
            chain=path,
            detail={
                "source": citation.source.to_dict(),
                "source_paragraphs": list(target_numbers),
                "actual_fingerprint": fingerprint_text(actual_text),
                "expected_fingerprint": fingerprint_text(expected_text),
            },
        )

    # ------------------------------------------------------------------
    # Output assembly
    # ------------------------------------------------------------------

    def _build_segments(self, results: list[ParagraphResult]) -> list[Segment]:
        segments: list[Segment] = []
        index = 0
        while index < len(results):
            first = results[index]
            status = first.status
            code_signature = tuple(first.reason_codes)
            end = index + 1
            while end < len(results):
                candidate = results[end]
                if (
                    candidate.status != status
                    or tuple(candidate.reason_codes) != code_signature
                ):
                    break
                end += 1

            group = results[index:end]
            paragraph_numbers = [item.number for item in group]
            unique_findings = self._unique_findings(
                finding for item in group for finding in item.all_findings()
            )
            paragraph_range = Range(paragraph_numbers[0], paragraph_numbers[-1])
            segments.append(
                Segment(
                    paragraph_range=paragraph_range,
                    status=status,  # type: ignore[arg-type]
                    paragraphs=paragraph_numbers,
                    text="\n\n".join(item.text for item in group),
                    reason_codes=list(code_signature),
                    findings=unique_findings,
                )
            )
            index = end
        return segments

    @staticmethod
    def _unique_findings(findings) -> list[Finding]:
        unique: dict[tuple[Any, ...], Finding] = {}
        for finding in findings:
            key = (
                finding.code,
                finding.message,
                finding.severity,
                finding.node.record_id if finding.node else None,
                finding.node.version if finding.node else None,
                finding.node.paragraph if finding.node else None,
                finding.citation_id,
            )
            unique.setdefault(key, finding)
        return list(unique.values())

    def _build_remediations(
        self, results: list[ParagraphResult]
    ) -> list[Remediation]:
        ordered: dict[tuple[str, str], Remediation] = {}
        for result in results:
            for finding in self._unique_findings(result.all_findings()):
                if finding.repair_from is None:
                    continue
                repair_key = self._ref_key(finding.repair_from)
                key = (finding.code, repair_key)
                if key not in ordered:
                    ordered[key] = Remediation(
                        code=finding.code,
                        repair_from=finding.repair_from,
                        action=_ACTION_BY_CODE.get(
                            finding.code,
                            "从 repair_from 所指记录开始补正后重新核查。",
                        ),
                        chain=list(finding.chain),
                        citation_id=finding.citation_id,
                        affected_submission_paragraphs=[result.number],
                        detail=dict(finding.detail),
                    )
                else:
                    remediation = ordered[key]
                    if result.number not in remediation.affected_submission_paragraphs:
                        remediation.affected_submission_paragraphs.append(result.number)
                    if finding.citation_id:
                        known = remediation.detail.setdefault("citation_ids", [])
                        if finding.citation_id not in known:
                            known.append(finding.citation_id)
                    if (
                        remediation.citation_id
                        and finding.citation_id
                        and remediation.citation_id != finding.citation_id
                    ):
                        remediation.citation_id = None
        return list(ordered.values())

    @staticmethod
    def _ref_key(ref: NodeRef | RecordRef) -> str:
        if isinstance(ref, NodeRef):
            return f"node:{ref.record_id}@{ref.version}#p{ref.paragraph}"
        return f"record:{ref.record_id}@{ref.version}"

    def _input_validation_errors(self) -> list[str]:
        errors = list(self.registry.validation_errors())
        for approval in self.registry.approvals:
            if self.registry.get(approval.ref) is None:
                errors.append(f"批准记录指向不存在的材料版本: {approval.ref}")
        for carry in self.registry.carry_forwards:
            if self.registry.get(carry.target) is None:
                errors.append(f"承接声明目标版本不存在: {carry.target}")
            if self.registry.get(carry.source) is None:
                errors.append(f"承接声明来源版本不存在: {carry.source}")
        return errors


def review_case(case: ReviewCase) -> ReviewReport:
    """Functional public API: review one fully constructed case."""

    return ReviewEngine.from_case(case).review_current(case.submission)
