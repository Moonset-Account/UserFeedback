"""Retention adjudication engine.

The engine is a pure, side-effect-free registry: register the archived
materials, the preservation notices (including late-arriving corrections) and
the derived artifacts, then ask for a decision *as of* any date.  Because the
answer only depends on the registered data and the requested date, historical
replay uses exactly the notices that were effective on the replayed day; a
correction registered later naturally alters replays whose ``as_of`` date is on
or after the correction's own effective date.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, List, Set, Tuple

from .models import (
    ArtifactDecision,
    DerivedArtifact,
    FieldDecision,
    Material,
    PreservationNotice,
    ReferenceLink,
    RetentionDecision,
    RetentionStatus,
)


class RetentionEngine:
    """Holds the archive inventory and answers retention questions."""

    def __init__(self) -> None:
        self._materials: Dict[str, Material] = {}
        self._notices: List[PreservationNotice] = []
        self._artifacts: List[DerivedArtifact] = []
        self._artifact_ids: Set[str] = set()
        self._notice_ids: Set[str] = set()

    # ------------------------------------------------------------------ registry
    def register_material(self, material: Material) -> None:
        """Register (or replace) one archived record."""
        if material.material_id in self._materials:
            raise ValueError(
                f"material {material.material_id!r} is already registered"
            )
        self._materials[material.material_id] = material

    def register_notice(self, notice: PreservationNotice) -> None:
        """Register a preservation notice or a later correction.

        Registration order is irrelevant to adjudication: each notice is
        selected purely by its own effective interval when a decision is
        computed.
        """
        if notice.notice_id in self._notice_ids:
            raise ValueError(
                f"notice {notice.notice_id!r} is already registered"
            )
        material = self._materials.get(notice.material_id)
        if material is None:
            raise ValueError(
                f"notice {notice.notice_id!r} references unknown material "
                f"{notice.material_id!r}"
            )
        unknown = notice.fields - material.fields
        if unknown:
            raise ValueError(
                f"notice {notice.notice_id!r} covers fields not present on "
                f"material {notice.material_id!r}: {sorted(unknown)}"
            )
        if notice.amends_notice_id is not None:
            target = next(
                (
                    candidate
                    for candidate in self._notices
                    if candidate.notice_id == notice.amends_notice_id
                ),
                None,
            )
            if target is None:
                raise ValueError(
                    f"notice {notice.notice_id!r} amends unknown notice "
                    f"{notice.amends_notice_id!r}; register the original "
                    f"notice first"
                )
            if target.material_id != notice.material_id:
                raise ValueError(
                    f"notice {notice.notice_id!r} amends notice "
                    f"{notice.amends_notice_id!r} on a different material"
                )
        self._notices.append(notice)
        self._notice_ids.add(notice.notice_id)

    def register_artifact(self, artifact: DerivedArtifact) -> None:
        """Register one derived piece and validate its field references."""
        if artifact.artifact_id in self._artifact_ids:
            raise ValueError(
                f"artifact {artifact.artifact_id!r} is already registered"
            )
        material = self._materials.get(artifact.material_id)
        if material is None:
            raise ValueError(
                f"artifact {artifact.artifact_id!r} references unknown "
                f"material {artifact.material_id!r}"
            )
        unknown = artifact.referenced_fields - material.fields
        if unknown:
            raise ValueError(
                f"artifact {artifact.artifact_id!r} references fields not "
                f"present on material {artifact.material_id!r}: "
                f"{sorted(unknown)}"
            )
        self._artifacts.append(artifact)
        self._artifact_ids.add(artifact.artifact_id)

    # --------------------------------------------------------------- adjudication
    def decide(self, material_id: str, as_of: date) -> RetentionDecision:
        """Adjudicate one material as of a given calendar day.

        Only notices whose effective interval contains ``as_of`` influence the
        answer, so a decision for a past date reproduces the view that was
        valid then -- except that a correction with an effective date on or
        before that day does apply, matching the rule that later-arriving
        corrections take effect on their own stated date.
        """
        material = self._materials.get(material_id)
        if material is None:
            raise KeyError(f"unknown material {material_id!r}")
        if not isinstance(as_of, date):
            raise TypeError("as_of must be a datetime.date")

        field_decisions = self._decide_fields(material, as_of)
        field_status_by_material: Dict[Tuple[str, str], RetentionStatus] = {}
        field_notices_by_material: Dict[Tuple[str, str], Tuple[str, ...]] = {}
        for decision in field_decisions:
            key = (material_id, decision.field_name)
            field_status_by_material[key] = decision.status
            field_notices_by_material[key] = decision.active_notice_ids

        artifacts = [
            artifact
            for artifact in self._artifacts
            if artifact.material_id == material_id
        ]
        artifact_decisions = tuple(
            self._decide_artifact(
                artifact,
                field_status_by_material,
                field_notices_by_material,
            )
            for artifact in sorted(artifacts, key=lambda a: a.artifact_id)
        )

        controlled_fields = tuple(
            d.field_name
            for d in field_decisions
            if d.status is RetentionStatus.PRESERVED
        )
        withdrawn_fields = tuple(
            d.field_name
            for d in field_decisions
            if d.status is not RetentionStatus.NORMAL
        )
        normal_fields = tuple(
            d.field_name
            for d in field_decisions
            if d.status is RetentionStatus.NORMAL
        )
        controlled_artifacts = tuple(
            d.artifact_id
            for d in artifact_decisions
            if d.status is RetentionStatus.PRESERVED
        )
        withdrawn_artifacts = tuple(
            d.artifact_id
            for d in artifact_decisions
            if d.status is not RetentionStatus.NORMAL
        )
        normal_artifacts = tuple(
            d.artifact_id
            for d in artifact_decisions
            if d.status is RetentionStatus.NORMAL
        )
        reference_links = tuple(
            link
            for decision in artifact_decisions
            for link in decision.references
        )

        return RetentionDecision(
            material_id=material_id,
            as_of=as_of,
            normal_expiry=material.normal_expiry,
            field_decisions=tuple(field_decisions),
            artifact_decisions=artifact_decisions,
            controlled_view_fields=controlled_fields,
            withdrawn_fields=withdrawn_fields,
            normal_view_fields=normal_fields,
            controlled_view_artifacts=controlled_artifacts,
            withdrawn_artifacts=withdrawn_artifacts,
            normal_view_artifacts=normal_artifacts,
            reference_links=reference_links,
        )

    # ------------------------------------------------------------------- helpers
    def _effective_notices(
        self, material_id: str, as_of: date
    ) -> Tuple[PreservationNotice, ...]:
        """Notices actually in force for a material on the given day.

        A correction (a notice carrying ``amends_notice_id``) that is itself
        effective on the day supersedes the notice it amends: the amended
        notice is dropped from the effective set for that day.  Corrections
        whose effective date has not arrived leave the original notice in
        force, which is what makes historical replay faithful.
        """
        scoped = [
            notice
            for notice in self._notices
            if notice.material_id == material_id
            and notice.is_effective_on(as_of)
        ]
        superseded = {
            notice.amends_notice_id
            for notice in scoped
            if notice.amends_notice_id is not None
        }
        return tuple(
            notice
            for notice in scoped
            if notice.notice_id not in superseded
        )

    def _decide_fields(
        self, material: Material, as_of: date
    ) -> Tuple[FieldDecision, ...]:
        expired = as_of >= material.normal_expiry
        effective = self._effective_notices(material.material_id, as_of)
        decisions: List[FieldDecision] = []
        for field_name in sorted(material.fields):
            active = tuple(
                sorted(
                    notice.notice_id
                    for notice in effective
                    if field_name in notice.fields
                )
            )
            if active:
                # A hold keeps the column alive in the controlled view even
                # after the ordinary period ends; columns not named by the
                # notice keep ageing on the original schedule.
                status = RetentionStatus.PRESERVED
            elif expired:
                status = RetentionStatus.EXPIRED
            else:
                status = RetentionStatus.NORMAL
            decisions.append(
                FieldDecision(
                    field_name=field_name,
                    status=status,
                    active_notice_ids=active,
                    normal_expiry=material.normal_expiry,
                )
            )
        return tuple(decisions)

    def _decide_artifact(
        self,
        artifact: DerivedArtifact,
        field_status_by_material: Dict[Tuple[str, str], RetentionStatus],
        field_notices_by_material: Dict[Tuple[str, str], Tuple[str, ...]],
    ) -> ArtifactDecision:
        references: List[ReferenceLink] = []
        holding_notices: Set[str] = set()
        saw_expired = False
        saw_normal = False

        for field_name in sorted(artifact.referenced_fields):
            key = (artifact.material_id, field_name)
            status = field_status_by_material[key]
            if status is RetentionStatus.PRESERVED:
                holding_notices.update(field_notices_by_material.get(key, ()))
            elif status is RetentionStatus.EXPIRED:
                saw_expired = True
            else:
                saw_normal = True
            references.append(
                ReferenceLink(
                    artifact_id=artifact.artifact_id,
                    material_id=artifact.material_id,
                    field_name=field_name,
                    field_status=status,
                )
            )

        if holding_notices:
            # At least one cited field is under hold: the derived piece stays
            # visible alongside it in the controlled preservation view and is
            # removed from the ordinary view.
            status = RetentionStatus.PRESERVED
        elif saw_expired:
            # Every cited field has aged out (no hold covers any of them): the
            # derivation loses all support and is withdrawn.
            status = RetentionStatus.EXPIRED
        else:
            status = RetentionStatus.NORMAL

        return ArtifactDecision(
            artifact_id=artifact.artifact_id,
            status=status,
            referenced_fields=tuple(sorted(artifact.referenced_fields)),
            references=tuple(references),
            holding_notice_ids=tuple(sorted(holding_notices)),
        )


def decide_retention(
    materials: Iterable[Material],
    notices: Iterable[PreservationNotice],
    artifacts: Iterable[DerivedArtifact],
    material_id: str,
    as_of: date,
) -> RetentionDecision:
    """One-shot convenience wrapper around :class:`RetentionEngine`.

    Builds a registry from the supplied inventory and adjudicates one
    material immediately.
    """
    engine = RetentionEngine()
    for material in materials:
        engine.register_material(material)
    for notice in notices:
        engine.register_notice(notice)
    for artifact in artifacts:
        engine.register_artifact(artifact)
    return engine.decide(material_id, as_of)
