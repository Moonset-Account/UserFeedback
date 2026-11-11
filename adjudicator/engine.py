"""裁决引擎：对每个 (发行版本, 组件, 漏洞) 组合给出四种处置之一。

处置语义：
- affected（受影响）：至少一条有效证据给出确定性“受影响”，且无冲突；
- not_affected（不受影响）：证据一致认为该版本不在受影响范围或被显式豁免；
- fixed（已修复）：存在受影响公告且该版本确定位于所有受影响区间之外，
  即已越过修复端点；与 not_affected 的区别是 fixed 必然伴随同漏洞的
  受影响公告，而 not_affected 可能来自公告前的旧版本豁免或适用性记录；
- needs_verification（待核实）：版本无法比较、证据互相矛盾、
  公告声明畸形、或适用性记录无法对应当前快照等所有无法定论的情形。

证据编号约定：
- ADV-* / STMT-*：公告及其声明；
- REC-*：适用性记录（过期记录只出现在 reasons，不进入 evidence）；
- BOM 的组件 ref 作为定位信息随 finding 输出，不作为判定证据。

裁决只依赖清单、公告、适用性记录三类元数据，不访问网络、不调用评分服务。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .interval import Interval, IntervalState
from .models import (
    Advisory,
    ApplicabilityRecord,
    Bom,
    Component,
    DISPOSITION_AFFECTED,
    DISPOSITION_FIXED,
    DISPOSITION_LABELS_ZH,
    DISPOSITION_NEEDS_VERIFICATION,
    DISPOSITION_NOT_AFFECTED,
    DISPOSITIONS,
    Finding,
    Statement,
    VERDICT_AFFECTED,
    VERDICT_FIXED,
    VERDICT_NOT_AFFECTED,
    VERDICT_UNKNOWN,
)
from .versioning import Version, parse_version

Coordinate = Tuple[str, str, str]


@dataclass(frozen=True)
class _Verdict:
    """单条证据来源（公告或记录）对一个组件版本的判定。"""

    verdict: str            # affected / not_affected / fixed / unknown
    evidence: Tuple[str, ...]
    reasons: Tuple[str, ...]


@dataclass(frozen=True)
class ChangeEntry:
    """公告更新前后的单条处置变化。"""

    key: Tuple[str, str, Optional[str]]
    component: str
    vuln_id: Optional[str]
    before: Optional[str]
    after: Optional[str]
    reasons: Tuple[str, ...]


class Adjudicator:
    def __init__(
        self,
        boms: Sequence[Bom],
        advisories: Sequence[Advisory],
        records: Sequence[ApplicabilityRecord],
    ) -> None:
        self.boms = list(boms)
        self.advisories = list(advisories)
        self.records = list(records)
        self._adv_index: Dict[Coordinate, List[Advisory]] = defaultdict(list)
        for adv in self.advisories:
            for stmt in adv.statements:
                self._adv_index[stmt.coordinate].append(adv)
        self._rec_index: Dict[
            Tuple[str, Coordinate, str, str], List[ApplicabilityRecord]
        ] = defaultdict(list)
        for rec in self.records:
            idx_key = (rec.release_id, rec.coordinate, rec.version, rec.vuln_id)
            self._rec_index[idx_key].append(rec)

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------
    def adjudicate_all(self) -> List[Finding]:
        findings: List[Finding] = []
        for bom in self.boms:
            for component in bom.components:
                findings.extend(self._adjudicate_component(bom, component))
        # 稳定排序，保证可重跑输出确定
        findings.sort(key=lambda f: (f.release_id, f.bom_ref,
                                     f.vuln_id or "", f.disposition))
        return findings

    def diff(self, baseline_findings: Sequence[Finding],
             current_findings: Sequence[Finding],
             baseline_advisories: Optional[Sequence[Advisory]] = None,
             current_advisories: Optional[Sequence[Advisory]] = None) -> List[ChangeEntry]:
        """对比公告更新前后的处置变化。

        baseline / current 两次运行必须基于同一份 BOM 快照；
        变化原因结合公告生命周期（撤回、重新打开、修订）给出。
        """
        base_map = {f.key: f for f in baseline_findings}
        cur_map = {f.key: f for f in current_findings}
        base_adv = self._advisory_lifecycle(baseline_advisories)
        cur_adv = self._advisory_lifecycle(current_advisories)
        changes: List[ChangeEntry] = []
        all_keys = sorted(set(base_map) | set(cur_map))
        for key in all_keys:
            old = base_map.get(key)
            new = cur_map.get(key)
            old_disp = old.disposition if old else None
            new_disp = new.disposition if new else None
            if old_disp == new_disp:
                continue
            reasons = self._change_reasons(key, old, new, base_adv, cur_adv)
            target = new or old
            assert target is not None
            changes.append(ChangeEntry(
                key=key,
                component=self._component_label(target),
                vuln_id=key[2],
                before=old_disp,
                after=new_disp,
                reasons=tuple(reasons),
            ))
        return changes

    # ------------------------------------------------------------------
    # 单组件裁决
    # ------------------------------------------------------------------
    def _adjudicate_component(self, bom: Bom, component: Component) -> List[Finding]:
        candidate_advs = self._adv_index.get(component.coordinate, [])
        # 该组件涉及的全部漏洞：公告给出的 + 适用性记录给出的
        vuln_ids = {a.vuln_id for a in candidate_advs}
        for rec in self._records_for(bom, component):
            vuln_ids.add(rec.vuln_id)
        if not vuln_ids:
            # 没有任何元数据提及该组件：不产生漏洞条目，
            # 组件级汇总中标记为无适用公告。
            return []
        findings = []
        for vuln_id in sorted(vuln_ids):
            findings.append(self._adjudicate_one(bom, component, vuln_id,
                                                 candidate_advs))
        return findings

    def _adjudicate_one(
        self,
        bom: Bom,
        component: Component,
        vuln_id: str,
        candidate_advs: Sequence[Advisory],
    ) -> Finding:
        evidence: List[str] = []
        reasons: List[str] = []
        verdicts: List[_Verdict] = []
        parsed = component.parsed_version

        # 1) 公告证据
        relevant = [a for a in candidate_advs if a.vuln_id == vuln_id]
        for adv in sorted(relevant, key=lambda a: (a.revised, a.advisory_id)):
            if adv.withdrawn:
                # 已撤回公告不提供判定，只保留审计痕迹
                reasons.append(f"advisory_withdrawn:{adv.advisory_id}")
                continue
            verdict = self._evaluate_advisory(adv, component, parsed)
            if verdict is not None:
                verdicts.append(verdict)
                evidence.extend(verdict.evidence)

        # 2) 适用性记录证据（必须对应当前快照）
        rec_verdict = self._evaluate_records(bom, component, vuln_id, reasons)
        if rec_verdict is not None:
            verdicts.append(rec_verdict)
            evidence.extend(rec_verdict.evidence)

        # 3) 汇总
        disposition, final_reasons = self._combine(
            verdicts, bool(relevant), parsed, reasons
        )
        all_reasons = reasons + final_reasons
        return Finding(
            release_id=bom.release_id,
            bom_ref=component.ref,
            ecosystem=component.ecosystem,
            namespace=component.namespace,
            name=component.name,
            version=component.version,
            vuln_id=vuln_id,
            disposition=disposition,
            evidence=tuple(self._dedup(evidence)),
            reasons=tuple(self._dedup(all_reasons)),
        )

    def _evaluate_advisory(
        self,
        adv: Advisory,
        component: Component,
        parsed: Optional[Version],
    ) -> Optional[_Verdict]:
        stmts = [s for s in adv.statements if s.coordinate == component.coordinate]
        if not stmts:
            return None
        # 组件版本不可解析时，仍要扫描区间端点是否也存在畸形，
        # 把两类“无法比较”的原因都保留下来。
        if parsed is None:
            endpoint_reasons = self._collect_malformed_endpoint_reasons(stmts)
            return _Verdict(
                VERDICT_UNKNOWN,
                (adv.advisory_id,),
                ((f"unparseable_component_version:{component.version}",)
                 + endpoint_reasons),
            )
        affected_hits: List[str] = []
        not_affected_hits: List[str] = []
        unknown_parts: List[str] = []
        affected_interval_seen = False

        for stmt in stmts:
            if stmt.kind == "affected":
                for interval in stmt.affected_intervals:
                    affected_interval_seen = True
                    state = interval.contains(parsed)
                    if state is IntervalState.INSIDE:
                        affected_hits.append(stmt.statement_id)
                    elif state is IntervalState.UNKNOWN:
                        unknown_parts.append(
                            f"unparseable_interval_endpoint:{stmt.statement_id}:"
                            f"{interval.describe()}"
                        )
            else:  # not_affected
                if component.version in stmt.not_affected_versions:
                    not_affected_hits.append(stmt.statement_id)
                for interval in stmt.not_affected_intervals:
                    state = interval.contains(parsed)
                    if state is IntervalState.INSIDE:
                        not_affected_hits.append(stmt.statement_id)
                    elif state is IntervalState.UNKNOWN:
                        unknown_parts.append(
                            f"unparseable_interval_endpoint:{stmt.statement_id}:"
                            f"{interval.describe()}"
                        )

        evidence = (adv.advisory_id,) + tuple(
            sid for sid in self._dedup(affected_hits + not_affected_hits)
        )
        # 同一公告内部若同时命中受影响与豁免，属于自相矛盾
        if affected_hits and not_affected_hits:
            return _Verdict(
                VERDICT_UNKNOWN, evidence,
                ("contradiction_within_advisory:" + adv.advisory_id,),
            )
        if affected_hits:
            return _Verdict(VERDICT_AFFECTED, evidence, ())
        if not_affected_hits:
            return _Verdict(VERDICT_NOT_AFFECTED, evidence, ())
        if unknown_parts:
            return _Verdict(VERDICT_UNKNOWN, (adv.advisory_id,), tuple(unknown_parts))
        # 未命中任何声明区间：
        # - 若公告声明了受影响区间，则版本确定在区间外 => 已修复；
        # - 若公告只有豁免声明而无受影响区间，无法据此定论 => unknown。
        if affected_interval_seen:
            return _Verdict(
                VERDICT_FIXED, (adv.advisory_id,),
                ("outside_affected_range:" + adv.advisory_id,),
            )
        return _Verdict(
            VERDICT_UNKNOWN, (adv.advisory_id,),
            ("no_matching_statement:" + adv.advisory_id,),
        )

    def _evaluate_records(
        self,
        bom: Bom,
        component: Component,
        vuln_id: str,
        reasons: List[str],
    ) -> Optional[_Verdict]:
        matched = self._rec_index.get(
            (bom.release_id, component.coordinate, component.version, vuln_id), []
        )
        if not matched:
            return None
        valid_evidence: List[str] = []
        verdicts: List[str] = []
        for rec in sorted(matched, key=lambda r: r.record_id):
            if rec.bom_snapshot_id != bom.snapshot_id:
                # 过期记录：保留原因，但绝不进入证据列表
                reasons.append(
                    f"record_stale:{rec.record_id}:expected_snapshot={bom.snapshot_id}"
                    f":actual_snapshot={rec.bom_snapshot_id}"
                )
                continue
            valid_evidence.append(rec.record_id)
            verdicts.append(
                VERDICT_AFFECTED if rec.verdict == "affected"
                else VERDICT_NOT_AFFECTED
            )
        if not valid_evidence:
            return None
        # 同一组件的有效记录若自相矛盾，同样降级为待核实
        if len(set(verdicts)) > 1:
            return _Verdict(
                VERDICT_UNKNOWN, tuple(valid_evidence),
                ("contradiction_within_records:" + ",".join(valid_evidence),),
            )
        return _Verdict(verdicts[0], tuple(valid_evidence), ())

    def _combine(
        self,
        verdicts: Sequence[_Verdict],
        had_relevant_advisory: bool,
        parsed: Optional[Version],
        existing_reasons: List[str],
    ) -> Tuple[str, List[str]]:
        definitive = [v for v in verdicts if v.verdict in
                      (VERDICT_AFFECTED, VERDICT_NOT_AFFECTED, VERDICT_FIXED)]
        unknowns = [v for v in verdicts if v.verdict == VERDICT_UNKNOWN]
        extra_reasons: List[str] = []
        for v in unknowns:
            extra_reasons.extend(v.reasons)

        if not definitive:
            if parsed is None:
                extra_reasons.insert(0, "version_not_comparable")
            if not had_relevant_advisory and not verdicts:
                extra_reasons.append("no_applicable_advisory")
            elif not verdicts:
                extra_reasons.append("all_advisories_withdrawn")
            return DISPOSITION_NEEDS_VERIFICATION, extra_reasons

        distinct = {v.verdict for v in definitive}
        if len(distinct) > 1:
            # 不同证据来源给出互斥的确定性结论 => 公告/记录互相矛盾
            evidence_ids = sorted({e for v in definitive for e in v.evidence})
            extra_reasons.append(
                "conflicting_evidence:" + ",".join(evidence_ids)
            )
            return DISPOSITION_NEEDS_VERIFICATION, extra_reasons

        only = next(iter(distinct))
        mapping = {
            VERDICT_AFFECTED: DISPOSITION_AFFECTED,
            VERDICT_NOT_AFFECTED: DISPOSITION_NOT_AFFECTED,
            VERDICT_FIXED: DISPOSITION_FIXED,
        }
        if unknowns:
            # 确定性结论存在但另有无法比较/畸形证据：保留确定性处置，
            # 同时把不确定原因透明列出，便于人工复核。
            extra_reasons.append("inconclusive_evidence_present")
        return mapping[only], extra_reasons

    # ------------------------------------------------------------------
    # 差异报告辅助
    # ------------------------------------------------------------------
    def _advisory_lifecycle(
        self, advisories: Optional[Sequence[Advisory]]
    ) -> Dict[str, Advisory]:
        if advisories is None:
            return {}
        return {a.advisory_id: a for a in advisories}

    def _change_reasons(
        self,
        key: Tuple[str, str, Optional[str]],
        old: Optional[Finding],
        new: Optional[Finding],
        base_adv: Dict[str, Advisory],
        cur_adv: Dict[str, Advisory],
    ) -> List[str]:
        reasons: List[str] = []
        # 只保留与本条目漏洞相关的公告生命周期事件，避免无关公告污染原因
        related_ids = self._related_advisory_ids(key[2], base_adv, cur_adv)
        for aid in sorted(related_ids):
            before = base_adv.get(aid)
            after = cur_adv.get(aid)
            if before is None and after is not None:
                reasons.append(f"advisory_added:{aid}")
            elif before is not None and after is None:
                reasons.append(f"advisory_removed:{aid}")
            elif before is not None and after is not None:
                if before.withdrawn != after.withdrawn:
                    state = "withdrawn" if after.withdrawn else "reopened"
                    reasons.append(f"advisory_{state}:{aid}")
                elif before.revised != after.revised:
                    reasons.append(
                        f"advisory_revised:{aid}:{before.revised}->{after.revised}"
                    )
        if old is None:
            reasons.append("finding_added")
        if new is None:
            reasons.append("finding_cleared")
        # 证据集合变化只统计同漏洞的适用性记录（公告证据已由上面的生命周期覆盖）
        old_rec = {e for e in (old.evidence if old else ())
                   if e.startswith("REC-")}
        new_rec = {e for e in (new.evidence if new else ())
                   if e.startswith("REC-")}
        if old_rec != new_rec:
            if new_rec - old_rec:
                reasons.append("evidence_added:" + ",".join(sorted(new_rec - old_rec)))
            if old_rec - new_rec:
                reasons.append("evidence_removed:" + ",".join(sorted(old_rec - new_rec)))
        return reasons

    @staticmethod
    def _related_advisory_ids(
        vuln_id: Optional[str],
        base_adv: Dict[str, Advisory],
        cur_adv: Dict[str, Advisory],
    ) -> set:
        if vuln_id is None:
            return set()
        return {
            aid for aid, adv in (set(base_adv.items()) | set(cur_adv.items()))
            if adv.vuln_id == vuln_id
        }

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    @staticmethod
    def _collect_malformed_endpoint_reasons(
        stmts: Sequence[Statement],
    ) -> Tuple[str, ...]:
        reasons: List[str] = []
        for stmt in stmts:
            for interval in (stmt.affected_intervals
                             + stmt.not_affected_intervals):
                if not interval.well_formed:
                    reasons.append(
                        f"unparseable_interval_endpoint:{stmt.statement_id}:"
                        f"{interval.describe()}"
                    )
        return tuple(reasons)

    def _records_for(
        self, bom: Bom, component: Component
    ) -> List[ApplicabilityRecord]:
        out: List[ApplicabilityRecord] = []
        for rec in self.records:
            if (rec.release_id == bom.release_id
                    and rec.coordinate == component.coordinate
                    and rec.version == component.version):
                out.append(rec)
        return out

    @staticmethod
    def _dedup(items: Sequence[str]) -> List[str]:
        seen = set()
        result = []
        for item in items:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    @staticmethod
    def _component_label(finding: Finding) -> str:
        ns = f"{finding.namespace}/" if finding.namespace else ""
        return f"{finding.ecosystem}:{ns}{finding.name}@{finding.version}"


def summarize(findings: Sequence[Finding]) -> Dict[str, int]:
    """按四种处置统计条目数（未知 key 不计数，保证总和可校验）。"""
    counts = {d: 0 for d in DISPOSITIONS}
    for finding in findings:
        counts[finding.disposition] = counts.get(finding.disposition, 0) + 1
    return counts
