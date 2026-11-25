"""Domain statistics and spreading-rule gate evaluations.

The same counting logic is used for:

* the per-step evidence (eligible domains and per-domain replica counts),
* the deviation check applied to any *newly started* replica,
* the final balanced-layout goal test.

Only domains containing at least one node that satisfies the replica's node
selection condition are eligible. A rack excluded by the selector therefore
never appears in the eligible set and can never be counted as a zero-replica
rack.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .model import DomainStat, GateEvaluation, Node, Replica, SpreadRule


def _domain_of(node: Node, topology_key: str) -> str | None:
    return node.labels.get(topology_key)


def _eligible_domains(nodes: Sequence[Node], replica: Replica, topology_key: str) -> tuple[str, ...]:
    """Domains that contain >= 1 node satisfying the replica's selector.

    Domains whose nodes are all rejected by the selector (e.g. an excluded
    rack) are omitted entirely; they are not reported with a zero count.
    Nodes without the topology label cannot belong to any domain.
    """

    eligible: set[str] = set()
    for node in nodes:
        domain = _domain_of(node, topology_key)
        if domain is None:
            continue
        if replica.selector.matches(node):
            eligible.add(domain)
    return tuple(sorted(eligible))


def _count_replicas_per_domain(
    nodes: Sequence[Node],
    placement: Mapping[str, frozenset[str]],
    topology_key: str,
) -> dict[str, int]:
    """Count currently running service replicas on each present domain.

    Every running instance counts toward the domain of the node it runs on,
    regardless of that instance's own selector condition. Domains that no
    running instance uses are simply absent from the mapping (the caller
    re-introduces zero counts for *eligible* domains).
    """

    node_by_id = {node.node_id: node for node in nodes}
    counts: dict[str, int] = {}
    for running_nodes in placement.values():
        for node_id in running_nodes:
            node = node_by_id[node_id]
            domain = _domain_of(node, topology_key)
            if domain is None:
                continue
            counts[domain] = counts.get(domain, 0) + 1
    return counts


def evaluate_domains(
    nodes: Sequence[Node],
    placement: Mapping[str, frozenset[str]],
    replica: Replica,
    rule: SpreadRule,
) -> DomainStat:
    """Compute eligible domains and same-service replica counts for one rule.

    Counts are restricted to eligible domains; ineligible domains (including
    selector-excluded racks) never receive a zero entry here.
    """

    eligible = _eligible_domains(nodes, replica, rule.topology_key)
    raw_counts = _count_replicas_per_domain(nodes, placement, rule.topology_key)
    counts = {domain: raw_counts.get(domain, 0) for domain in eligible}
    eligible_count = len(eligible)
    if eligible_count < rule.min_domains:
        effective_min = 0
    else:
        effective_min = min(counts.values())
    max_count = max(counts.values(), default=0)
    return DomainStat(
        rule_name=rule.name,
        topology_key=rule.topology_key,
        eligible_domains=eligible,
        replica_counts=dict(sorted(counts.items())),
        eligible_domain_count=eligible_count,
        min_domains=rule.min_domains,
        effective_min=effective_min,
        max_count=max_count,
        max_skew_observed=max_count - effective_min,
    )


def evaluate_start(
    nodes: Sequence[Node],
    placement: Mapping[str, frozenset[str]],
    replica: Replica,
    target_node: Node,
    rule: SpreadRule,
) -> GateEvaluation:
    """Deviation check for starting ``replica`` on ``target_node``.

    The check simulates the extra instance and compares the target domain's
    post-start count against the pre-start global effective minimum.
    """

    target_domain = _domain_of(target_node, rule.topology_key)
    stat = evaluate_domains(nodes, placement, replica, rule)

    if not replica.selector.matches(target_node):
        return GateEvaluation(
            rule_name=rule.name,
            topology_key=rule.topology_key,
            target_domain=target_domain,
            allowed=False,
            max_skew=rule.max_skew,
            observed_skew=stat.max_skew_observed,
            effective_min=stat.effective_min,
            eligible_domain_count=stat.eligible_domain_count,
            replica_counts_after=dict(stat.replica_counts),
            reason="target node does not satisfy the replica node selector",
        )
    if target_domain is None:
        return GateEvaluation(
            rule_name=rule.name,
            topology_key=rule.topology_key,
            target_domain=None,
            allowed=False,
            max_skew=rule.max_skew,
            observed_skew=stat.max_skew_observed,
            effective_min=stat.effective_min,
            eligible_domain_count=stat.eligible_domain_count,
            replica_counts_after=dict(stat.replica_counts),
            reason=f"target node has no {rule.topology_key!r} label",
        )
    if target_domain not in stat.eligible_domains:
        # Defensive: selector matched above, so the domain must be eligible.
        return GateEvaluation(
            rule_name=rule.name,
            topology_key=rule.topology_key,
            target_domain=target_domain,
            allowed=False,
            max_skew=rule.max_skew,
            observed_skew=stat.max_skew_observed,
            effective_min=stat.effective_min,
            eligible_domain_count=stat.eligible_domain_count,
            replica_counts_after=dict(stat.replica_counts),
            reason="target domain is not eligible for the replica selector",
        )

    counts_after = dict(stat.replica_counts)
    counts_after[target_domain] += 1
    observed_skew = counts_after[target_domain] - stat.effective_min
    allowed = observed_skew <= rule.max_skew
    if allowed:
        reason = "within max_skew"
    else:
        reason = (
            f"skew {observed_skew} exceeds max_skew {rule.max_skew} "
            f"(effective minimum {stat.effective_min})"
        )
    return GateEvaluation(
        rule_name=rule.name,
        topology_key=rule.topology_key,
        target_domain=target_domain,
        allowed=allowed,
        max_skew=rule.max_skew,
        observed_skew=observed_skew,
        effective_min=stat.effective_min,
        eligible_domain_count=stat.eligible_domain_count,
        replica_counts_after=dict(sorted(counts_after.items())),
        reason=reason,
    )


def find_balance_violations(
    nodes: Sequence[Node],
    placement: Mapping[str, frozenset[str]],
    replicas: Sequence[Replica],
    rules: Sequence[SpreadRule],
) -> list[tuple[Replica, SpreadRule, DomainStat]]:
    """Return every (replica, rule) pair whose observed skew is too large."""

    violations: list[tuple[Replica, SpreadRule, DomainStat]] = []
    for replica in replicas:
        for rule in rules:
            stat = evaluate_domains(nodes, placement, replica, rule)
            if stat.max_skew_observed > rule.max_skew:
                violations.append((replica, rule, stat))
    return violations
