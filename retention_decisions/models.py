"""Domain model for the retention / legal-hold adjudication module.

A :class:`Material` describes an archived investigation record: its field
list and its ordinary retention expiry date.  A :class:`PreservationNotice`
is a legal-hold style instruction that, from its own effective date, keeps a
subset of fields alive in the controlled preservation view.  A
:class:`DerivedArtifact` is a derived piece of material that references
(quotes/cites) some of the original record's fields.

Dates throughout the module are plain :class:`datetime.date` values; the
module never performs wall-clock lookups, so historical replay is fully
deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import date
from enum import Enum
from typing import FrozenSet, Optional, Sequence, Tuple


class RetentionStatus(str, Enum):
    """Lifecycle state of one field or derived artifact on a given day."""

    #: Still inside the ordinary retention period; visible in the normal view.
    NORMAL = "normal"
    #: Held by one or more effective preservation notices; visible only in the
    #: controlled preservation view.
    PRESERVED = "preserved"
    #: Ordinary period has elapsed and no preservation notice applies; removed
    #: from both views.
    EXPIRED = "expired"


@dataclass(frozen=True)
class Material:
    """An archived investigation record.

    :param material_id: stable identifier of the record.
    :param fields: names of the raw fields kept on the record.
    :param normal_expiry: end of the ordinary retention period.  On and after
        this date a field has aged out of the normal investigation view,
        unless a preservation notice keeps it alive.
    """

    material_id: str
    fields: FrozenSet[str]
    normal_expiry: date

    def __post_init__(self) -> None:
        if not self.material_id:
            raise ValueError("material_id must be a non-empty string")
        raw = tuple(self.fields)
        if not raw:
            raise ValueError("material must declare at least one field")
        if any(not name for name in raw):
            raise ValueError("field names must be non-empty strings")
        if len(raw) != len(set(raw)):
            raise ValueError("field names must be unique")
        object.__setattr__(self, "fields", frozenset(raw))
    @classmethod
    def create(
        cls, material_id: str, fields: Sequence[str], normal_expiry: date
    ) -> "Material":
        """Convenience constructor accepting any sequence of field names."""
        return cls(material_id=material_id, fields=frozenset(fields),
                   normal_expiry=normal_expiry)


@dataclass(frozen=True)
class PreservationNotice:
    """A preservation (legal hold) instruction scoped to selected fields.

    A notice becomes effective at ``effective_date`` and, unless terminated,
    remains effective forever.  A notice carrying an ``expires_on`` date only
    covers the half-open interval ``[effective_date, expires_on)``: on
    ``expires_on`` it is no longer effective.  A later-arriving correction is
    modelled simply as another notice whose own effective date decides when it
    starts to influence both live and replayed decisions.

    :param notice_id: stable identifier of the notice/correction.
    :param material_id: record whose fields are placed under hold.
    :param fields: the columns covered by the hold; every other column keeps
        ageing on the original schedule.
    :param effective_date: first day on which the hold is effective.
    :param expires_on: optional first day on which the hold has ceased to be
        effective.
    :param amends_notice_id: when set, this notice is a correction that
        supersedes the cited notice from its own effective date onward.  On
        days the correction is effective, the amended notice is treated as no
        longer in force (the correction's field list replaces the original
        scope).  On earlier days -- including historical replays of those days
        -- the original notice still applies.
    """

    notice_id: str
    material_id: str
    fields: FrozenSet[str]
    effective_date: date
    expires_on: Optional[date] = None
    summary: str = ""
    amends_notice_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.notice_id:
            raise ValueError("notice_id must be a non-empty string")
        if not self.material_id:
            raise ValueError("material_id must be a non-empty string")
        raw = tuple(self.fields)
        if not raw:
            raise ValueError(
                "a preservation notice must cover at least one field; "
                "use no notice instead of an empty hold"
            )
        if any(not name for name in raw):
            raise ValueError("field names must be non-empty strings")
        if len(raw) != len(set(raw)):
            raise ValueError("field names must be unique")
        if self.expires_on is not None and self.expires_on <= self.effective_date:
            raise ValueError("expires_on must be later than effective_date")
        if self.amends_notice_id == self.notice_id:
            raise ValueError("a notice cannot amend itself")
        object.__setattr__(self, "fields", frozenset(raw))

    @classmethod
    def create(
        cls,
        notice_id: str,
        material_id: str,
        fields: Sequence[str],
        effective_date: date,
        expires_on: Optional[date] = None,
        summary: str = "",
        amends_notice_id: Optional[str] = None,
    ) -> "PreservationNotice":
        """Convenience constructor accepting any sequence of field names."""
        return cls(
            notice_id=notice_id,
            material_id=material_id,
            fields=frozenset(fields),
            effective_date=effective_date,
            expires_on=expires_on,
            summary=summary,
            amends_notice_id=amends_notice_id,
        )

    def is_effective_on(self, day: date) -> bool:
        """Return whether the notice's own effective interval contains ``day``.

        This ignores amendment relationships: the engine decides which
        notices are superseded by effective corrections.
        """
        if day < self.effective_date:
            return False
        if self.expires_on is not None and day >= self.expires_on:
            return False
        return True


@dataclass(frozen=True)
class DerivedArtifact:
    """A derived piece referencing fields of an archived record.

    :param artifact_id: stable identifier of the derived piece.
    :param material_id: record the derivation is based on.
    :param referenced_fields: raw fields the derivation cites/uses.
    """

    artifact_id: str
    material_id: str
    referenced_fields: FrozenSet[str]

    def __post_init__(self) -> None:
        if not self.artifact_id:
            raise ValueError("artifact_id must be a non-empty string")
        if not self.material_id:
            raise ValueError("material_id must be a non-empty string")
        raw = tuple(self.referenced_fields)
        if any(not name for name in raw):
            raise ValueError("field names must be non-empty strings")
        if len(raw) != len(set(raw)):
            raise ValueError("referenced field names must be unique")
        object.__setattr__(self, "referenced_fields", frozenset(raw))

    @classmethod
    def create(
        cls, artifact_id: str, material_id: str, referenced_fields: Sequence[str]
    ) -> "DerivedArtifact":
        """Convenience constructor accepting any sequence of field names."""
        return cls(
            artifact_id=artifact_id,
            material_id=material_id,
            referenced_fields=frozenset(referenced_fields),
        )


@dataclass(frozen=True)
class FieldDecision:
    """Adjudication of one raw field on the requested day."""

    field_name: str
    status: RetentionStatus
    #: Notices that were effective for this field on the day (sorted).
    active_notice_ids: Tuple[str, ...]
    #: Ordinary retention expiry of the owning record.
    normal_expiry: date

    @property
    def in_controlled_view(self) -> bool:
        """Whether the field must stay in the controlled preservation view."""
        return self.status is RetentionStatus.PRESERVED

    @property
    def withdrawn_from_normal_view(self) -> bool:
        """Whether the field must be taken out of the normal investigation view."""
        return self.status is not RetentionStatus.NORMAL


@dataclass(frozen=True)
class ReferenceLink:
    """One edge of the field -> derived-artifact reference graph."""

    artifact_id: str
    material_id: str
    field_name: str
    field_status: RetentionStatus


@dataclass(frozen=True)
class ArtifactDecision:
    """Adjudication of one derived artifact on the requested day."""

    artifact_id: str
    status: RetentionStatus
    referenced_fields: Tuple[str, ...]
    #: Per-referenced-field status, including references to fields of other
    #: records registered with the engine.
    references: Tuple[ReferenceLink, ...]
    #: Notices (on any referenced field) that force this artifact to be held.
    holding_notice_ids: Tuple[str, ...]


@dataclass(frozen=True)
class RetentionDecision:
    """Full adjudication result for one material on one day."""

    material_id: str
    as_of: date
    normal_expiry: date
    field_decisions: Tuple[FieldDecision, ...]
    artifact_decisions: Tuple[ArtifactDecision, ...]
    #: Fields that must remain accessible in the controlled preservation view.
    controlled_view_fields: Tuple[str, ...]
    #: Fields that must be withdrawn from the ordinary investigation view.
    withdrawn_fields: Tuple[str, ...]
    #: Fields still visible in the ordinary investigation view.
    normal_view_fields: Tuple[str, ...]
    #: Artifacts that must remain accessible in the controlled preservation view.
    controlled_view_artifacts: Tuple[str, ...]
    #: Artifacts that must be withdrawn from the ordinary investigation view.
    withdrawn_artifacts: Tuple[str, ...]
    #: Artifacts still visible in the ordinary investigation view.
    normal_view_artifacts: Tuple[str, ...]
    #: Every field -> artifact reference edge, including cross-material links.
    reference_links: Tuple[ReferenceLink, ...]

    def field_status(self, field_name: str) -> RetentionStatus:
        """Look up the status of one field (KeyError if it does not exist)."""
        for decision in self.field_decisions:
            if decision.field_name == field_name:
                return decision.status
        raise KeyError(field_name)

    def artifact_status(self, artifact_id: str) -> RetentionStatus:
        """Look up the status of one derived artifact."""
        for decision in self.artifact_decisions:
            if decision.artifact_id == artifact_id:
                return decision.status
        raise KeyError(artifact_id)
