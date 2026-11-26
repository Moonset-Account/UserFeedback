"""Interactive audit session and one-shot audit helper."""

from __future__ import annotations

from dataclasses import replace
from itertools import combinations
from typing import Any, Iterable, Mapping

from .graph import make_type_predicate, validate_bridges, validate_topology
from .matcher import MatcherConfig, enumerate_matches
from .model import (
    SOURCE_CONFIRMED,
    SOURCE_FROZEN,
    Anchor,
    BridgeConstraint,
    Topology,
    anchors_from_mapping,
)
from .report import AnchorFeedback, AuditReport, Match, summarize_matches


def minimal_anchor_conflict_sets(
    session: "AuditSession",
    anchors: Iterable[Anchor],
) -> tuple[tuple[str, ...], ...]:
    """Find inclusion-minimal anchor subsets with no common legal match."""

    anchor_tuple = tuple(anchors)
    if not session.baseline_matches:
        return ()
    support_sets: list[frozenset[int]] = []
    all_indices = frozenset(range(len(session.baseline_matches)))
    for anchor in anchor_tuple:
        supported = session.anchor_supported(anchor, session.baseline_matches)
        indices = frozenset(
            i for i, match in enumerate(session.baseline_matches) if match in supported
        )
        # ``match in supported`` relies on frozen Match equality; build directly
        # from mapping filtering if that proves too clever for callers.
        if not indices:
            indices = frozenset()
        support_sets.append(indices)

    found: list[frozenset[int]] = []
    for size in range(1, len(anchor_tuple) + 1):
        for subset in combinations(range(len(anchor_tuple)), size):
            subset_set = frozenset(subset)
            if any(previous <= subset_set for previous in found):
                continue
            common = set(all_indices)
            for index in subset:
                common &= support_sets[index]
            if not common:
                found.append(subset_set)

    labels = [
        tuple(
            sorted(
                f"{anchor_tuple[i].logical}->{anchor_tuple[i].observed}"
                for i in subset
            )
        )
        for subset in found
    ]
    return tuple(sorted(labels))


