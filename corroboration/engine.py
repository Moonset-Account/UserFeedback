"""判定引擎：校验输入、回溯证据、逐指标裁决。"""

from __future__ import annotations

from .models import Advisory, Observation
from .resolver import resolve_observation_sets
from .verdict import IndicatorVerdict, evaluate_indicator


class CorroborationEngine:
    """佐证判定引擎。

    用法：
        engine = CorroborationEngine(observations, advisories)
        verdicts = engine.evaluate()   # {指标编号: IndicatorVerdict}
    """

    def __init__(self, observations: list[Observation], advisories: list[Advisory]):
        self.observations = list(observations)
        self.advisories = list(advisories)
        self._validate()
        self._obs_sources = {o.id: o.source for o in self.observations}
        self._resolved, self._origin = resolve_observation_sets(
            self.observations, self.advisories
        )

    def _validate(self) -> None:
        obs_ids = [o.id for o in self.observations]
        adv_ids = [a.id for a in self.advisories]
        if len(set(obs_ids)) != len(obs_ids):
            raise ValueError("观测编号重复")
        if len(set(adv_ids)) != len(adv_ids):
            raise ValueError("通告编号重复")
        known = set(obs_ids) | set(adv_ids)
        if len(known) != len(obs_ids) + len(adv_ids):
            raise ValueError("观测编号与通告编号命名空间冲突")
        for o in self.observations:
            if not o.indicator or not o.source:
                raise ValueError(f"观测 {o.id} 缺少指标编号或采集源")
        for a in self.advisories:
            if not a.indicator or not a.direction:
                raise ValueError(f"通告 {a.id} 缺少指标编号或结论方向")
            if not a.evidence_groups:
                raise ValueError(f"通告 {a.id} 没有任何证据组")
            for group in a.evidence_groups:
                if not group:
                    raise ValueError(f"通告 {a.id} 存在空证据组（空组不能作为依据）")
                for ref in group:
                    if ref not in known:
                        raise ValueError(f"通告 {a.id} 引用了未知材料 {ref}")

    def evaluate(self) -> dict[str, IndicatorVerdict]:
        indicators = {o.indicator for o in self.observations}
        indicators |= {a.indicator for a in self.advisories}
        return {
            ind: evaluate_indicator(
                ind, self.advisories, self._obs_sources, self._resolved, self._origin
            )
            for ind in sorted(indicators)
        }
