"""迁移核验主流程。

输入两版声明式规则、旧结论、原始观测（含证据等级与更正链），输出三类结果：

* ``migrated``：可直接迁移的结论（含适用时段与所用证据）；
* ``re_review``：需要重新复核的证据项（观测被更正、证据等级不足、
  缺少独立来源）；
* ``retained``：保留原归因的结论及理由（含保留的适用时段）。

规则形态与迁移语义
------------------

* ``replaces``：一对一替换。新规则在当前有效观测上成立则迁移，
  否则在对应时段保留旧归因并登记复核项。
* ``split``：一条旧规则拆成多个子结论。所有子结论都满足
  （独立来源约束跨子结论全局生效）才整体迁移；任一不满足则该时段
  保留旧归因，登记缺口复核项。
* ``merge``：互相排斥的旧解释合并。多个分支同时成立时，按合并规则
  产出 ``uncertain`` 结论；仅一个分支成立时迁移该分支；都不成立时
  保留旧归因并登记复核项。

观测被更正时，以更正生效时间 ``corrected_at`` 切分旧结论的适用时段：
更正前的时段保留旧归因（当时依据有效），更正后的时段用更正后的
具体来源重新求值。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .engine import Deficit, Evaluation, evaluate_all, evaluate_conclusion
from .model import (
    ConclusionSpec,
    LegacyConclusion,
    NewRule,
    Observation,
    OldRule,
    RuleValidationError,
    format_timestamp,
)

# ---- 复核项原因 -----------------------------------------------------------
REVIEW_OBSERVATION_CORRECTED = "observation_corrected"
REVIEW_BELOW_THRESHOLD = "evidence_below_new_threshold"
REVIEW_LACKS_INDEPENDENT = "lacks_independent_source"

# ---- 保留旧归因的理由 ------------------------------------------------------
RETAIN_PRE_CORRECTION = "retained_pre_correction_period"
RETAIN_THRESHOLD = "retained_evidence_below_new_threshold"
RETAIN_INDEPENDENCE = "retained_lacks_independent_source"
RETAIN_SPLIT_PENDING = "retained_split_pending_independent_sources"
RETAIN_MERGE_PENDING = "retained_merge_branch_unsupported"
RETAIN_NO_MAPPING = "retained_no_successor_rule"

VERDICT_ESTABLISHED = "established"
VERDICT_UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class MigratedConclusion:
    conclusion_id: str
    label: str
    verdict: str
    new_rule_id: str
    source_rule_ids: tuple[str, ...]
    evidence_obs_ids: tuple[str, ...]
    valid_from: datetime
    valid_to: Optional[datetime]
    basis: str

    def to_dict(self) -> dict:
        return {
            "conclusion_id": self.conclusion_id,
            "label": self.label,
            "verdict": self.verdict,
            "new_rule_id": self.new_rule_id,
            "source_rule_ids": list(self.source_rule_ids),
            "evidence_obs_ids": list(self.evidence_obs_ids),
            "valid_from": format_timestamp(self.valid_from),
            "valid_to": format_timestamp(self.valid_to),
            "basis": self.basis,
        }


@dataclass(frozen=True)
class ReReviewItem:
    reason: str
    fact: str
    related_rule_id: str
    detail: str
    obs_id: Optional[str] = None
    source_id: Optional[str] = None
    evidence_level: Optional[str] = None
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "reason": self.reason,
            "obs_id": self.obs_id,
            "fact": self.fact,
            "source_id": self.source_id,
            "evidence_level": self.evidence_level,
            "related_rule_id": self.related_rule_id,
            "period_start": format_timestamp(self.period_start),
            "period_end": format_timestamp(self.period_end),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RetainedConclusion:
    legacy_rule_id: str
    legacy_label: str
    valid_from: datetime
    valid_to: Optional[datetime]
    reason: str
    detail: str

    def to_dict(self) -> dict:
        return {
            "legacy_rule_id": self.legacy_rule_id,
            "legacy_label": self.legacy_label,
            "valid_from": format_timestamp(self.valid_from),
            "valid_to": format_timestamp(self.valid_to),
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass
class MigrationReport:
    migrated: list[MigratedConclusion]
    re_review: list[ReReviewItem]
    retained: list[RetainedConclusion]

    def to_dict(self) -> dict:
        return {
            "migrated": [item.to_dict() for item in self.migrated],
            "re_review": [item.to_dict() for item in self.re_review],
            "retained": [item.to_dict() for item in self.retained],
        }


# ---------------------------------------------------------------------------
# 输入解析与引用完整性校验
# ---------------------------------------------------------------------------


def _parse_inputs(raw_old_rules, raw_new_rules, raw_legacy, raw_observations):
    if not isinstance(raw_old_rules, list) or not raw_old_rules:
        raise RuleValidationError("old_rules 必须是非空列表")
    if not isinstance(raw_new_rules, list) or not raw_new_rules:
        raise RuleValidationError("new_rules 必须是非空列表")
    if not isinstance(raw_legacy, list) or not raw_legacy:
        raise RuleValidationError("legacy_conclusions 必须是非空列表")
    if not isinstance(raw_observations, list) or not raw_observations:
        raise RuleValidationError("observations 必须是非空列表")

    old_rules = [OldRule.from_dict(item) for item in raw_old_rules]
    new_rules = [NewRule.from_dict(item) for item in raw_new_rules]
    legacy = [LegacyConclusion.from_dict(item) for item in raw_legacy]
    observations = [Observation.from_dict(item) for item in raw_observations]

    old_by_id: dict[str, OldRule] = {}
    for rule in old_rules:
        if rule.rule_id in old_by_id:
            raise RuleValidationError(f"旧规则 id 重复: {rule.rule_id}")
        old_by_id[rule.rule_id] = rule

    new_ids: set[str] = set()
    conclusion_ids: set[str] = set()
    for rule in new_rules:
        if rule.rule_id in new_ids:
            raise RuleValidationError(f"新规则 id 重复: {rule.rule_id}")
        new_ids.add(rule.rule_id)
        specs = list(rule.conclusions) + list(rule.branches)
        if rule.conclusion is not None:
            specs.append(rule.conclusion)
        for spec in specs:
            if spec.conclusion_id in conclusion_ids:
                raise RuleValidationError(
                    f"新结论 id 重复: {spec.conclusion_id}"
                )
            conclusion_ids.add(spec.conclusion_id)

    obs_by_id: dict[str, Observation] = {}
    for obs in observations:
        if obs.obs_id in obs_by_id:
            raise RuleValidationError(f"观测 id 重复: {obs.obs_id}")
        obs_by_id[obs.obs_id] = obs

    # 更正链引用与环检测
    replaced_by: dict[str, Observation] = {}
    for obs in observations:
        if obs.supersedes is not None:
            if obs.supersedes not in obs_by_id:
                raise RuleValidationError(
                    f"观测 {obs.obs_id} 的 supersedes 指向不存在的观测 {obs.supersedes}"
                )
            if obs.supersedes in replaced_by:
                raise RuleValidationError(
                    f"观测 {obs.supersedes} 被多条观测更正，更正链必须线性"
                )
            replaced_by[obs.supersedes] = obs
    for start_id in obs_by_id:
        seen = {start_id}
        cursor = start_id
        while cursor in replaced_by:
            cursor = replaced_by[cursor].obs_id
            if cursor in seen:
                raise RuleValidationError(f"观测更正链存在环，涉及 {cursor}")
            seen.add(cursor)

    # 新规则只能引用旧规则
    referenced: set[str] = set()
    for rule in new_rules:
        refs = []
        if rule.replaces:
            refs.append(rule.replaces)
        if rule.splits_from:
            refs.append(rule.splits_from)
        refs.extend(rule.merges)
        for ref in refs:
            if ref not in old_by_id:
                raise RuleValidationError(
                    f"新规则 {rule.rule_id} 引用了不存在的旧规则 {ref}"
                )
            if ref in referenced:
                raise RuleValidationError(
                    f"旧规则 {ref} 被多条新规则引用，迁移关系必须唯一"
                )
            referenced.add(ref)

    # 旧结论引用
    legacy_by_rule: dict[str, list[LegacyConclusion]] = {}
    for lc in legacy:
        if lc.legacy_rule_id not in old_by_id:
            raise RuleValidationError(
                f"旧结论引用了不存在的旧规则 {lc.legacy_rule_id}"
            )
        for obs_id in lc.supporting_obs_ids:
            if obs_id not in obs_by_id:
                raise RuleValidationError(
                    f"旧结论 {lc.legacy_rule_id} 引用了不存在的观测 {obs_id}"
                )
        legacy_by_rule.setdefault(lc.legacy_rule_id, []).append(lc)

    return old_by_id, new_rules, legacy_by_rule, obs_by_id, replaced_by


# ---------------------------------------------------------------------------
# 更正链与时段切分
# ---------------------------------------------------------------------------


def _correction_chain(
    start_obs_id: str,
    obs_by_id: dict[str, Observation],
    replaced_by: dict[str, Observation],
) -> list[Observation]:
    """从旧结论引用的观测出发，沿更正链向后返回所有观测（含起点）。"""
    chain = [obs_by_id[start_obs_id]]
    cursor = start_obs_id
    while cursor in replaced_by:
        nxt = replaced_by[cursor]
        chain.append(nxt)
        cursor = nxt.obs_id
    return chain


def _correction_events(
    lc: LegacyConclusion,
    obs_by_id: dict[str, Observation],
    replaced_by: dict[str, Observation],
) -> tuple[list[datetime], dict[str, list[Observation]]]:
    """返回旧结论适用时段内的更正时间点，以及每个起始观测的更正链。"""
    events: set[datetime] = set()
    chains: dict[str, list[Observation]] = {}
    upper = lc.valid_to
    for obs_id in lc.supporting_obs_ids:
        chain = _correction_chain(obs_id, obs_by_id, replaced_by)
        chains[obs_id] = chain
        for obs in chain[1:]:
            assert obs.corrected_at is not None
            if obs.corrected_at <= lc.valid_from:
                continue  # 结论生效前已更正，整个时段都按更正后求值
            if upper is not None and obs.corrected_at >= upper:
                continue  # 结论失效后才更正，不影响其适用时段
            events.add(obs.corrected_at)
    return sorted(events), chains


def _build_segments(
    lc: LegacyConclusion, events: list[datetime]
) -> list[tuple[datetime, Optional[datetime], bool]]:
    """切分时段，返回 ``(start, end, recompute)``。

    ``recompute=False`` 的段位于更正生效之前，保留旧归因；
    ``recompute=True`` 的段用当前有效观测重新求值。
    """
    if not events:
        return [(lc.valid_from, lc.valid_to, True)]
    segments: list[tuple[datetime, Optional[datetime], bool]] = []
    cursor = lc.valid_from
    for event_at in events:
        segments.append((cursor, event_at, False))
        cursor = event_at
    if lc.valid_to is None or cursor < lc.valid_to:
        segments.append((cursor, lc.valid_to, True))
    return segments


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def verify_migration(
    old_rules: list[dict],
    new_rules: list[dict],
    legacy_conclusions: list[dict],
    observations: list[dict],
) -> MigrationReport:
    """执行迁移核验，返回 :class:`MigrationReport`。

    :raises RuleValidationError: 输入结构、引用完整性或更正链非法时。
    """
    old_by_id, new_rule_list, legacy_by_rule, obs_by_id, replaced_by = _parse_inputs(
        old_rules, new_rules, legacy_conclusions, observations
    )

    active_observations = [
        obs for obs in obs_by_id.values() if obs.obs_id not in replaced_by
    ]

    replacing: dict[str, NewRule] = {}
    merge_groups: list[NewRule] = []
    for rule in new_rule_list:
        if rule.kind in ("replaces", "split"):
            target = rule.replaces or rule.splits_from
            assert target is not None
            replacing[target] = rule
        else:
            merge_groups.append(rule)
    merge_member_to_group: dict[str, NewRule] = {}
    for group in merge_groups:
        for member in group.merges:
            merge_member_to_group[member] = group

    migrated: list[MigratedConclusion] = []
    retained: list[RetainedConclusion] = []
    review: list[ReReviewItem] = []
    review_keys: set[tuple] = set()
    migrated_keys: set[tuple] = set()

    def add_review(item: ReReviewItem) -> None:
        if item.reason == REVIEW_OBSERVATION_CORRECTED:
            # 更正类复核项描述的是观测本身的状态，跨旧规则/跨时段按观测去重
            key = (item.reason, item.obs_id)
        else:
            # 证据缺口与具体规则、时段绑定，需分别登记
            key = (
                item.reason,
                item.obs_id,
                item.fact,
                item.source_id,
                item.related_rule_id,
                item.period_start,
                item.period_end,
            )
        if key not in review_keys:
            review_keys.add(key)
            review.append(item)

    def add_migrated(item: MigratedConclusion) -> None:
        key = (
            item.conclusion_id,
            item.valid_from,
            item.valid_to,
            item.verdict,
        )
        if key not in migrated_keys:
            migrated_keys.add(key)
            migrated.append(item)

    def retain(
        lc: LegacyConclusion,
        start: datetime,
        end: Optional[datetime],
        reason: str,
        detail: str,
    ) -> None:
        retained.append(
            RetainedConclusion(
                legacy_rule_id=lc.legacy_rule_id,
                legacy_label=old_by_id[lc.legacy_rule_id].label,
                valid_from=start,
                valid_to=end,
                reason=reason,
                detail=detail,
            )
        )

    # ---- 第一步：所有被更正的支撑观测都登记复核项 ------------------------
    correction_events_cache: dict[int, tuple[list[datetime], dict]] = {}
    for rule_id, lcs in legacy_by_rule.items():
        for lc in lcs:
            events, chains = _correction_events(lc, obs_by_id, replaced_by)
            correction_events_cache[id(lc)] = (events, chains)
            for start_obs_id, chain in chains.items():
                if len(chain) <= 1:
                    continue
                originals = chain[:-1]
                latest = chain[-1]
                add_review(
                    ReReviewItem(
                        reason=REVIEW_OBSERVATION_CORRECTED,
                        obs_id=start_obs_id,
                        fact=obs_by_id[start_obs_id].fact,
                        source_id=latest.source_id,
                        evidence_level=latest.evidence_level,
                        related_rule_id=rule_id,
                        detail=(
                            f"观测 {start_obs_id} 已被更正"
                            f"（更正链: {' -> '.join(o.obs_id for o in chain)}），"
                            f"需依据最新来源 {latest.obs_id} 复核；"
                            f"原来源: {', '.join(o.source_id for o in originals)}"
                        ),
                    )
                )

    # ---- 缺口 -> 复核项 ---------------------------------------------------
    def add_deficit_reviews(
        deficits: list[Deficit], related_rule_id: str, start, end
    ) -> None:
        for deficit in deficits:
            candidate_id = deficit.candidate_obs_ids[0] if deficit.candidate_obs_ids else None
            candidate_source = (
                deficit.candidate_sources[0] if deficit.candidate_sources else None
            )
            candidate_level = (
                obs_by_id[candidate_id].evidence_level if candidate_id else None
            )
            if deficit.reason == "below_threshold":
                reason = REVIEW_BELOW_THRESHOLD
                detail = (
                    f"事实 {deficit.fact} 的现有观测等级 "
                    f"{candidate_level or '缺失'} 低于新规则要求的 "
                    f"{deficit.min_level}，需补充更高等级证据"
                )
            else:
                reason = REVIEW_LACKS_INDEPENDENT
                detail = (
                    f"{deficit.detail}；候选来源: "
                    f"{', '.join(deficit.candidate_sources) or '无'}"
                )
            add_review(
                ReReviewItem(
                    reason=reason,
                    obs_id=candidate_id,
                    fact=deficit.fact,
                    source_id=candidate_source,
                    evidence_level=candidate_level,
                    related_rule_id=related_rule_id,
                    period_start=start,
                    period_end=end,
                    detail=detail,
                )
            )

    # ---- 第二步：merge 规则（互斥解释合并） ------------------------------
    def process_merge(group: NewRule) -> None:
        branch_by_rule = dict(zip(group.merges, group.branches))
        eval_cache: dict[tuple, dict[str, Evaluation]] = {}

        def cached_eval(start, end):
            key = (start, end)
            if key not in eval_cache:
                eval_cache[key] = evaluate_all(
                    list(group.branches), active_observations
                )
            return eval_cache[key]

        for member_rule_id in group.merges:
            for lc in legacy_by_rule.get(member_rule_id, []):
                events, _ = correction_events_cache[id(lc)]
                segments = _build_segments(lc, events)
                for start, end, recompute in segments:
                    if not recompute:
                        retain(
                            lc,
                            start,
                            end,
                            RETAIN_PRE_CORRECTION,
                            f"更正生效前的时段保留旧规则 {member_rule_id} 的归因，"
                            "当时所依据的观测尚未被更正",
                        )
                        continue

                    evals = cached_eval(start, end)
                    satisfied_branches = [
                        spec
                        for spec in group.branches
                        if evals[spec.conclusion_id].satisfied
                    ]

                    if len(satisfied_branches) > 1:
                        # 互相排斥的解释同时成立 -> 不确定
                        if group.conclusion is not None:
                            merged_eval = evaluate_conclusion(
                                group.conclusion, active_observations
                            )
                            if not merged_eval.satisfied:
                                retain(
                                    lc,
                                    start,
                                    end,
                                    RETAIN_MERGE_PENDING,
                                    f"互斥分支同时成立但合并结论 "
                                    f"{group.conclusion.conclusion_id} 的证据要求未满足",
                                )
                                add_deficit_reviews(
                                    merged_eval.deficits, group.rule_id, start, end
                                )
                                continue
                            spec = group.conclusion
                            evidence = merged_eval.used_obs_ids
                            basis = "互斥分支同时成立，按合并规则判定为不确定"
                        else:
                            spec = ConclusionSpec(
                                conclusion_id=(
                                    f"uncertain::{group.rule_id}::"
                                    + "|".join(
                                        s.conclusion_id for s in satisfied_branches
                                    )
                                ),
                                label=f"合并不确定结论（{group.rule_id}）",
                                requires=(),
                            )
                            evidence = tuple(
                                sorted(
                                    {
                                        obs_id
                                        for s in satisfied_branches
                                        for obs_id in evals[
                                            s.conclusion_id
                                        ].used_obs_ids
                                    }
                                )
                            )
                            basis = (
                                "互斥分支 "
                                + ", ".join(s.conclusion_id for s in satisfied_branches)
                                + " 同时成立，合并为不确定结论"
                            )
                        add_migrated(
                            MigratedConclusion(
                                conclusion_id=spec.conclusion_id,
                                label=spec.label,
                                verdict=VERDICT_UNCERTAIN,
                                new_rule_id=group.rule_id,
                                source_rule_ids=tuple(group.merges),
                                evidence_obs_ids=evidence,
                                valid_from=start,
                                valid_to=end,
                                basis=basis,
                            )
                        )
                    elif len(satisfied_branches) == 1:
                        winner = satisfied_branches[0]
                        if branch_by_rule[member_rule_id].conclusion_id == winner.conclusion_id:
                            winning_eval = evals[winner.conclusion_id]
                            add_migrated(
                                MigratedConclusion(
                                    conclusion_id=winner.conclusion_id,
                                    label=winner.label,
                                    verdict=VERDICT_ESTABLISHED,
                                    new_rule_id=group.rule_id,
                                    source_rule_ids=(member_rule_id,),
                                    evidence_obs_ids=winning_eval.used_obs_ids,
                                    valid_from=start,
                                    valid_to=end,
                                    basis="合并规则中仅该互斥分支获得证据支持",
                                )
                            )
                        else:
                            retain(
                                lc,
                                start,
                                end,
                                RETAIN_MERGE_PENDING,
                                f"新规则下仅分支 {winner.conclusion_id} 成立，"
                                f"旧解释 {member_rule_id} 未获支持，待人工复核",
                            )
                            # 本成员对应的分支未成立，其证据缺口仍需复核
                            own_branch = branch_by_rule[member_rule_id]
                            add_deficit_reviews(
                                evals[own_branch.conclusion_id].deficits,
                                group.rule_id,
                                start,
                                end,
                            )
                    else:
                        retain(
                            lc,
                            start,
                            end,
                            RETAIN_MERGE_PENDING,
                            "合并规则的所有互斥分支均未满足证据要求，保留原归因待复核",
                        )
                        for spec in group.branches:
                            add_deficit_reviews(
                                evals[spec.conclusion_id].deficits,
                                group.rule_id,
                                start,
                                end,
                            )

    for group in merge_groups:
        process_merge(group)

    # ---- 第三步：replaces / split / 无映射 的旧结论 ----------------------
    for old_rule_id, lcs in legacy_by_rule.items():
        if old_rule_id in merge_member_to_group:
            continue  # merge 已统一处理
        successor = replacing.get(old_rule_id)

        for lc in lcs:
            events, _ = correction_events_cache[id(lc)]
            segments = _build_segments(lc, events)

            if successor is None:
                # 无新规则承接：整段保留（切分只可能来自更正，仍登记复核）
                for start, end, _recompute in segments:
                    retain(
                        lc,
                        start,
                        end,
                        RETAIN_NO_MAPPING,
                        f"旧规则 {old_rule_id} 在新版中没有承接规则，"
                        "结论无法自动迁移，保留原归因",
                    )
                continue

            for start, end, recompute in segments:
                if not recompute:
                    retain(
                        lc,
                        start,
                        end,
                        RETAIN_PRE_CORRECTION,
                        f"更正生效前的时段保留旧规则 {old_rule_id} 的归因，"
                        "当时所依据的观测尚未被更正",
                    )
                    continue

                if successor.kind == "replaces":
                    assert successor.conclusion is not None
                    spec = successor.conclusion
                    evaluation = evaluate_conclusion(spec, active_observations)
                    if evaluation.satisfied:
                        add_migrated(
                            MigratedConclusion(
                                conclusion_id=spec.conclusion_id,
                                label=spec.label,
                                verdict=VERDICT_ESTABLISHED,
                                new_rule_id=successor.rule_id,
                                source_rule_ids=(old_rule_id,),
                                evidence_obs_ids=evaluation.used_obs_ids,
                                valid_from=start,
                                valid_to=end,
                                basis="新规则证据要求全部满足，直接迁移",
                            )
                        )
                    else:
                        reasons = {d.reason for d in evaluation.deficits}
                        reason = (
                            RETAIN_INDEPENDENCE
                            if "lacks_independent_source" in reasons
                            else RETAIN_THRESHOLD
                        )
                        required_levels = sorted(
                            {d.min_level for d in evaluation.deficits}
                        )
                        retain(
                            lc,
                            start,
                            end,
                            reason,
                            f"新规则 {successor.rule_id} 证据要求未满足"
                            f"（{', '.join(sorted(reasons))}；"
                            f"门槛: {', '.join(required_levels)}），保留原归因待复核",
                        )
                        add_deficit_reviews(
                            evaluation.deficits, successor.rule_id, start, end
                        )

                else:  # split
                    specs = list(successor.conclusions)
                    evals = evaluate_all(specs, active_observations)
                    failed = [
                        spec
                        for spec in specs
                        if not evals[spec.conclusion_id].satisfied
                    ]
                    if not failed:
                        for spec in specs:
                            add_migrated(
                                MigratedConclusion(
                                    conclusion_id=spec.conclusion_id,
                                    label=spec.label,
                                    verdict=VERDICT_ESTABLISHED,
                                    new_rule_id=successor.rule_id,
                                    source_rule_ids=(old_rule_id,),
                                    evidence_obs_ids=evals[
                                        spec.conclusion_id
                                    ].used_obs_ids,
                                    valid_from=start,
                                    valid_to=end,
                                    basis=(
                                        f"由旧规则 {old_rule_id} 拆分，"
                                        "各子结论独立来源要求均满足"
                                    ),
                                )
                            )
                    else:
                        retain(
                            lc,
                            start,
                            end,
                            RETAIN_SPLIT_PENDING,
                            "拆分子结论 "
                            + ", ".join(spec.conclusion_id for spec in failed)
                            + " 未满足证据/独立来源要求，整体保留原归因待复核",
                        )
                        for spec in failed:
                            add_deficit_reviews(
                                evals[spec.conclusion_id].deficits,
                                successor.rule_id,
                                start,
                                end,
                            )

    # ---- 确定性排序 -------------------------------------------------------
    migrated.sort(key=lambda m: (m.valid_from, m.new_rule_id, m.conclusion_id))
    retained.sort(key=lambda r: (r.valid_from, r.legacy_rule_id, r.reason))
    review.sort(
        key=lambda i: (
            i.related_rule_id,
            i.reason,
            i.fact,
            i.obs_id or "",
            i.period_start or datetime.min.replace(tzinfo=timezone.utc),
        )
    )

    return MigrationReport(migrated=migrated, re_review=review, retained=retained)