class AuditSession:
    """A monotonic session: accepted confirmations only remove candidates."""

    def __init__(
        self,
        logical: Topology | Mapping[str, Any],
        observed: Topology | Mapping[str, Any],
        *,
        bridges: Iterable[BridgeConstraint | Mapping[str, Any]] = (),
        exclusive_locations: Iterable[str] = (),
        anonymous_types: Iterable[str] = (),
        type_families: Mapping[str, str] | None = None,
        solution_limit: int | None = None,
        _baseline: tuple[Match, ...] | None = None,
        _baseline_complete: bool | None = None,
        _accepted_anchors: tuple[Anchor, ...] = (),
        _feedback: tuple[AnchorFeedback, ...] = (),
        _conflict_sets: tuple[tuple[str, ...], ...] = (),
        _report: AuditReport | None = None,
    ) -> None:
        self.logical = Topology.from_dict(logical)
        self.observed = Topology.from_dict(observed)
        self.bridges = tuple(BridgeConstraint.from_dict(b) for b in bridges)
        self.exclusive_locations = frozenset(exclusive_locations)
        self.anonymous_types = frozenset(anonymous_types)
        self.type_families = dict(type_families or {})
        self.solution_limit = solution_limit

        validate_topology(self.logical, name="logical topology")
        validate_topology(self.observed, name="observed topology")
        validate_bridges(self.logical, self.bridges)

        self.type_compatible = make_type_predicate(
            self.anonymous_types, self.type_families
        )
        self.config = MatcherConfig(
            exclusive_locations=self.exclusive_locations,
            type_compatible=self.type_compatible,
            solution_limit=solution_limit,
        )
        self.logical_ids = tuple(self.logical.device_ids())

        if _baseline is None:
            baseline, truncated = enumerate_matches(
                self.logical,
                self.observed,
                bridges=self.bridges,
                config=self.config,
            )
            self.baseline_matches: tuple[Match, ...] = tuple(baseline)
            self.complete: bool = not truncated
        else:
            self.baseline_matches = tuple(_baseline)
            self.complete = True if _baseline_complete is None else _baseline_complete

        self.accepted_anchors = tuple(_accepted_anchors)
        self.anchor_feedback = tuple(_feedback)
        self.conflict_sets = tuple(_conflict_sets)
        self.report = _report or self._build_report(
            self.baseline_matches,
            feedback=self.anchor_feedback,
        )

    @classmethod
    def create(
        cls,
        logical: Topology | Mapping[str, Any],
        observed: Topology | Mapping[str, Any],
        *,
        bridges: Iterable[BridgeConstraint | Mapping[str, Any]] = (),
        frozen: Mapping[str, Any] | Iterable[Any] | None = None,
        exclusive_locations: Iterable[str] = (),
        forbidden_share_locations: Iterable[str] | None = None,
        anonymous_types: Iterable[str] = (),
        type_families: Mapping[str, str] | None = None,
        solution_limit: int | None = None,
    ) -> "AuditSession":
        exclusions = set(exclusive_locations)
        if forbidden_share_locations:
            exclusions.update(forbidden_share_locations)
        session = cls(
            logical,
            observed,
            bridges=bridges,
            exclusive_locations=exclusions,
            anonymous_types=anonymous_types,
            type_families=type_families,
            solution_limit=solution_limit,
        )
        anchors = anchors_from_mapping(frozen, source=SOURCE_FROZEN)
        # Frozen data has no arrival order in a mapping; sort for deterministic
        # conflict resolution. Interactive confirmations retain arrival order.
        for anchor in sorted(anchors, key=lambda a: (a.logical, a.observed, a.location or "")):
            result = session.confirm_anchor(anchor)
            if result.accepted and result.session is not None:
                session = result.session
            else:
                feedback = result.feedback
                session.anchor_feedback = session.anchor_feedback + (feedback,)
                session.report = session._build_report(
                    session.report.matches,
                    feedback=session.anchor_feedback,
                )
        session.conflict_sets = minimal_anchor_conflict_sets(session, anchors)
        session.report = replace(
            session._build_report(
                session.report.matches,
                feedback=session.anchor_feedback,
            ),
            conflict_sets=session.conflict_sets,
        )
        return session

    def _build_report(
        self,
        matches: Iterable[Match],
        *,
        feedback: tuple[AnchorFeedback, ...] | None = None,
        accepted: tuple[Anchor, ...] | None = None,
    ) -> AuditReport:
        match_tuple = tuple(matches)
        certain, candidates, ambiguous = summarize_matches(match_tuple, self.logical_ids)
        base_certain, base_candidates = summarize_matches(
            self.baseline_matches, self.logical_ids
        )[:2]
        return AuditReport(
            feasible=bool(match_tuple),
            complete=self.complete,
            matches=match_tuple,
            mapping=certain,
            candidates=candidates,
            ambiguous=ambiguous,
            baseline_mapping=base_certain,
            baseline_candidates=base_candidates,
            baseline_matches=self.baseline_matches,
            accepted_anchors=self.accepted_anchors if accepted is None else accepted,
            anchor_feedback=self.anchor_feedback if feedback is None else feedback,
            conflict_sets=self.conflict_sets,
            logical_device_ids=self.logical_ids,
            solution_limit=self.solution_limit,
        )

    def _anchor_error(self, anchor: Anchor) -> str | None:
        if anchor.logical not in self.logical.device_map():
            return f"unknown logical device {anchor.logical!r}"
        if anchor.observed not in self.observed.device_map():
            return f"unknown observed device {anchor.observed!r}"
        if anchor.location is not None:
            actual = self.observed.device_map()[anchor.observed].location
            if actual != anchor.location:
                return (
                    f"observed location {actual!r} does not match anchor "
                    f"location {anchor.location!r}"
                )
        return None

    def anchor_supported(self, anchor: Anchor, matches: Iterable[Match]) -> list[Match]:
        error = self._anchor_error(anchor)
        if error:
            return []
        obs_device = self.observed.device_map()[anchor.observed]
        result = []
        for match in matches:
            mapped = match.mapping_dict().get(anchor.logical)
            if mapped != anchor.observed:
                continue
            if anchor.location is not None and obs_device.location != anchor.location:
                continue
            result.append(match)
        return result

    def confirm(
        self,
        logical: str,
        observed: str,
        *,
        location: str | None = None,
        note: str | None = None,
        source: str = SOURCE_CONFIRMED,
    ) -> "ConfirmationResult":
        anchor = Anchor(
            logical=logical,
            observed=observed,
            location=location,
            source=source,
            note=note,
        )
        return self.confirm_anchor(anchor)

    def confirm_anchor(self, anchor: Anchor) -> "ConfirmationResult":
        error = self._anchor_error(anchor)
        baseline_support = self.anchor_supported(anchor, self.baseline_matches)
        if error is not None or not baseline_support:
            reason = error or "anchor is absent from every topology-legal match"
            feedback = AnchorFeedback(
                anchor=anchor,
                status="unsatisfiable",
                support_count=len(baseline_support),
                reason=reason,
            )
            rejection_report = replace(
                self.report,
                anchor_feedback=self.report.anchor_feedback + (feedback,),
            )
            return ConfirmationResult(
                accepted=False,
                session=self,
                report=rejection_report,
                feedback=feedback,
            )

        supported = self.anchor_supported(anchor, self.report.matches)
        if not supported:
            conflicts = tuple(
                sorted({f"{a.logical}->{a.observed}" for a in self.accepted_anchors})
            )
            feedback = AnchorFeedback(
                anchor=anchor,
                status="rejected",
                support_count=0,
                reason="anchor is possible alone but conflicts with accepted anchors",
                conflicts_with=conflicts,
            )
            rejection_report = replace(
                self.report,
                anchor_feedback=self.report.anchor_feedback + (feedback,),
            )
            return ConfirmationResult(
                accepted=False,
                session=self,
                report=rejection_report,
                feedback=feedback,
            )

        accepted = self.accepted_anchors + (anchor,)
        feedback = self.anchor_feedback + (
            AnchorFeedback(
                anchor=anchor,
                status="accepted",
                support_count=len(supported),
                reason=None,
            ),
        )
        new_session = AuditSession(
            self.logical,
            self.observed,
            bridges=self.bridges,
            exclusive_locations=self.exclusive_locations,
            anonymous_types=self.anonymous_types,
            type_families=self.type_families,
            solution_limit=self.solution_limit,
            _baseline=self.baseline_matches,
            _baseline_complete=self.complete,
            _accepted_anchors=accepted,
            _feedback=feedback,
            _conflict_sets=self.conflict_sets,
        )
        new_session.report = new_session._build_report(
            supported,
            feedback=feedback,
            accepted=accepted,
        )
        return ConfirmationResult(
            accepted=True,
            session=new_session,
            report=new_session.report,
            feedback=feedback[-1],
        )


