"""Ticket trust-chain validation."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .model import ValidationResult
from .normalization import (
    canonical_domain,
    coerce_ticket,
    normalize_hop_graph,
    normalize_parent_graph,
    normalize_review_requirements,
)

OK = "ok"
INVALID_TICKET = "invalid_ticket"
ORIGIN_MISMATCH = "origin_mismatch"
TARGET_MISMATCH = "target_mismatch"
UNTRUSTED_HOP = "untrusted_hop"
TICKET_CYCLE = "ticket_cycle"
PARENT_CYCLE = "parent_cycle"
MISSING_PARENT_EDGE = "missing_parent_edge"
AMBIGUOUS_SEGMENT = "ambiguous_segment"
INVALID_SEGMENT = "invalid_segment"
REVIEW_MISSING = "required_review_missing"


def _can_reach_end(start: str, end: str, graph: Mapping[str, set[str]]) -> bool:
    """Return whether ``end`` is reachable from ``start`` via parent edges."""

    if start == end:
        return True
    seen = {start}
    stack = [start]
    while stack:
        current = stack.pop()
        for parent in graph.get(current, set()):
            if parent == end:
                return True
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return False


class TrustValidator:
    """Validate verified ticket summaries against local trust policy.

    Parameters
    ----------
    issued_hops:
        Mapping of ``issuer_domain -> set/dict of next hops it is responsible
        for issuing``.  A direct trusted edge elsewhere in this graph is never
        used to rewrite or shorten the ticket path.
    parents:
        Child-to-parent domain edges used only to expand compressed segments.
        These edges do not themselves imply trust.
    review_requirements:
        Source-domain to required review-domain policy.
    normalize_domains:
        Canonicalize case and trailing dots before comparison.
    reject_ticket_cycles:
        Reject repeated domains in the expanded ticket route.  Parent-chain
        cycles are always rejected, even if this is false.
    review_source_scope:
        ``"origin"`` checks only the ticket's start domain.  ``"any_domain"``
        applies a policy to the first occurrence of that source anywhere in
        the expanded route.
    """

    def __init__(
        self,
        issued_hops: Mapping[str, Any] | Iterable[Any] | None = None,
        parents: Mapping[str, Any] | Iterable[Any] | None = None,
        review_requirements: Mapping[str, Any] | Iterable[Any] | None = None,
        *,
        trusted_next_hops: Mapping[str, Any] | Iterable[Any] | None = None,
        trust_edges: Iterable[Any] | None = None,
        trust_relations: Mapping[str, Any] | Iterable[Any] | None = None,
        signed_hops: Iterable[Any] | None = None,
        parent_edges: Iterable[Any] | None = None,
        parent_map: Mapping[str, Any] | Iterable[Any] | None = None,
        required_reviews: Mapping[str, Any] | Iterable[Any] | None = None,
        audit_requirements: Mapping[str, Any] | Iterable[Any] | None = None,
        normalize_domains: bool = True,
        reject_ticket_cycles: bool = True,
        review_source_scope: str = "origin",
    ) -> None:
        self.normalize_domains = normalize_domains
        self.reject_ticket_cycles = reject_ticket_cycles
        if review_source_scope not in {"origin", "any_domain"}:
            raise ValueError("review_source_scope must be 'origin' or 'any_domain'")
        self.review_source_scope = review_source_scope

        hop_aliases: dict[str, Any] = {}
        for alias_name, alias_value in (
            ("trusted_next_hops", trusted_next_hops),
            ("trust_edges", trust_edges),
            ("trust_relations", trust_relations),
            ("signed_hops", signed_hops),
        ):
            if alias_value is not None:
                hop_aliases[alias_name] = alias_value
        self.issued_hops = normalize_hop_graph(
            issued_hops, normalize=normalize_domains, **hop_aliases
        )

        parent_aliases: dict[str, Any] = {}
        for alias_name, alias_value in (
            ("parent_edges", parent_edges),
            ("parent_map", parent_map),
        ):
            if alias_value is not None:
                parent_aliases[alias_name] = alias_value
        self.parents = normalize_parent_graph(
            parents, normalize=normalize_domains, **parent_aliases
        )
        review_policy = review_requirements
        if review_policy is None:
            review_policy = required_reviews
        if review_policy is None:
            review_policy = audit_requirements
        self.review_requirements = normalize_review_requirements(
            review_policy,
            normalize=normalize_domains,
        )

    def validate(self, ticket: Any) -> ValidationResult:
        """Validate one ticket and return a structured result."""

        try:
            origin, target, transit, path = coerce_ticket(
                ticket, normalize=self.normalize_domains
            )
        except Exception as exc:  # service boundary: report unusable claims
            return ValidationResult(
                allowed=False,
                code=INVALID_TICKET,
                reason=str(exc),
            )

        route: list[str] = []
        seen: set[str] = set()

        def failure(
            code: str,
            reason: str,
            *,
            position: int | None = None,
            domain: str | None = None,
            **kwargs: Any,
        ) -> ValidationResult:
            return ValidationResult(
                allowed=False,
                code=code,
                reason=reason,
                expanded_path=tuple(route),
                position=position,
                domain=domain,
                **kwargs,
            )

        def initialize(domain: str) -> None:
            route.append(domain)
            seen.add(domain)

        def admit(
            next_domain: str,
            *,
            item_index: int | None,
            cycle_code: str,
        ) -> ValidationResult | None:
            current = route[-1]
            edge_index = len(route) - 1

            if next_domain not in self.issued_hops.get(current, set()):
                return failure(
                    UNTRUSTED_HOP,
                    f"domain {current!r} has not issued/authorized next hop {next_domain!r}",
                    position=edge_index,
                    domain=current,
                    next_domain=next_domain,
                    edge_index=edge_index,
                    item_index=item_index,
                )

            if next_domain in seen:
                first_position = route.index(next_domain)
                is_parent_cycle = cycle_code == PARENT_CYCLE
                if is_parent_cycle or self.reject_ticket_cycles:
                    return failure(
                        cycle_code,
                        (
                            f"parent chain loops back to {next_domain!r}; "
                            "compressed segment cannot be a unique ascending chain"
                            if is_parent_cycle
                            else f"ticket route contains a cycle at {next_domain!r}"
                        ),
                        position=edge_index,
                        domain=current,
                        next_domain=next_domain,
                        edge_index=edge_index,
                        item_index=item_index,
                        cycle_domain=next_domain,
                        cycle_start_position=first_position,
                    )

            route.append(next_domain)
            seen.add(next_domain)
            return None

        def process_domain(domain: str, item_index: int | None) -> ValidationResult | None:
            if not route:
                initialize(domain)
                if origin is not None and domain != origin:
                    return failure(
                        ORIGIN_MISMATCH,
                        f"path starts at {domain!r}, but ticket origin is {origin!r}",
                        position=0,
                        domain=domain,
                        item_index=item_index,
                        details={"expected_origin": origin},
                    )
                return None
            return admit(domain, item_index=item_index, cycle_code=TICKET_CYCLE)

        def process_segment(segment: Any, item_index: int | None) -> ValidationResult | None:
            seg_start = segment.start
            seg_end = segment.end

            if not route:
                if seg_start is None:
                    return failure(
                        INVALID_SEGMENT,
                        "the first route item is a compressed segment without a start domain",
                        item_index=item_index,
                    )
                initialize(seg_start)
                if origin is not None and seg_start != origin:
                    return failure(
                        ORIGIN_MISMATCH,
                        f"segment starts at {seg_start!r}, but ticket origin is {origin!r}",
                        position=0,
                        domain=seg_start,
                        item_index=item_index,
                        details={"expected_origin": origin},
                    )
            elif seg_start is None:
                seg_start = route[-1]
            elif seg_start != route[-1]:
                # The explicit segment start is the next claimed domain after
                # the preceding route item.  That boundary hop is checked like
                # any other ticket hop before parent expansion begins.
                boundary_failure = admit(
                    seg_start, item_index=item_index, cycle_code=TICKET_CYCLE
                )
                if boundary_failure is not None:
                    return boundary_failure

            assert seg_start is not None and seg_end is not None

            if route[-1] != seg_start:
                # Defensive; admit above either appends or returns a failure.
                return failure(
                    INVALID_SEGMENT,
                    "compressed segment is not adjacent to its declared start",
                    item_index=item_index,
                    domain=route[-1] if route else None,
                )

            current = seg_start
            while current != seg_end:
                candidates = self.parents.get(current, set())
                if not candidates:
                    return failure(
                        MISSING_PARENT_EDGE,
                        f"no immediate parent edge for {current!r} while restoring "
                        f"segment to {seg_end!r}",
                        position=len(route) - 1,
                        domain=current,
                        item_index=item_index,
                        details={"segment_end": seg_end},
                    )

                next_parent: str
                if len(candidates) == 1:
                    next_parent = next(iter(candidates))
                else:
                    # A domain may contain unrelated extra parent edges.  The
                    # compressed segment is ambiguous only if more than one
                    # immediate candidate can actually restore the declared end.
                    viable = {
                        candidate
                        for candidate in candidates
                        if _can_reach_end(candidate, seg_end, self.parents)
                    }
                    if len(viable) > 1:
                        return failure(
                            AMBIGUOUS_SEGMENT,
                            f"domain {current!r} has multiple parent routes to "
                            f"{seg_end!r}: {sorted(viable)!r}; segment cannot be uniquely restored",
                            position=len(route) - 1,
                            domain=current,
                            item_index=item_index,
                            details={"parent_candidates": sorted(viable)},
                        )
                    if len(viable) == 1:
                        next_parent = next(iter(viable))
                    else:
                        # No candidate reaches the endpoint.  Choose a stable
                        # candidate so the normal walk reports the first actual
                        # missing edge or cycle further along.
                        next_parent = min(candidates)

                step_failure = admit(
                    next_parent, item_index=item_index, cycle_code=PARENT_CYCLE
                )
                if step_failure is not None:
                    return step_failure
                current = next_parent

            return None

        # Build the ordered work list.  Path mode treats path as the complete
        # route.  Ticket mode keeps origin/transit/target as separate claims.
        if path is not None:
            for item_index, item in enumerate(path):
                if isinstance(item, str):
                    err = process_domain(item, item_index)
                else:
                    err = process_segment(item, item_index)
                if err is not None:
                    return err

            if not route:
                return ValidationResult(
                    allowed=False,
                    code=INVALID_TICKET,
                    reason="ticket path is empty",
                )

            if target is not None and route[-1] != target:
                return failure(
                    TARGET_MISMATCH,
                    f"path ends at {route[-1]!r}, but ticket target is {target!r}",
                    position=len(route) - 1,
                    domain=route[-1],
                    target=target,
                    target_position=len(route) - 1,
                    details={"expected_target": target},
                )
        else:
            assert origin is not None and target is not None
            initialize(origin)
            for item_index, item in enumerate(transit):
                if isinstance(item, str):
                    err = process_domain(item, item_index)
                else:
                    err = process_segment(item, item_index)
                if err is not None:
                    return err

            if not route:
                return ValidationResult(
                    allowed=False,
                    code=INVALID_TICKET,
                    reason="ticket route is empty",
                )

            # A zero-edge origin == target call is valid without requiring a
            # self-issued hop.  If target is elsewhere, admit it normally; a
            # return to an already-seen domain is caught as a cycle.
            if route[-1] != target:
                err = admit(target, item_index=None, cycle_code=TICKET_CYCLE)
                if err is not None:
                    return err

        return self._check_reviews(route)

    def _check_reviews(self, route: list[str]) -> ValidationResult:
        actual_origin = route[0]
        actual_target = route[-1]
        route_set = set(route)
        missing: list[tuple[int, str, str, Any]] = []

        for requirement in self.review_requirements:
            if (
                requirement.targets is not None
                and actual_target not in requirement.targets
            ):
                continue

            if self.review_source_scope == "origin":
                if requirement.source != actual_origin:
                    continue
                source_positions = [0]
            else:
                source_positions = [i for i, domain in enumerate(route) if domain == requirement.source]
                if not source_positions:
                    continue

            source_position = source_positions[0]
            if requirement.reviewer in route_set:
                continue

            missing.append(
                (source_position, requirement.source, requirement.reviewer, requirement)
            )

        if missing:
            source_position, source, reviewer, requirement = min(
                missing, key=lambda item: (item[0], item[1], item[2])
            )
            target_position = len(route) - 1
            return ValidationResult(
                allowed=False,
                code=REVIEW_MISSING,
                reason=(
                    f"source {source!r} is required to pass review domain "
                    f"{reviewer!r}, but that domain is absent from the ticket path"
                ),
                expanded_path=tuple(route),
                position=source_position,
                domain=source,
                source=source,
                source_position=source_position,
                required_reviewer=reviewer,
                target=actual_target,
                target_position=target_position,
                edge_index=None,
                details={
                    "enforcement_position": target_position,
                    "targets": (
                        sorted(requirement.targets)
                        if requirement.targets is not None
                        else None
                    ),
                },
            )

        return ValidationResult(
            allowed=True,
            code=OK,
            reason="all ticket hops are issued and all required reviews are present",
            expanded_path=tuple(route),
            position=len(route) - 1,
            domain=actual_target,
            target=actual_target,
            target_position=len(route) - 1,
            source=actual_origin,
            source_position=0,
        )


def validate_ticket(
    ticket: Any,
    issued_hops: Mapping[str, Any] | Iterable[Any] | None = None,
    parents: Mapping[str, Any] | Iterable[Any] | None = None,
    review_requirements: Mapping[str, Any] | Iterable[Any] | None = None,
    **kwargs: Any,
) -> ValidationResult:
    """Functional convenience wrapper around :class:`TrustValidator`."""

    validator = TrustValidator(
        issued_hops=issued_hops,
        parents=parents,
        review_requirements=review_requirements,
        **kwargs,
    )
    return validator.validate(ticket)


def first_failure_position(*args: Any, **kwargs: Any) -> int | None:
    """Return only the expanded-path position of the first failure."""

    result = validate_ticket(*args, **kwargs)
    return None if result.allowed else result.position


def verify_ticket(*args: Any, **kwargs: Any) -> tuple[bool, int | None, str]:
    """Return the compact tuple ``(allowed, position, code)``."""

    result = validate_ticket(*args, **kwargs)
    position = None if result.allowed else result.position
    return result.allowed, position, result.code


# Common descriptive aliases.
validate_trust_chain = validate_ticket
validate_chain = validate_ticket
validate = validate_ticket
check_ticket = validate_ticket
check_trust_chain = validate_ticket
TrustChainValidator = TrustValidator
