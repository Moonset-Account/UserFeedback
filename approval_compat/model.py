"""Data model for pre-release approval-semantics compatibility checks.

Every value object is a frozen dataclass, so a route can be hand written in a
test or loaded from JSON without a schema library.

Terminology
-----------
*field number*
    Stable protobuf-style number used on the wire.  Numbers, not names, are what
    survive a hop that has no idea the field exists.
*encoding*
    ``normal``, ``packed``, ``oneof`` or ``repeated``.  Only ``normal`` is a
    sound carrier for approval evidence: with the others a legacy writer picks
    the branch or element and thereby picks the meaning.
*business meaning*
    Declared explicitly by the caller.  Meanings in :data:`APPROVAL_MEANINGS`
    are approval-bearing; everything else is ordinary.  The checker never
    guesses from a field name.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Mapping, Optional, Tuple

ENCODINGS = ("normal", "packed", "oneof", "repeated")

#: Encoding mechanism strength.  Approval carried by anything but ``normal`` is
#: a finding: a legacy sender can choose the branch or element.
MECHANISM_STRENGTH = {
    "normal": 3,
    "packed": 2,
    "oneof": 1,
    "repeated": 0,
}

#: Business meanings that make a field approval-bearing.
APPROVAL_MEANINGS = frozenset(
    {
        "approval",
        "approval_decision",
        "approval_scope",
        "approver_identity",
    }
)

#: Recognised ordinary meanings (documentation only; anything else is ordinary).
ORDINARY_MEANINGS = frozenset(
    {
        "ordinary",
        "display_name",
        "request_id",
        "timestamp",
        "summary",
        "attachment_ref",
    }
)


class SchemaError(ValueError):
    """Raised when a route fixture or version description is inconsistent."""


def _normalise_meaning(meaning: str) -> str:
    if not isinstance(meaning, str) or not meaning.strip():
        raise SchemaError("business meaning must be a non-empty string")
    return meaning.strip().lower()


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


@dataclass(frozen=True)
class FieldDef:
    """One field as one message version understands it."""

    number: int
    encoding: str
    meaning: str

    def __post_init__(self) -> None:
        if not _is_positive_int(self.number):
            raise SchemaError(f"field number must be a positive int: {self.number!r}")
        if self.encoding not in ENCODINGS:
            raise SchemaError(
                f"unknown encoding {self.encoding!r} for field {self.number}"
            )
        object.__setattr__(self, "meaning", _normalise_meaning(self.meaning))

    @property
    def is_approval(self) -> bool:
        return self.meaning in APPROVAL_MEANINGS

    @property
    def is_ordinary(self) -> bool:
        return not self.is_approval

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "encoding": self.encoding,
            "meaning": self.meaning,
        }


@dataclass(frozen=True)
class MessageVersion:
    """One published (or proposed) wire version of the internal message."""

    version: str
    fields: Tuple[FieldDef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise SchemaError("version tag must be a non-empty string")
        numbers = [f.number for f in self.fields]
        if len(numbers) != len(set(numbers)):
            raise SchemaError(f"duplicate field numbers in version {self.version}")

    @classmethod
    def from_raw(
        cls, version: str, fields: Mapping[int, Tuple[str, str]]
    ) -> "MessageVersion":
        """Build from ``{number: (encoding, meaning)}`` (JSON friendly)."""
        return cls(
            version=version,
            fields=tuple(
                FieldDef(number=int(n), encoding=enc, meaning=meaning)
                for n, (enc, meaning) in sorted(fields.items())
            ),
        )

    def field(self, number: int) -> Optional[FieldDef]:
        for f in self.fields:
            if f.number == number:
                return f
        return None

    @property
    def numbers(self) -> Tuple[int, ...]:
        return tuple(sorted(f.number for f in self.fields))

    @property
    def approval_numbers(self) -> Tuple[int, ...]:
        return tuple(f.number for f in self.fields if f.is_approval)

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "fields": [f.as_dict() for f in self.fields],
        }


@dataclass(frozen=True)
class HopTransform:
    """How one fixed forwarding hop treats each field number it sees.

    Every hop states three buckets, matching the operational question "what is
    regenerated, what is copied byte-for-byte, what is removed":

    ``regenerate``
        Numbers the hop writes afresh.  ``rebuilt_from`` says where the new
        value comes from; only ``"trusted"`` / ``"trusted_input"`` confers
        approval authority, ``"untrusted"`` (or an omitted entry) does not.
        Regeneration *severs* the legacy value only if the hop can actually
        decode the field, i.e. the number is in ``known_numbers``.  A hop that
        cannot decode field N can only append a second field N next to the
        opaque legacy bytes -- the checker reports that as a duplicate carrier.

    ``passthrough``
        Numbers the hop knows and copies byte-for-byte.  Same bytes, same
        trust level; the hop is a witness, not an authority.

    ``drop``
        Numbers the hop removes outright.  Removing is what terminates a
        legacy value's journey.

    Every number in none of the buckets and absent from ``known_numbers`` is an
    *unknown field* to the hop:

    * ``unknown_bytes="preserved"`` (default) forwards it untouched -- the
      classic relay that has never heard of the field still carries it;
    * ``unknown_bytes="dropped"`` severs it.

    ``known_numbers=None`` means the hop's schema declares nothing at all (an
    opaque relay); set it explicitly for a relay that parses a schema.
    """

    hop: str
    regenerate: frozenset = frozenset()
    passthrough: frozenset = frozenset()
    drop: frozenset = frozenset()
    rebuilt_from: Mapping[int, str] = dc_field(default_factory=dict)
    known_numbers: Optional[frozenset] = None
    unknown_bytes: str = "preserved"
    to_version: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.hop, str) or not self.hop:
            raise SchemaError("hop label must be a non-empty string")
        for attr in ("regenerate", "passthrough", "drop"):
            value = getattr(self, attr)
            if not isinstance(value, frozenset):
                value = frozenset(value)
                object.__setattr__(self, attr, value)
            if any(not _is_positive_int(n) for n in value):
                raise SchemaError(f"{attr} must contain positive field numbers")
        buckets = {
            "regenerate": self.regenerate,
            "passthrough": self.passthrough,
            "drop": self.drop,
        }
        names = list(buckets)
        for i, left in enumerate(names):
            for right in names[i + 1 :]:
                overlap = buckets[left] & buckets[right]
                if overlap:
                    raise SchemaError(
                        f"hop {self.hop}: fields {sorted(overlap)} are both "
                        f"{left} and {right}"
                    )
        if self.unknown_bytes not in ("preserved", "dropped"):
            raise SchemaError(
                f"hop {self.hop}: unknown_bytes must be 'preserved' or 'dropped'"
            )
        if self.known_numbers is not None:
            if not isinstance(self.known_numbers, frozenset):
                object.__setattr__(
                    self, "known_numbers", frozenset(self.known_numbers)
                )
            if any(not _is_positive_int(n) for n in self.known_numbers):
                raise SchemaError(
                    f"hop {self.hop}: known_numbers must contain positive ints"
                )
            contradictory = self.known_numbers & self.drop
            if contradictory:
                raise SchemaError(
                    f"hop {self.hop}: fields {sorted(contradictory)} are declared "
                    f"known and dropped at the same time"
                )
        if not isinstance(self.rebuilt_from, Mapping):
            raise SchemaError(f"hop {self.hop}: rebuilt_from must be a mapping")
        for number, source in dict(self.rebuilt_from).items():
            if not _is_positive_int(number):
                raise SchemaError(
                    f"hop {self.hop}: rebuilt_from keys must be field numbers"
                )
            if number not in self.regenerate:
                raise SchemaError(
                    f"hop {self.hop}: rebuilt_from mentions field {number} which "
                    f"is not regenerated"
                )
            if source not in ("trusted", "trusted_input", "untrusted"):
                raise SchemaError(
                    f"hop {self.hop}: rebuilt_from[{number}] must be "
                    f"'trusted'/'trusted_input' or 'untrusted', got {source!r}"
                )
        object.__setattr__(self, "rebuilt_from", dict(self.rebuilt_from))

    def is_trusted_rebuild(self, number: int) -> bool:
        return self.rebuilt_from.get(number) in ("trusted", "trusted_input")

    def treats_as_unknown(self, number: int) -> bool:
        """Does this hop carry ``number`` without understanding it?"""
        if number in self.regenerate or number in self.passthrough:
            return False
        if number in self.drop:
            return False
        if self.known_numbers is None:
            return True
        return number not in self.known_numbers

    def can_decode(self, number: int) -> bool:
        """Can the hop replace field ``number`` rather than duplicate it?"""
        return self.known_numbers is not None and number in self.known_numbers


@dataclass(frozen=True)
class Route:
    """A fixed forwarding path: ordered hops plus every version on it.

    ``timeline`` is the publication order of the wire versions this path may
    carry, from the oldest client to the newest reader.  It lets the checker
    name the version that first gave a number an approval meaning even when no
    hop rewrote a byte.  When omitted, the timeline is ``(origin,
    destination)``.

    ``senders`` are the client versions that actually feed this fixed path.
    Default ``(origin,)``.  A number is legacy-writable when any sender declares
    it as an *ordinary* field; a sender that declares it as an approval field is
    an authorized approval writer, not a legacy one.

    ``legacy_unknown_field_bytes`` models the stricter assumption that a legacy
    encoder may also emit numbers its own schema never declared.  When true,
    such bytes are treated as sender-controlled and must be severed like any
    ordinary field.  The default (false) matches the ordinary reading of "the
    fields the old version can write".
    """

    name: str
    origin: MessageVersion
    destination: MessageVersion
    hops: Tuple[HopTransform, ...]
    timeline: Tuple[MessageVersion, ...] = ()
    senders: Tuple[MessageVersion, ...] = ()
    legacy_unknown_field_bytes: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise SchemaError("route name must be a non-empty string")
        if not self.hops:
            raise SchemaError(f"route {self.name}: at least one hop is required")
        if not isinstance(self.legacy_unknown_field_bytes, bool):
            raise SchemaError(
                f"route {self.name}: legacy_unknown_field_bytes must be a bool"
            )
        seen = set()
        for hop in self.hops:
            if hop.hop in seen:
                raise SchemaError(f"route {self.name}: duplicate hop {hop.hop!r}")
            seen.add(hop.hop)
        if self.timeline:
            tags = [v.version for v in self.timeline]
            if len(tags) != len(set(tags)):
                raise SchemaError(f"route {self.name}: duplicate timeline versions")
            if tags[0] != self.origin.version:
                raise SchemaError(
                    f"route {self.name}: timeline starts at {tags[0]!r}, "
                    f"origin is {self.origin.version!r}"
                )
            if tags[-1] != self.destination.version:
                raise SchemaError(
                    f"route {self.name}: timeline ends at {tags[-1]!r}, "
                    f"destination is {self.destination.version!r}"
                )
            for hop in self.hops:
                if hop.to_version and hop.to_version not in tags:
                    raise SchemaError(
                        f"route {self.name}: hop {hop.hop!r} speaks unknown "
                        f"version {hop.to_version!r}"
                    )
        for sender in self.senders:
            if sender.version not in [v.version for v in self.versions]:
                raise SchemaError(
                    f"route {self.name}: sender {sender.version!r} is not on the "
                    f"timeline"
                )

    @property
    def versions(self) -> Tuple[MessageVersion, ...]:
        return self.timeline or (self.origin, self.destination)

    @property
    def effective_senders(self) -> Tuple[MessageVersion, ...]:
        return self.senders or (self.origin,)


def _build_version(spec: Mapping) -> MessageVersion:
    return MessageVersion.from_raw(
        spec["version"],
        {int(n): (v["encoding"], v["meaning"]) for n, v in spec["fields"].items()},
    )


def route_from_dict(raw: Mapping) -> Route:
    """Load a route from the JSON shape documented in the README."""
    try:
        origin = _build_version(raw["origin"])
        destination = _build_version(raw["destination"])
        hops = tuple(
            HopTransform(
                hop=h["hop"],
                regenerate=h.get("regenerate", ()),
                drop=h.get("drop", ()),
                rebuilt_from={
                    int(n): source
                    for n, source in h.get("rebuilt_from", {}).items()
                },
                known_numbers=(
                    frozenset(h["known_numbers"])
                    if "known_numbers" in h
                    else None
                ),
                unknown_bytes=h.get("unknown_bytes", "preserved"),
                to_version=h.get("to_version", ""),
            )
            for h in raw["hops"]
        )
        timeline = tuple(_build_version(v) for v in raw.get("timeline", ()))
        senders = tuple(_build_version(v) for v in raw.get("senders", ()))
        name = raw["name"]
    except KeyError as exc:
        raise SchemaError(f"route {raw.get('name')!r} is missing {exc}") from exc
    return Route(
        name=name,
        origin=origin,
        destination=destination,
        hops=hops,
        timeline=timeline,
        senders=senders,
        legacy_unknown_field_bytes=raw.get("legacy_unknown_field_bytes", False),
    )