class ConfirmationResult:
    def __init__(
        self,
        *,
        accepted: bool,
        session: AuditSession,
        report: AuditReport,
        feedback: AnchorFeedback,
    ) -> None:
        self.accepted = accepted
        self.session = session
        self.report = report
        self.feedback = feedback

    def __bool__(self) -> bool:
        return self.accepted


def audit_topology(
    logical: Topology | Mapping[str, Any],
    observed: Topology | Mapping[str, Any],
    *,
    bridges: Iterable[BridgeConstraint | Mapping[str, Any]] = (),
    frozen: Mapping[str, Any] | Iterable[Any] | None = None,
    confirmations: Mapping[str, Any] | Iterable[Any] | None = None,
    exclusive_locations: Iterable[str] = (),
    forbidden_share_locations: Iterable[str] | None = None,
    anonymous_types: Iterable[str] = (),
    type_families: Mapping[str, str] | None = None,
    solution_limit: int | None = None,
) -> AuditReport:
    """One-shot audit. Confirmations are replayed after frozen anchors."""

    session = AuditSession.create(
        logical,
        observed,
        bridges=bridges,
        frozen=frozen,
        exclusive_locations=exclusive_locations,
        forbidden_share_locations=forbidden_share_locations,
        anonymous_types=anonymous_types,
        type_families=type_families,
        solution_limit=solution_limit,
    )
    anchors = anchors_from_mapping(confirmations, source=SOURCE_CONFIRMED)
    current = session
    for anchor in anchors:
        result = current.confirm_anchor(anchor)
        if result.accepted:
            current = result.session
        else:
            # One-shot helper records the rejection but preserves the prior
            # monotonic candidate set.
            current = result.session
            current.report = replace(
                current.report,
                anchor_feedback=current.report.anchor_feedback + (result.feedback,),
            )
    return current.report
