"""指标级裁决：佐证 / 争议 / 未佐证，并给出支撑裁决的两套来源。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .models import Advisory, Observation
from .resolver import minimal_sets

STATUS_CORROBORATED = "corroborated"
STATUS_DISPUTED = "disputed"
STATUS_UNCORROBORATED = "uncorroborated"


@dataclass(frozen=True)
class EvidenceSide:
    """支撑某方向结论的一套依据（已回溯到原始观测与采集源）。"""

    advisory_id: str
    evidence_group: tuple[str, ...]
    observation_ids: tuple[str, ...]
    sources: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "advisory_id": self.advisory_id,
            "evidence_group": list(self.evidence_group),
            "observation_ids": list(self.observation_ids),
            "sources": list(self.sources),
        }


@dataclass
class IndicatorVerdict:
    """单个指标的裁决结果。

    status: corroborated（恰好一个方向达标）/ disputed（两个及以上方向达标）/ uncorroborated。
    support: 方向 -> 实际支撑该方向的两套独立来源。
    """

    indicator: str
    status: str
    support: dict[str, tuple[EvidenceSide, EvidenceSide]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "indicator": self.indicator,
            "status": self.status,
            "support": {
                direction: [side_a.to_dict(), side_b.to_dict()]
                for direction, (side_a, side_b) in sorted(self.support.items())
            },
        }


def evaluate_indicator(
    indicator: str,
    advisories: list[Advisory],
    obs_sources: dict[str, str],
    resolved: dict[str, set[frozenset[str]]],
    origin: dict[str, dict[frozenset[str], tuple[str, ...]]],
) -> IndicatorVerdict:
    """对单个指标做裁决。

    佐证门槛：同一方向下存在两条不同通告，各自可选出一套可解析证据组，
    且两套依据回溯到的原始采集源完全不交叠。
    """

    def sources_of(obs_set: frozenset[str]) -> frozenset[str]:
        return frozenset(obs_sources[o] for o in obs_set)

    # 方向 -> [(通告id, 底层观测集合)]，仅保留极小集合
    candidates: dict[str, list[tuple[str, frozenset[str]]]] = defaultdict(list)
    for adv in advisories:
        if adv.indicator != indicator:
            continue
        for obs_set in minimal_sets(resolved.get(adv.id, set())):
            candidates[adv.direction].append((adv.id, obs_set))

    support: dict[str, tuple[EvidenceSide, EvidenceSide]] = {}
    for direction, cands in candidates.items():
        for i in range(len(cands)):
            adv_a, set_a = cands[i]
            for j in range(i + 1, len(cands)):
                adv_b, set_b = cands[j]
                if adv_a == adv_b:
                    continue  # 两个结论必须来自不同通告
                if not sources_of(set_a).isdisjoint(sources_of(set_b)):
                    continue  # 原始采集源必须完全不交叠
                support[direction] = (
                    _side(adv_a, set_a, obs_sources, origin),
                    _side(adv_b, set_b, obs_sources, origin),
                )
                break
            if direction in support:
                break

    if len(support) >= 2:
        status = STATUS_DISPUTED
    elif len(support) == 1:
        status = STATUS_CORROBORATED
    else:
        status = STATUS_UNCORROBORATED
    return IndicatorVerdict(indicator=indicator, status=status, support=support)


def _side(
    advisory_id: str,
    obs_set: frozenset[str],
    obs_sources: dict[str, str],
    origin: dict[str, dict[frozenset[str], tuple[str, ...]]],
) -> EvidenceSide:
    group = origin[advisory_id].get(obs_set, ())
    return EvidenceSide(
        advisory_id=advisory_id,
        evidence_group=tuple(group),
        observation_ids=tuple(sorted(obs_set)),
        sources=tuple(sorted({obs_sources[o] for o in obs_set})),
    )
