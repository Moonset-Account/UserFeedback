"""证据回溯：把每条通告解析为它可依赖的原始观测集合族。

采用最小不动点迭代：观测解析为自身；通告的某个证据组在组内全部
引用都可解析时，产生若干“底层观测集合”（组内各引用的解析做笛卡尔
并集）。只增不减，集合族有限，迭代必然终止。

纯粹互引的循环（环上没有任何观测落地）永远不会获得解析结果，
因此“循环引用不算观测”由该构造自然保证。
"""

from __future__ import annotations

from .models import Advisory, Observation


def resolve_observation_sets(
    observations: list[Observation],
    advisories: list[Advisory],
) -> tuple[dict[str, set[frozenset[str]]], dict[str, dict[frozenset[str], tuple[str, ...]]]]:
    """返回 (resolved, origin)。

    resolved[node] 是该节点可回溯到的“原始观测编号集合”的族；
    origin[node][观测集合] 记录该集合由哪一条证据组首次推出（用于裁决回报）。
    """
    resolved: dict[str, set[frozenset[str]]] = {}
    origin: dict[str, dict[frozenset[str], tuple[str, ...]]] = {}

    for obs in observations:
        resolved[obs.id] = {frozenset({obs.id})}
        origin[obs.id] = {}
    for adv in advisories:
        resolved[adv.id] = set()
        origin[adv.id] = {}

    changed = True
    while changed:
        changed = False
        for adv in advisories:
            for group in adv.evidence_groups:
                partials: list[frozenset[str]] | None = [frozenset()]
                for ref in group:
                    options = resolved.get(ref)
                    if not options:
                        partials = None
                        break
                    partials = [p | o for p in partials for o in options]
                if not partials:
                    continue
                for obs_set in partials:
                    if obs_set not in resolved[adv.id]:
                        resolved[adv.id].add(obs_set)
                        origin[adv.id].setdefault(obs_set, group)
                        changed = True
    return resolved, origin


def minimal_sets(obs_sets: set[frozenset[str]]) -> list[frozenset[str]]:
    """裁剪为反链（去掉被更小子集覆盖的集合）。

    对“两套来源不交叠”的判定而言，更小的集合永远不更差，
    因此只保留极小集合是完备的。
    """
    kept: list[frozenset[str]] = []
    for s in sorted(obs_sets, key=lambda x: (len(x), sorted(x))):
        if not any(t <= s for t in kept):
            kept.append(s)
    return kept
