"""Public API for the static error-flow disclosure checker."""

from __future__ import annotations

from typing import Any

from .analysis import FlowAnalyzer
from .graph import load_graph
from .model import AuditReport, ReverseSlice
from .policy import DisclosurePolicy, identity_filter


def make_policy(
    public_diagnostics: Any = None,
    caller: Any = None,
    *,
    policy: DisclosurePolicy | None = None,
    public_codes: Any = None,
    allowed_fields: Any = None,
    allowed_sources: Any = None,
    **kwargs: Any,
) -> DisclosurePolicy:
    if policy is not None:
        if caller is None:
            return policy
        return policy.for_caller(caller)
    return DisclosurePolicy(
        caller=caller,
        public_diagnostics=public_diagnostics,
        public_codes=public_codes,
        allowed_fields=allowed_fields,
        allowed_sources=allowed_sources,
        **kwargs,
    )


def audit_flow(
    graph: Any = None,
    public_diagnostics: Any = None,
    caller: Any = None,
    *,
    policy: DisclosurePolicy | None = None,
    public_codes: Any = None,
    allowed_fields: Any = None,
    allowed_sources: Any = None,
    **kwargs: Any,
) -> AuditReport:
    """Audit all reachable response exits in ``graph``.

    The common positional form is ``audit_flow(graph, public_ids, caller)``.
    """

    if graph is None:
        raise TypeError("audit_flow() requires a graph")

    # Allow audit_flow(policy, graph) or audit_flow(graph, policy).
    if isinstance(graph, DisclosurePolicy):
        policy = graph
        graph = public_diagnostics
        public_diagnostics = None
    elif isinstance(public_diagnostics, DisclosurePolicy):
        policy = public_diagnostics
        public_diagnostics = None

    resolved_policy = make_policy(
        public_diagnostics,
        caller,
        policy=policy,
        public_codes=public_codes,
        allowed_fields=allowed_fields,
        allowed_sources=allowed_sources,
        **kwargs,
    )
    return FlowAnalyzer(resolved_policy).audit(graph)


# Descriptive aliases.
check = audit_flow
check_flow = audit_flow
analyze_flow = audit_flow
audit_error_flow = audit_flow
check_error_flow = audit_flow
run_check = audit_flow


def reverse_scan(
    graph: Any,
    exit_id: str,
    *,
    policy: DisclosurePolicy | None = None,
    public_diagnostics: Any = None,
    caller: Any = None,
) -> ReverseSlice:
    """Return the semantic backward slice for one response exit."""

    resolved_policy = make_policy(
        public_diagnostics, caller, policy=policy
    )
    analyzer = FlowAnalyzer(resolved_policy)
    analyzer.audit(graph)
    return analyzer.reverse_slice(exit_id)


def reverse_identity_filter(
    graph: Any,
    caller: Any,
    public_diagnostics: Any = None,
    *,
    exit_id: str | None = None,
    policy: DisclosurePolicy | None = None,
) -> dict[str, ReverseSlice] | ReverseSlice:
    """Backward-scan exits and retain only diagnostics visible to ``caller``.

    With ``exit_id`` supplied, returns one :class:`ReverseSlice`; otherwise
    returns a mapping from every reachable response exit id to its slice.
    """

    resolved_policy = make_policy(
        public_diagnostics, caller, policy=policy
    )
    analyzer = FlowAnalyzer(resolved_policy)
    report = analyzer.audit(graph)
    if exit_id is not None:
        return analyzer.reverse_slice(exit_id)

    result: dict[str, ReverseSlice] = {}
    for exit_report in report.reachable_exits:
        result[exit_report.exit_id] = analyzer.reverse_slice(exit_report.exit_id)
    return result


def filter_diagnostics_for_identity(
    diagnostics: Any,
    caller: Any,
    public_diagnostics: Any = None,
    *,
    policy: DisclosurePolicy | None = None,
) -> tuple[list[Any], list[Any]]:
    """Split diagnostic declarations/uses into allowed and blocked lists."""

    resolved_policy = make_policy(
        public_diagnostics, caller, policy=policy
    )
    return identity_filter(diagnostics, caller, resolved_policy)


def graph_from_dict(data: Any):
    """Parse and normalize a graph (mainly useful for API exploration)."""

    return load_graph(data)
