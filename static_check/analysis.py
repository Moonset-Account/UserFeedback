"""Static propagation and disclosure analysis for error-flow graphs."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Iterable

from .graph import (
    NormalizedGraph,
    diagnostic_specs,
    first_present,
    load_graph,
    parse_diagnostics,
)
from .model import (
    AuditReport,
    DiagnosticReport,
    DiagnosticUse,
    ErrorReport,
    ErrorValue,
    ExitReport,
    FieldReport,
    Finding,
    PropState,
    ResponseScenario,
    ReverseSlice,
)
from .policy import DisclosurePolicy


class FlowAnalysisError(ValueError):
    """Raised for API misuse that cannot be represented as a graph finding."""


class FlowAnalyzer:
    """Analyze one graph with a fixed :class:`DisclosurePolicy`."""

    def __init__(self, policy: DisclosurePolicy | None = None) -> None:
        self.policy = policy or DisclosurePolicy()
        self.caller_name = (
            self.policy.caller.identity if self.policy.caller is not None else None
        )
        self.findings: list[Finding] = []
        self._finding_keys: set[tuple[Any, ...]] = set()
        self.states_by_node: dict[str, list[PropState]] = {}
        self.graph: NormalizedGraph | None = None

    # ------------------------------------------------------------------
    # Public orchestration
    # ------------------------------------------------------------------
    def audit(self, graph: Any) -> AuditReport:
        normalized = load_graph(graph)
        self.graph = normalized
        self.findings = []
        self._finding_keys = set()
        self.states_by_node = {node_id: [] for node_id in normalized.nodes}

        reachable = self._reachable_nodes(normalized)
        topo_findings = self._evaluate_nodes(normalized, reachable)

        exits: list[ExitReport] = []
        for exit_id in normalized.response_ids:
            if exit_id not in reachable:
                exits.append(
                    ExitReport(
                        exit_id=exit_id,
                        reachable=False,
                        kind=self._declared_response_kind(
                            normalized.nodes[exit_id], None
                        ),
                    )
                )
                continue
            exits.append(self._analyze_exit(normalized, exit_id))

        # Response nodes are the normal terminal exits.  If a graph marks some
        # other node as an exit alias, normalize_graph already typed it.
        report = AuditReport(
            caller=self.caller_name,
            exits=exits,
            findings=list(self.findings),
            graph_errors=list(normalized.findings) + topo_findings,
        )
        return report

    def reverse_slice(self, exit_id: str) -> ReverseSlice:
        if self.graph is None:
            raise FlowAnalysisError("call audit() before reverse_slice()")
        report = self.audit(self.graph)
        exit_report = report.exit(exit_id)
        uses = _collect_exit_diagnostic_uses(exit_report)
        nodes: list[str] = [exit_id]
        for use in uses:
            for node_id in reversed(use.route):
                if node_id not in nodes:
                    nodes.append(node_id)
            if use.origin_node not in nodes:
                nodes.append(use.origin_node)

        allowed: list[DiagnosticUse] = []
        blocked: list[DiagnosticUse] = []
        for use in uses:
            ok, _ = self.policy.diagnostic_allowed(
                use.diagnostic_id, source=use.source
            )
            (allowed if ok else blocked).append(use)

        return ReverseSlice(
            exit_id=exit_id,
            nodes=tuple(nodes),
            diagnostics=tuple(dict.fromkeys(uses)),
            allowed_diagnostics=tuple(allowed),
            blocked_diagnostics=tuple(blocked),
        )

    # ------------------------------------------------------------------
    # Findings
    # ------------------------------------------------------------------
    def add_finding(
        self,
        code: str,
        message: str,
        *,
        severity: str = "error",
        exit_id: str | None = None,
        node_id: str | None = None,
        diagnostic_id: str | None = None,
        error_code: str | None = None,
        field_name: str | None = None,
        source: str | None = None,
        route: tuple[str, ...] = (),
        details: dict[str, Any] | None = None,
    ) -> Finding:
        finding = Finding(
            code=code,
            message=message,
            severity=severity,  # type: ignore[arg-type]
            exit_id=exit_id,
            node_id=node_id,
            diagnostic_id=diagnostic_id,
            error_code=error_code,
            field_name=field_name,
            caller=self.caller_name,
            source=source,
            route=tuple(route),
            details=dict(details or {}),
        )
        key = (
            finding.code,
            finding.exit_id,
            finding.node_id,
            finding.diagnostic_id,
            finding.error_code,
            finding.field_name,
            finding.source,
            finding.route,
        )
        if key not in self._finding_keys:
            self._finding_keys.add(key)
            self.findings.append(finding)
        return finding

    # ------------------------------------------------------------------
    # Reachability and topological propagation
    # ------------------------------------------------------------------
    def _reachable_nodes(self, graph: NormalizedGraph) -> set[str]:
        reachable: set[str] = set()
        queue: deque[str] = deque()
        for entry in graph.entries:
            if entry in graph.nodes:
                reachable.add(entry)
                queue.append(entry)

        while queue:
            node_id = queue.popleft()
            for edge in graph.outgoing(node_id):
                target = edge["to"]
                if target in graph.nodes and target not in reachable:
                    reachable.add(target)
                    queue.append(target)
        return reachable

    def _evaluate_nodes(
        self, graph: NormalizedGraph, reachable: set[str]
    ) -> list[Finding]:
        indegree = {node_id: 0 for node_id in reachable}
        for edge in graph.edges:
            if edge["from"] in reachable and edge["to"] in reachable:
                indegree[edge["to"]] = indegree.get(edge["to"], 0) + 1

        queue = deque(node_id for node_id, degree in indegree.items() if degree == 0)
        processed = 0
        topo_findings: list[Finding] = []

        while queue:
            node_id = queue.popleft()
            processed += 1
            node = graph.nodes[node_id]

            incoming: list[tuple[dict[str, Any], PropState]] = []
            for edge in graph.incoming(node_id):
                if edge["from"] not in reachable:
                    continue
                for state in self.states_by_node.get(edge["from"], []):
                    if self._branch_accepts(edge.get("branch", "any"), state):
                        incoming.append((edge, state))

            try:
                outputs = self._process_node(graph, node, incoming)
            except Exception as exc:  # defensive: report malformed graph instead of crashing
                finding = self.add_finding(
                    "INVALID_FLOW_NODE",
                    f"node {node_id!r} could not be analyzed: {exc}",
                    node_id=node_id,
                    details={"exception": type(exc).__name__},
                )
                topo_findings.append(finding)
                outputs = []

            # De-duplicate semantically identical alternative states.
            self.states_by_node[node_id] = list(dict.fromkeys(outputs))

            for edge in graph.outgoing(node_id):
                target = edge["to"]
                if target in indegree:
                    indegree[target] -= 1
                    if indegree[target] == 0:
                        queue.append(target)

        if processed < len(reachable):
            cycle_nodes = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
            finding = self.add_finding(
                "UNSUPPORTED_FLOW_CYCLE",
                "error flow contains a cycle that cannot be statically summarized",
                severity="error",
                details={"nodes": cycle_nodes},
                route=tuple(cycle_nodes),
            )
            topo_findings.append(finding)
        return topo_findings

    @staticmethod
    def _branch_accepts(branch: str, state: PropState) -> bool:
        if branch in {"any", "recovery"}:
            return True
        if branch == "normal":
            return state.kind == "normal"
        if branch in {"error", "member"}:
            return state.kind == "error"
        return True

    # ------------------------------------------------------------------
    # Node processing
    # ------------------------------------------------------------------
    def _process_node(
        self,
        graph: NormalizedGraph,
        node: dict[str, Any],
        incoming: list[tuple[dict[str, Any], PropState]],
    ) -> list[PropState]:
        node_id = node["id"]
        node_type = node.get("type", "task")

        if node_type == "start":
            return [PropState.normal(route=(node_id,))]

        if node_type == "response":
            # Exits are analyzed separately with full incoming context.
            return []

        states = [state for _, state in incoming]

        if node_type == "raise":
            return self._process_raise(node)

        if node_type == "wrap":
            return self._process_wrap(node, states)

        if node_type in {"recover", "replace"}:
            return self._process_recovery(node, states)

        if node_type == "aggregate":
            return [self._process_aggregate(graph, node, incoming)]

        if node_type == "filter":
            return [self._process_filter(state, node_id) for state in states] or [
                PropState.normal(route=(node_id,))
            ]

        # task/step/default: propagate incoming states and optionally emit a
        # locally declared fault as another possible outcome.
        results: list[PropState] = []
        if states:
            for state in states:
                results.append(self._pass_through(state, node_id))
        else:
            results.append(PropState.normal(route=(node_id,)))

        fault_spec = first_present(node, "error", "fault", "raises", "throws")
        if fault_spec is not None:
            results.extend(self._fault_states_from_spec(fault_spec, node_id))
        elif node.get("code") and node.get("emit_error"):
            results.append(
                PropState.error_state(
                    self._build_error(node, node_id), route=(node_id,)
                )
            )
        return results

    def _pass_through(self, state: PropState, node_id: str) -> PropState:
        route = self._next_route(state.route, node_id)
        if state.kind == "error" and state.error is not None:
            return PropState.error_state(
                self._extend_error_route(state.error, node_id), route=route
            )
        diagnostics = tuple(
            replace(item, route=self._next_route(item.route, node_id))
            for item in state.diagnostics
        )
        return PropState.normal(diagnostics=diagnostics, route=route)

    @staticmethod
    def _next_route(route: tuple[str, ...], node_id: str) -> tuple[str, ...]:
        if route and route[-1] == node_id:
            return route
        return route + (node_id,)

    def _fault_states_from_spec(self, spec: Any, node_id: str) -> list[PropState]:
        if isinstance(spec, list):
            return [
                PropState.error_state(self._build_error(item, node_id), route=(node_id,))
                for item in spec
            ]
        if spec is True:
            spec = {}
        return [PropState.error_state(self._build_error(spec, node_id), route=(node_id,))]

    def _process_raise(self, node: dict[str, Any]) -> list[PropState]:
        node_id = node["id"]
        specs: list[Any] = []
        if node.get("errors"):
            value = node["errors"]
            specs = value if isinstance(value, list) else [value]
        else:
            spec = first_present(node, "error", "fault", "throw", "raises", "throws")
            if spec is None:
                spec = node
            specs = spec if isinstance(spec, list) else [spec]

        results: list[PropState] = []
        for spec in specs:
            if spec is True:
                spec = node
            error = self._build_error(spec, node_id)
            if error.code is None and not error.diagnostics and not error.members:
                self.add_finding(
                    "EMPTY_FAULT",
                    "raise node does not define a code or diagnostic context",
                    severity="warning",
                    node_id=node_id,
                )
            results.append(PropState.error_state(error, route=(node_id,)))
        return results

    def _process_wrap(
        self, node: dict[str, Any], states: list[PropState]
    ) -> list[PropState]:
        node_id = node["id"]
        error_states = [state for state in states if state.kind == "error" and state.error]
        if not error_states:
            self.add_finding(
                "WRAP_WITHOUT_ERROR",
                "wrap node is not reachable by an error state",
                node_id=node_id,
            )
            # Still analyze a locally declared wrapper if supplied.
            return [
                PropState.error_state(self._build_error(node, node_id), route=(node_id,))
            ]

        outer_spec = first_present(node, "wrapper", "outer_error", default=node) or node
        outer = self._build_error(outer_spec, node_id)
        include_cause = bool(
            first_present(node, "include_cause", "keep_cause", "retain_cause", default=True)
        )
        results: list[PropState] = []
        for state in error_states:
            route = self._next_route(state.route, node_id)
            cause = self._extend_error_route(state.error, node_id) if include_cause else None
            wrapped = replace(
                outer,
                cause=cause,
                origin_node=node_id,
            )
            # Outer diagnostics were parsed with route (node_id,); ensure the
            # complete propagation route is visible on every diagnostic.
            wrapped = self._prepend_route_to_outer_diagnostics(wrapped, route)
            results.append(PropState.error_state(wrapped, route=route))
        return results

    def _prepend_route_to_outer_diagnostics(
        self, error: ErrorValue, route: tuple[str, ...]
    ) -> ErrorValue:
        diagnostics = tuple(replace(item, route=route) for item in error.diagnostics)
        return replace(error, diagnostics=diagnostics)

    def _process_recovery(
        self, node: dict[str, Any], states: list[PropState]
    ) -> list[PropState]:
        node_id = node["id"]
        error_states = [state for state in states if state.kind == "error" and state.error]
        replacement_spec = first_present(
            node,
            "replacement",
            "replace",
            "new_error",
            "fallback_error",
            "replacement_error",
        )
        if node.get("type") == "replace" and replacement_spec is None:
            replacement_spec = node
        elif (
            replacement_spec is None
            and node.get("type") == "recover"
            and (node.get("code") is not None or node.get("error") is not None)
            and not node.get("resume", False)
        ):
            replacement_spec = first_present(node, "error", default=node)

        results: list[PropState] = []
        if not error_states:
            if replacement_spec is not None and replacement_spec is not False:
                spec = node if replacement_spec is True else replacement_spec
                results.append(
                    PropState.error_state(
                        self._build_error(spec, node_id), route=(node_id,)
                    )
                )
            else:
                results.append(PropState.normal(route=(node_id,)))
            return results

        for state in error_states:
            route = self._next_route(state.route, node_id)
            if replacement_spec is not None and replacement_spec is not False:
                spec = node if replacement_spec is True else replacement_spec
                replacement = self._build_error(spec, node_id)

                keep_cause = first_present(
                    node, "keep_cause", "wrap_original", "attach_cause", "cause"
                )
                if keep_cause in (True, "@input", "@caught", "input", "caught", "original"):
                    replacement = replace(
                        replacement,
                        cause=self._extend_error_route(state.error, node_id),
                    )

                carry = first_present(
                    node,
                    "carry_diagnostics",
                    "inherit_diagnostics",
                    "retain_diagnostics",
                )
                if carry:
                    selector = "all" if carry is True else carry
                    inherited = self._select_uses(
                        state.error.all_diagnostics(True), selector, node_id
                    )
                    inherited = tuple(
                        replace(item, route=self._next_route(item.route, node_id))
                        for item in inherited
                    )
                    replacement = replace(
                        replacement,
                        diagnostics=tuple(replacement.diagnostics) + inherited,
                    )
                results.append(PropState.error_state(replacement, route=route))
                continue

            # Recovery turns the flow into a normal response.  Fault
            # diagnostics are dropped unless explicitly propagated.
            propagated: tuple[DiagnosticUse, ...] = ()
            carry = first_present(
                node,
                "propagate_diagnostics",
                "propagates",
                "propagate",
                "retain",
                "retained_diagnostics",
                "carry_diagnostics",
            )
            if carry:
                selector = "all" if carry is True else carry
                propagated = self._select_uses(
                    state.error.all_diagnostics(True), selector, node_id
                )
                propagated = tuple(
                    replace(item, route=self._next_route(item.route, node_id))
                    for item in propagated
                )

            new_diagnostics = parse_diagnostics(
                first_present(
                    node,
                    "normal_diagnostics",
                    "response_diagnostics",
                    "diagnostics",
                    default=[],
                ),
                node_id,
                route=route,
            )
            results.append(
                PropState.normal(
                    diagnostics=tuple(propagated) + tuple(new_diagnostics),
                    route=route,
                )
            )
        return results

    def _select_uses(
        self,
        uses: Iterable[DiagnosticUse],
        selector: Any,
        node_id: str,
    ) -> tuple[DiagnosticUse, ...]:
        uses_tuple = tuple(uses)
        if selector is True or (
            isinstance(selector, str) and selector in {"all", "*"}
        ):
            return uses_tuple
        wanted = {str(item) for item in diagnostic_specs(selector)}
        selected = tuple(item for item in uses_tuple if item.diagnostic_id in wanted)
        found = {item.diagnostic_id for item in selected}
        for missing in sorted(wanted - found):
            self.add_finding(
                "PROPAGATED_DIAGNOSTIC_NOT_AVAILABLE",
                f"recovery at {node_id!r} cannot propagate {missing!r}: it is not in the caught fault",
                severity="warning",
                node_id=node_id,
                diagnostic_id=missing,
            )
        return selected

    def _process_aggregate(
        self,
        graph: NormalizedGraph,
        node: dict[str, Any],
        incoming: list[tuple[dict[str, Any], PropState]],
    ) -> PropState:
        node_id = node["id"]
        members: list[ErrorValue] = []
        seen_errors: set[ErrorValue] = set()

        def add_error(error: ErrorValue | None) -> None:
            if error is not None and error not in seen_errors:
                seen_errors.add(error)
                members.append(error)

        # Explicit templates and node references.
        explicit = first_present(
            node, "members", "errors", "faults", "aggregate_errors", default=[]
        )
        for item in explicit if isinstance(explicit, list) else [explicit]:
            if isinstance(item, str) and item in graph.nodes:
                for state in self.states_by_node.get(item, []):
                    if state.kind == "error":
                        add_error(state.error)
            elif isinstance(item, Mapping) and (item.get("node") or item.get("ref")):
                ref = str(item.get("node") or item.get("ref"))
                for state in self.states_by_node.get(ref, []):
                    if state.kind == "error":
                        add_error(state.error)
            elif isinstance(item, PropState) and item.error:
                add_error(item.error)
            else:
                add_error(self._build_error(item, node_id))

        inputs = node.get("inputs") or node.get("input_nodes")
        if inputs:
            refs = inputs if isinstance(inputs, list) else [inputs]
            for ref in refs:
                ref_id = str(ref.get("node") or ref.get("ref")) if isinstance(ref, Mapping) else str(ref)
                for state in self.states_by_node.get(ref_id, []):
                    if state.kind == "error":
                        add_error(state.error)

        # Edge-carried members.  Member edges are canonical; ordinary incoming
        # error edges are also accepted for compact graphs.
        for edge, state in incoming:
            if state.kind == "error" and state.error is not None:
                if edge.get("branch") in {"member", "any", "error"}:
                    add_error(self._extend_error_route(state.error, node_id))

        outer = self._build_error(node, node_id)
        aggregate = replace(
            outer,
            aggregate=True,
            members=tuple(members),
            origin_node=node_id,
        )
        if not members:
            self.add_finding(
                "EMPTY_AGGREGATE",
                "aggregate node does not receive any fault members",
                severity="warning",
                node_id=node_id,
            )
        return PropState.error_state(aggregate, route=(node_id,))

    def _process_filter(self, state: PropState, node_id: str) -> PropState:
        route = self._next_route(state.route, node_id)
        if state.kind == "error" and state.error is not None:
            return PropState.error_state(
                self._filter_error_for_policy(state.error, node_id), route=route
            )
        diagnostics = tuple(
            item
            for item in state.diagnostics
            if self.policy.diagnostic_allowed(item.diagnostic_id, source=item.source)[0]
        )
        return PropState.normal(diagnostics=diagnostics, route=route)

    def _filter_error_for_policy(
        self, error: ErrorValue, node_id: str
    ) -> ErrorValue:
        diagnostics = tuple(
            item
            for item in error.diagnostics
            if self.policy.diagnostic_allowed(item.diagnostic_id, source=item.source)[0]
        )
        code_ok, _ = self.policy.code_allowed(error.code)
        cause = (
            self._filter_error_for_policy(error.cause, node_id)
            if error.cause is not None
            else None
        )
        members = tuple(
            self._filter_error_for_policy(member, node_id) for member in error.members
        )
        return replace(
            error,
            code=error.code if code_ok else None,
            diagnostics=diagnostics,
            cause=cause,
            members=members,
        )

    # ------------------------------------------------------------------
    # Error construction
    # ------------------------------------------------------------------
    def _build_error(self, spec: Any, node_id: str) -> ErrorValue:
        if isinstance(spec, ErrorValue):
            return self._extend_error_route(spec, node_id)
        if spec is None:
            return ErrorValue(code=None, origin_node=node_id)
        if isinstance(spec, str):
            text = spec.strip()
            if text.startswith("{"):
                import json

                try:
                    spec = json.loads(text)
                except json.JSONDecodeError:
                    spec = {"code": spec}
            else:
                return ErrorValue(code=spec, origin_node=node_id)
        if not isinstance(spec, Mapping):
            # Dataclass/object style.
            code = getattr(spec, "code", None) or getattr(spec, "error_code", None)
            diagnostics = parse_diagnostics(
                getattr(spec, "diagnostics", None), node_id
            )
            return ErrorValue(
                code=str(code) if code is not None else None,
                diagnostics=diagnostics,
                origin_node=node_id,
            )

        code = first_present(spec, "code", "error_code", "outer_code")
        diag_value = first_present(
            spec,
            "diagnostics",
            "diagnostic_ids",
            "diagnostic_locations",
            "locations",
            "context",
            default=[],
        )
        diagnostics = parse_diagnostics(diag_value, node_id)

        cause_spec = first_present(spec, "cause", "inner", "inner_error", "wrapped")
        cause: ErrorValue | None = None
        if isinstance(cause_spec, ErrorValue):
            cause = self._extend_error_route(cause_spec, node_id)
        elif isinstance(cause_spec, Mapping):
            cause = self._build_error(cause_spec, node_id)
        elif isinstance(cause_spec, str) and not cause_spec.startswith("@"):
            cause = self._build_error(cause_spec, node_id)

        member_specs = first_present(spec, "members", "errors", "faults")
        members: tuple[ErrorValue, ...] = ()
        if isinstance(member_specs, list):
            members = tuple(self._build_error(item, node_id) for item in member_specs)
        elif isinstance(member_specs, (Mapping, str)):
            members = (self._build_error(member_specs, node_id),)

        aggregate = bool(spec.get("aggregate", False)) or bool(members)
        visibility = str(spec.get("visibility", "")).lower()
        code_declared_public = bool(
            spec.get("code_public", False)
            or spec.get("public_code", False)
            or spec.get("public", False)
            or visibility in {"public", "disclosable"}
        )
        return ErrorValue(
            code=str(code) if code is not None else None,
            diagnostics=diagnostics,
            cause=cause,
            members=members,
            origin_node=node_id,
            aggregate=aggregate,
            label=spec.get("label"),
            code_declared_public=code_declared_public,
        )

    def _extend_error_route(self, error: ErrorValue, node_id: str) -> ErrorValue:
        def extend_uses(uses: tuple[DiagnosticUse, ...]) -> tuple[DiagnosticUse, ...]:
            return tuple(
                replace(item, route=self._next_route(item.route, node_id))
                for item in uses
            )

        cause = self._extend_error_route(error.cause, node_id) if error.cause else None
        members = tuple(self._extend_error_route(member, node_id) for member in error.members)
        return replace(
            error,
            diagnostics=extend_uses(error.diagnostics),
            cause=cause,
            members=members,
        )

    # ------------------------------------------------------------------
    # Exit analysis
    # ------------------------------------------------------------------
    def _declared_response_kind(
        self, node: dict[str, Any], state: PropState | None
    ) -> str | None:
        kind = first_present(
            node, "response_kind", "response_type", "result_kind", "outcome"
        )
        if kind is None:
            status_kind = node.get("kind")
            if status_kind in {"error", "normal", "ok", "success"}:
                kind = status_kind
        if kind is not None:
            text = str(kind).lower().replace("-", "_")
            if text in {"ok", "success", "successful", "normal", "response"}:
                return "normal"
            if text in {"error", "failure", "failed", "err"}:
                return "error"
        raw_type = str(node.get("raw_type", ""))
        if "error_response" in raw_type or raw_type == "error-response":
            return "error"
        if "ok_response" in raw_type or "success_response" in raw_type:
            return "normal"
        if state is not None:
            return state.kind
        status_code = node.get("status_code") or node.get("status")
        if isinstance(status_code, int) and status_code >= 400:
            return "error"
        if node.get("error") is not None or node.get("code") is not None:
            return "error"
        return "normal"

    def _analyze_exit(self, graph: NormalizedGraph, exit_id: str) -> ExitReport:
        node = graph.nodes[exit_id]
        incoming: list[tuple[dict[str, Any], PropState]] = []
        for edge in graph.incoming(exit_id):
            for state in self.states_by_node.get(edge["from"], []):
                if self._branch_accepts(edge.get("branch", "any"), state):
                    incoming.append((edge, state))

        states = list(dict.fromkeys(state for _, state in incoming))
        if not states:
            synthesized = self._synthesize_exit_state(node, exit_id)
            states = [synthesized]

        permitted_value = first_present(
            node,
            "permitted_fields",
            "allowed_fields",
            "licensed_fields",
            "field_allowlist",
        )
        permitted_fields = (
            tuple(str(item) for item in permitted_value)
            if isinstance(permitted_value, (list, tuple, set))
            else None
        )

        scenarios: list[ResponseScenario] = []
        exit_findings: list[Finding] = []
        for state in states:
            kind = self._declared_response_kind(node, state) or state.kind
            scenario = self._analyze_scenario(
                node=node,
                exit_id=exit_id,
                state=state,
                kind=kind,  # type: ignore[arg-type]
                permitted_fields=permitted_fields,
            )
            scenarios.append(scenario)

        return ExitReport(
            exit_id=exit_id,
            reachable=True,
            kind=scenarios[0].kind if scenarios else None,
            scenarios=tuple(scenarios),
            permitted_fields=permitted_fields,
            findings=tuple(exit_findings),
        )

    def _synthesize_exit_state(
        self, node: dict[str, Any], exit_id: str
    ) -> PropState:
        if node.get("error") is not None or (
            node.get("code") is not None and node.get("kind") == "error"
        ):
            spec = first_present(node, "error", default=node)
            return PropState.error_state(
                self._build_error(spec, exit_id), route=(exit_id,)
            )
        diagnostics = parse_diagnostics(
            first_present(
                node, "diagnostics", "diagnostic_ids", "locations", default=[]
            ),
            exit_id,
            route=(exit_id,),
        )
        return PropState.normal(diagnostics=diagnostics, route=(exit_id,))

    def _analyze_scenario(
        self,
        *,
        node: dict[str, Any],
        exit_id: str,
        state: PropState,
        kind: str,
        permitted_fields: tuple[str, ...] | None,
    ) -> ResponseScenario:
        route = self._next_route(state.route, exit_id)
        error_report: ErrorReport | None = None
        if kind == "error":
            error = state.error
            if error is None:
                # An explicitly declared error exit can define its own payload.
                error = self._build_error(
                    first_present(node, "error", default=node), exit_id
                )
            error_report = self._report_error(error, exit_id)

        available_uses = self._available_uses(state, error_report)
        uses_by_id = self._index_uses(available_uses)

        field_specs = self._field_specs(node, kind)
        if kind == "error" and state.error is not None:
            # Structural context is part of the propagated error value.  Add
            # projections only when the structure exists; graphs can suppress
            # either projection with explicit fields or emit_*=false.
            if (
                state.error.cause is not None
                and "cause" not in field_specs
                and node.get("emit_cause", True)
            ):
                field_specs["cause"] = "@cause"
            if (
                state.error.members
                and "members" not in field_specs
                and node.get("emit_members", True)
            ):
                field_specs["members"] = "@members"

        field_reports: list[FieldReport] = []
        safe_response: dict[str, Any] = {}

        for field_name, spec in field_specs.items():
            if field_name == "code" and kind == "error":
                report = self._analyze_code_field(
                    field_name=field_name,
                    spec=spec,
                    exit_id=exit_id,
                    route=route,
                    state=state,
                    error_report=error_report,
                    permitted_fields=permitted_fields,
                )
            else:
                report = self._analyze_field(
                    field_name=field_name,
                    spec=spec,
                    exit_id=exit_id,
                    route=route,
                    state=state,
                    error_report=error_report,
                    uses_by_id=uses_by_id,
                    available_uses=available_uses,
                    permitted_fields=permitted_fields,
                )
            field_reports.append(report)
            if report.licensed and report.safe_value is not None:
                safe_response[field_name] = report.safe_value
            elif report.licensed and field_name == "diagnostics":
                safe_response[field_name] = []

        return ResponseScenario(
            kind=kind,  # type: ignore[arg-type]
            route=route,
            error=error_report,
            fields=tuple(field_reports),
            safe_response=safe_response,
            findings=(),
        )

    def _field_specs(self, node: dict[str, Any], kind: str) -> dict[str, Any]:
        raw_fields = first_present(
            node, "fields", "response_fields", "payload_fields", "body"
        )
        specs: dict[str, Any]
        if raw_fields is None:
            # Both response kinds expose the diagnostics collection by default.
            # For a normal recovery path that collection contains only the
            # diagnostics explicitly propagated into the normal state.
            specs = {"diagnostics": "@diagnostics"}
        elif isinstance(raw_fields, Mapping):
            specs = dict(raw_fields)
        elif isinstance(raw_fields, list):
            specs = {}
            for item in raw_fields:
                name = str(item)
                if name == "code":
                    specs[name] = "@code"
                elif name in {"diagnostics", "diags", "locations"}:
                    specs[name] = "@diagnostics"
                else:
                    specs[name] = [name]
        else:
            specs = {"value": raw_fields}

        diag_value = first_present(
            node,
            "diagnostics",
            "diagnostic_ids",
            "diagnostic_locations",
            "locations",
        )
        if diag_value is not None:
            key = "diagnostics" if "diagnostics" not in specs else "diagnostic_locations"
            if key not in specs:
                specs[key] = {"sources": diag_value}

        if kind == "error" and node.get("include_code", True):
            if "code" not in specs:
                specs["code"] = node.get("code", "@code")
        return specs

    def _available_uses(
        self, state: PropState, error_report: ErrorReport | None
    ) -> tuple[DiagnosticUse, ...]:
        if state.kind == "error" and state.error is not None:
            return state.error.all_diagnostics(True)
        return tuple(state.diagnostics)

    @staticmethod
    def _index_uses(
        uses: tuple[DiagnosticUse, ...],
    ) -> dict[str, list[DiagnosticUse]]:
        result: dict[str, list[DiagnosticUse]] = {}
        for use in uses:
            result.setdefault(use.diagnostic_id, []).append(use)
        return result

    # ------------------------------------------------------------------
    # Error policy reports
    # ------------------------------------------------------------------
    def _report_error(self, error: ErrorValue, exit_id: str) -> ErrorReport:
        findings: list[Finding] = []
        code_allowed = True
        if error.code is not None:
            policy_allowed, reasons = self.policy.code_allowed(error.code)
            code_allowed = policy_allowed or error.code_declared_public
            if not code_allowed:
                code = (
                    "UNDECLARED_ERROR_CODE"
                    if "code_not_declared_public" in reasons
                    else "ERROR_CODE_NOT_VISIBLE_TO_CALLER"
                )
                finding = self.add_finding(
                    code,
                    f"error code {error.code!r} is not disclosable to caller {self.caller_name!r}",
                    exit_id=exit_id,
                    node_id=error.origin_node,
                    error_code=error.code,
                    route=tuple(),
                    details={"reasons": list(reasons)},
                )
                findings.append(finding)

        diagnostic_reports: list[DiagnosticReport] = []
        for use in error.diagnostics:
            allowed, reasons = self.policy.diagnostic_allowed(
                use.diagnostic_id, source=use.source
            )
            if not allowed:
                code = (
                    "UNDECLARED_DIAGNOSTIC"
                    if "not_declared" in reasons
                    else "DIAGNOSTIC_NOT_VISIBLE_TO_CALLER"
                )
                finding = self.add_finding(
                    code,
                    f"diagnostic {use.diagnostic_id!r} from {use.source!r} is not disclosable",
                    exit_id=exit_id,
                    node_id=use.origin_node,
                    diagnostic_id=use.diagnostic_id,
                    source=use.source,
                    route=use.route,
                    details={"reasons": list(reasons)},
                )
                findings.append(finding)
            diagnostic_reports.append(DiagnosticReport(use, allowed, tuple(reasons)))

        cause_report = self._report_error(error.cause, exit_id) if error.cause else None
        if cause_report is not None:
            findings.extend(cause_report.findings)
        member_reports = tuple(
            self._report_error(member, exit_id) for member in error.members
        )
        for member_report in member_reports:
            findings.extend(member_report.findings)

        return ErrorReport(
            code=error.code,
            code_allowed=code_allowed,
            diagnostics=tuple(diagnostic_reports),
            cause=cause_report,
            members=member_reports,
            aggregate=error.aggregate,
            origin_node=error.origin_node,
            findings=tuple(findings),
        )

    # ------------------------------------------------------------------
    # Field analysis
    # ------------------------------------------------------------------
    def _field_license(
        self,
        field_name: str,
        exit_id: str,
        permitted_fields: tuple[str, ...] | None,
        route: tuple[str, ...],
    ) -> tuple[bool, list[Finding]]:
        findings: list[Finding] = []
        licensed = True
        if permitted_fields is not None and field_name not in permitted_fields:
            licensed = False
            findings.append(
                self.add_finding(
                    "FIELD_NOT_PERMITTED",
                    f"field {field_name!r} is not in the response exit's licensed fields",
                    exit_id=exit_id,
                    field_name=field_name,
                    route=route,
                )
            )
        policy_ok, reasons = self.policy.field_allowed(field_name)
        if not policy_ok:
            licensed = False
            findings.append(
                self.add_finding(
                    "FIELD_NOT_AUTHORIZED_FOR_CALLER",
                    f"field {field_name!r} is not allowed for caller {self.caller_name!r}",
                    exit_id=exit_id,
                    field_name=field_name,
                    route=route,
                    details={"reasons": list(reasons)},
                )
            )
        return licensed, findings

    def _analyze_code_field(
        self,
        *,
        field_name: str,
        spec: Any,
        exit_id: str,
        route: tuple[str, ...],
        state: PropState,
        error_report: ErrorReport | None,
        permitted_fields: tuple[str, ...] | None,
    ) -> FieldReport:
        licensed, findings = self._field_license(
            field_name, exit_id, permitted_fields, route
        )

        actual_code: str | None
        if isinstance(spec, str) and spec in {"@code", "@error.code", "code"}:
            actual_code = state.error.code if state.error else error_report.code if error_report else None
        elif spec is None:
            actual_code = None
        else:
            actual_code = str(spec)

        policy_allowed, reasons = self.policy.code_allowed(actual_code)
        graph_allowed = (
            error_report is not None
            and actual_code == error_report.code
            and error_report.code_allowed
        )
        allowed = policy_allowed or graph_allowed
        if actual_code is not None and not allowed:
            # Reuse the full-tree code finding when this field emits the root
            # state code, avoiding duplicate violations.
            existing = None
            if error_report is not None and actual_code == error_report.code:
                existing = next(
                    (
                        item
                        for item in error_report.findings
                        if item.error_code == actual_code and item.field_name is None
                    ),
                    None,
                )
            if existing is not None:
                findings.append(existing)
            else:
                code = (
                    "UNDECLARED_ERROR_CODE"
                    if "code_not_declared_public" in reasons
                    else "ERROR_CODE_NOT_VISIBLE_TO_CALLER"
                )
                findings.append(
                    self.add_finding(
                        code,
                        f"response code {actual_code!r} is not disclosable to caller {self.caller_name!r}",
                        exit_id=exit_id,
                        field_name=field_name,
                        error_code=actual_code,
                        route=route,
                        details={"reasons": list(reasons)},
                    )
                )

        safe_value = actual_code if licensed and allowed else None
        return FieldReport(
            name=field_name,
            licensed=licensed and allowed,
            findings=tuple(findings),
            safe_value=safe_value,
        )

    def _analyze_field(
        self,
        *,
        field_name: str,
        spec: Any,
        exit_id: str,
        route: tuple[str, ...],
        state: PropState,
        error_report: ErrorReport | None,
        uses_by_id: dict[str, list[DiagnosticUse]],
        available_uses: tuple[DiagnosticUse, ...],
        permitted_fields: tuple[str, ...] | None,
    ) -> FieldReport:
        licensed, findings = self._field_license(
            field_name, exit_id, permitted_fields, route
        )

        collected_uses: list[DiagnosticUse] = []
        missing: list[str] = []
        literals: list[Any] = []
        special: str | None = None
        multi = isinstance(spec, (list, tuple, set)) or (
            isinstance(spec, Mapping) and ("sources" in spec or "refs" in spec)
        )

        def collect(value: Any, *, strict: bool = False) -> None:
            nonlocal special, multi
            if isinstance(value, Mapping):
                if "literal" in value or "value" in value and not any(
                    key in value for key in ("ref", "source", "id", "diagnostic_id")
                ):
                    literals.append(value.get("literal", value.get("value")))
                    return
                if "ref" in value or "source" in value or "diagnostic_id" in value or "id" in value:
                    ref = value.get("ref") or value.get("source") or value.get("diagnostic_id") or value.get("id")
                    if isinstance(ref, str) and ref.startswith("@"):
                        special = ref
                        return
                    uses, missing_ref = self._resolve_token(
                        {"id": ref, "source": value.get("source_name") or value.get("provenance")},
                        uses_by_id,
                        strict=True,
                    )
                    collected_uses.extend(uses)
                    if missing_ref:
                        missing.append(missing_ref)
                for key in ("sources", "refs", "diagnostics", "ids"):
                    if key in value:
                        multi = True
                        for item in value[key] if isinstance(value[key], list) else [value[key]]:
                            collect(item, strict=True)
                if "special" in value:
                    special = str(value["special"])
                return

            if isinstance(value, (list, tuple, set)):
                multi = True
                for item in value:
                    # Compact lists are source collections; literal arrays
                    # should be wrapped as {"literal": [...]}.
                    collect(item, strict=True)
                return

            if not isinstance(value, str):
                literals.append(value)
                return

            text = value.strip()
            if text.startswith("@"):
                special = text
                return
            for prefix in ("ref:", "diagnostic:", "source:", "id:"):
                if text.startswith(prefix):
                    token = text[len(prefix) :]
                    uses, missing_ref = self._resolve_token(token, uses_by_id, strict=True)
                    collected_uses.extend(uses)
                    if missing_ref:
                        missing.append(missing_ref)
                    return
            if text.startswith("$"):
                uses, missing_ref = self._resolve_token(text[1:], uses_by_id, strict=True)
                collected_uses.extend(uses)
                if missing_ref:
                    missing.append(missing_ref)
                return

            uses, missing_ref = self._resolve_token(
                text, uses_by_id, strict=strict or field_name in {"diagnostics", "locations"}
            )
            if uses:
                collected_uses.extend(uses)
            elif missing_ref:
                missing.append(missing_ref)
            else:
                literals.append(value)

        collect(spec)

        # Special references expand to all propagated diagnostics or structural
        # error projections.
        if special in {"@diagnostics", "@diags", "@all_diagnostics"}:
            collected_uses.extend(available_uses)
            multi = True
        elif special in {"@allowed_diagnostics"}:
            collected_uses.extend(
                use
                for use in available_uses
                if self.policy.diagnostic_allowed(use.diagnostic_id, source=use.source)[0]
            )
            multi = True

        unique_uses = tuple(dict.fromkeys(collected_uses))
        diagnostic_reports: list[DiagnosticReport] = []
        for use in unique_uses:
            allowed, reasons = self.policy.diagnostic_allowed(
                use.diagnostic_id, source=use.source
            )
            diagnostic_reports.append(DiagnosticReport(use, allowed, tuple(reasons)))
            if state.kind == "normal" and not allowed:
                # Error-state diagnostics were already reported by
                # _report_error; normal-response diagnostics are reported here.
                code = (
                    "UNDECLARED_DIAGNOSTIC"
                    if "not_declared" in reasons
                    else "DIAGNOSTIC_NOT_VISIBLE_TO_CALLER"
                )
                findings.append(
                    self.add_finding(
                        code,
                        f"normal response diagnostic {use.diagnostic_id!r} is not disclosable",
                        exit_id=exit_id,
                        field_name=field_name,
                        node_id=use.origin_node,
                        diagnostic_id=use.diagnostic_id,
                        source=use.source,
                        route=use.route,
                        details={"reasons": list(reasons)},
                    )
                )

        for missing_id in dict.fromkeys(missing):
            finding = self.add_finding(
                "DIAGNOSTIC_NOT_PROPAGATED",
                f"response field {field_name!r} references {missing_id!r}, but it is not actually propagated to this exit",
                exit_id=exit_id,
                field_name=field_name,
                diagnostic_id=missing_id,
                route=route,
            )
            findings.append(finding)

        safe_value = self._safe_field_value(
            spec=spec,
            special=special,
            multi=multi,
            literals=literals,
            diagnostic_reports=diagnostic_reports,
            error_report=error_report,
        )

        return FieldReport(
            name=field_name,
            licensed=licensed,
            diagnostics=tuple(diagnostic_reports),
            missing_sources=tuple(dict.fromkeys(missing)),
            findings=tuple(findings),
            safe_value=safe_value,
        )

    def _resolve_token(
        self,
        token: Any,
        uses_by_id: dict[str, list[DiagnosticUse]],
        *,
        strict: bool,
    ) -> tuple[list[DiagnosticUse], str | None]:
        source_filter: str | None = None
        if isinstance(token, Mapping):
            identifier = str(
                token.get("id")
                or token.get("diagnostic_id")
                or token.get("ref")
                or token.get("name")
            )
            source_filter = token.get("source") or token.get("source_name")
        else:
            identifier = str(token)

        matches = list(uses_by_id.get(identifier, ()))
        if source_filter is not None:
            matches = [use for use in matches if use.source == source_filter]
        if matches:
            return matches, None

        declared = identifier in self.policy.diagnostic_grants
        if strict or declared:
            return [], identifier
        # Undeclared and absent bare strings are treated as ordinary literals.
        return [], None

    def _safe_field_value(
        self,
        *,
        spec: Any,
        special: str | None,
        multi: bool,
        literals: list[Any],
        diagnostic_reports: tuple[DiagnosticReport, ...] | list[DiagnosticReport],
        error_report: ErrorReport | None,
    ) -> Any:
        allowed_ids = [item.id for item in diagnostic_reports if item.allowed]

        if special in {"@error", "@this", "@projection"} and error_report is not None:
            return error_report.safe_projection()
        if special in {"@cause"} and error_report is not None:
            return error_report.cause.safe_projection() if error_report.cause else None
        if special in {"@members", "@aggregate_members"} and error_report is not None:
            return [
                member.safe_projection()
                for member in error_report.members
                if member.admissible
            ]
        if special in {"@code"}:
            # Handled by code field; keep a conservative fallback.
            return error_report.code if error_report and error_report.code_allowed else None
        if special in {"@diagnostics", "@diags", "@all_diagnostics", "@allowed_diagnostics"}:
            return allowed_ids

        # Explicit source collections retain the allowed IDs.  Single source
        # references are projected as one scalar.
        if diagnostic_reports:
            if multi:
                return allowed_ids
            return allowed_ids[0] if allowed_ids else None

        if literals:
            return literals[0] if len(literals) == 1 and not multi else literals
        if isinstance(spec, (list, tuple, set)):
            return []
        return None


def _collect_exit_diagnostic_uses(exit_report: ExitReport) -> list[DiagnosticUse]:
    uses: list[DiagnosticUse] = []

    def from_error_report(report: ErrorReport | None) -> None:
        if report is None:
            return
        for diag_report in report.diagnostics:
            uses.append(diag_report.diagnostic)
        from_error_report(report.cause)
        for member in report.members:
            from_error_report(member)

    for scenario in exit_report.scenarios:
        from_error_report(scenario.error)
        for field_report in scenario.fields:
            for diag_report in field_report.diagnostics:
                uses.append(diag_report.diagnostic)
    return list(dict.fromkeys(uses))
