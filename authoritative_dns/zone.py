"""In-memory authoritative zone with implicit-node and wildcard semantics.

The zone keeps the set of *existing names* alongside the RRsets.  A name
exists when it owns an RRset (including a delegation NS RRset), or when it is
an empty non-terminal because some other name lies below it.  Both indexes are
updated together by :meth:`Zone.add_record` and :meth:`Zone.remove_record`, so
adding or deleting a record never leaves a stale empty node behind, and the
next query sees a consistent name tree.

Resolution order for a query ``qname``/``qtype``:

1. If ``qname`` is at or below a delegation point (a non-apex name owning NS
   records), return a **referral** carrying that NS RRset.  No answer is
   synthesised from zone data below the cut.
2. If ``qname`` exists exactly, return its RRset of ``qtype`` (**answer**) or
   **no-data** when the type is absent.
3. Otherwise find the nearest existing ancestor (closest to ``qname``).  Only
   that ancestor's direct wildcard child, ``*.<ancestor>``, may synthesise:
   it answers under ``qname`` when it owns ``qtype``, else no-data; if it does
   not exist the name does not exist (**NXDOMAIN**).  A wildcard higher up is
   never consulted, so it cannot shadow a nearer empty node.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from .names import (
    Name,
    OutOfZoneName,
    is_subdomain,
    parse_name,
    proper_ancestors,
    to_text,
)
from .records import Record
from .status import RECORD_TYPES, AnswerStatus

#: Status values, re-exported for callers that import them from here.
ANSWER = AnswerStatus.ANSWER
NODATA = AnswerStatus.NODATA
NXDOMAIN = AnswerStatus.NXDOMAIN
REFERRAL = AnswerStatus.REFERRAL

#: Kept as aliases so callers may compare against a result directly.
NO_ERROR_NODATA = NODATA
NAME_ERROR = NXDOMAIN


class ZoneError(ValueError):
    """Raised for invalid zone configuration or record mutations."""


@dataclass(frozen=True)
class Response:
    """Outcome of a single authoritative lookup.

    ``records`` holds the answer RRset for ``ANSWER``, the child NS RRset for
    ``REFERRAL`` and an empty tuple otherwise.  ``source`` names the node the
    answer came from (``None`` for NXDOMAIN) and ``wildcard`` is ``True`` when
    the records were synthesised from a wildcard node.
    """

    qname: str
    qtype: str
    status: str
    records: Tuple[Record, ...] = ()
    source: Optional[str] = None
    wildcard: bool = False

    @property
    def is_answer(self) -> bool:
        return self.status == AnswerStatus.ANSWER

    @property
    def is_referral(self) -> bool:
        return self.status == AnswerStatus.REFERRAL

    @property
    def is_nodata(self) -> bool:
        return self.status == AnswerStatus.NODATA

    @property
    def is_nxdomain(self) -> bool:
        return self.status == AnswerStatus.NXDOMAIN

    @property
    def values(self) -> Tuple:
        out = []
        for rr in self.records:
            out.append(list(rr.value) if rr.rtype == "TXT" else rr.value)
        return tuple(out)

    def to_dict(self) -> dict:
        return {
            "qname": self.qname,
            "qtype": self.qtype,
            "status": self.status,
            "source": self.source,
            "wildcard": self.wildcard,
            "records": [rr.to_dict() for rr in self.records],
        }


class Zone:
    """A single authoritative zone.

    Parameters
    ----------
    origin:
        Zone apex, e.g. ``"example."``.  Every record must live at or below it.
    default_ttl:
        TTL for records created through :meth:`add` when none is supplied.
    """

    def __init__(self, origin: str, *, default_ttl: int = 300) -> None:
        try:
            labels = parse_name(origin)
        except Exception as exc:  # InvalidDNSName / TypeError
            raise ZoneError(f"invalid zone origin {origin!r}: {exc}") from exc
        if not labels:
            raise ZoneError("root zone is not supported")
        if "*" in labels:
            raise ZoneError("a zone origin must not contain a wildcard label")
        self._origin: Name = labels
        self._default_ttl = default_ttl
        # name -> rtype -> list of Record (insertion order preserved)
        self._rrsets: Dict[Name, Dict[str, List[Record]]] = {}
        # every name that exists, including empty non-terminals
        self._existing: Set[Name] = {labels}

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def origin(self) -> Name:
        return self._origin

    @property
    def origin_text(self) -> str:
        return to_text(self._origin)

    @property
    def default_ttl(self) -> int:
        return self._default_ttl

    def __len__(self) -> int:
        return sum(len(by_type) for by_type in self._rrsets.values())

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        try:
            return parse_name(name) in self._existing
        except Exception:
            return False

    def existing_names(self) -> List[str]:
        """All existing names in canonical text form, sorted."""
        return sorted(to_text(name) for name in self._existing)

    def is_delegation(self, name) -> bool:
        """True when *name* (a proper subdomain of the apex) owns NS records."""
        labels = self._coerce(name)
        return (
            labels != self._origin
            and bool(self._rrsets.get(labels, {}).get("NS"))
        )

    def records(self) -> List[Record]:
        """Every stored record, in deterministic order."""
        out: List[Record] = []
        for name in sorted(self._rrsets):
            for rtype in sorted(self._rrsets[name]):
                out.extend(self._rrsets[name][rtype])
        return out

    def rrsets(self) -> Dict[Tuple[str, str], List[Record]]:
        """Return a copy of the RRset index keyed by (owner text, type)."""
        return {
            (to_text(name), rtype): list(records)
            for name, by_type in self._rrsets.items()
            for rtype, records in by_type.items()
        }

    def get(self, name: str, rtype: str) -> List[Record]:
        """Return the RRset at *name* of *rtype* (empty list when absent)."""
        labels = self._require_in_zone(name)
        return list(self._rrsets.get(labels, {}).get(_record_type(rtype), []))

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    def add(self, name: str, rtype: str, value, ttl: Optional[int] = None) -> Record:
        """Create a :class:`Record` and add it to the zone atomically."""
        record = Record(
            name=name,
            rtype=rtype,
            value=value,
            ttl=self._default_ttl if ttl is None else ttl,
        )
        self.add_record(record)
        return record

    def add_record(self, record: Record) -> None:
        if not isinstance(record, Record):
            raise ZoneError("add_record expects a Record instance")
        labels = self._require_in_zone(record.name)

        # The cut splits authority: nothing may be added under a delegation
        # point, and a delegation point may only carry its own NS RRset.
        for ancestor in proper_ancestors(labels):
            if not is_subdomain(ancestor, self._origin):
                continue
            if self.is_delegation(ancestor):
                raise ZoneError(
                    f"{record.name} is below delegation point "
                    f"{to_text(ancestor)}; child-zone data cannot be added"
                )
        if self.is_delegation(labels) and record.rtype != "NS":
            raise ZoneError(
                f"{to_text(labels)} is a delegation point; only NS records "
                "may be added there"
            )
        existing_types = set(self._rrsets.get(labels, {}))
        if record.rtype == "NS" and labels != self._origin:
            # Creating a cut above existing zone data would orphan that data.
            if any(
                other != labels and is_subdomain(other, labels)
                for other in self._existing
            ):
                raise ZoneError(
                    f"cannot delegate {to_text(labels)}: child-zone data "
                    "already exists below it"
                )
            if existing_types - {"NS"}:
                raise ZoneError(
                    f"cannot delegate {to_text(labels)}: it already owns "
                    f"{', '.join(sorted(existing_types - {'NS'}))} data"
                )
        elif "NS" in existing_types and labels != self._origin:
            raise ZoneError(
                f"{to_text(labels)} is a delegation point; only NS records "
                "may be added there"
            )

        by_type = self._rrsets.get(labels)
        rrset = by_type.get(record.rtype) if by_type else None
        if rrset:
            if any(rr.value == record.value for rr in rrset):
                raise ZoneError(
                    f"duplicate record {record.name} {record.rtype} "
                    f"{record.value!r}"
                )
            if rrset[0].ttl != record.ttl:
                raise ZoneError(
                    f"TTL mismatch in RRset {record.name} {record.rtype}: "
                    f"{rrset[0].ttl} != {record.ttl}"
                )

        by_type = self._rrsets.setdefault(labels, {})
        by_type.setdefault(record.rtype, []).append(record)

        self._existing.add(labels)
        for ancestor in proper_ancestors(labels):
            if is_subdomain(ancestor, self._origin):
                self._existing.add(ancestor)

    def remove(self, name: str, rtype: str, value=None) -> None:
        """Remove one record (selected by value) or the whole RRset.

        ``value`` may be omitted to drop every record of that type.  TXT
        values accept the same forms as :class:`Record` (a string or a
        sequence of strings) and matching is by canonical value.
        """
        labels = self._require_in_zone(name)
        rtype = _record_type(rtype)
        by_type = self._rrsets.get(labels)
        if not by_type or rtype not in by_type:
            raise ZoneError(f"no {rtype} records at {to_text(labels)}")

        if value is None:
            del by_type[rtype]
        else:
            probe = Record(to_text(labels), rtype, value, ttl=0)
            matches = [rr for rr in by_type[rtype] if rr.value == probe.value]
            if not matches:
                raise ZoneError(
                    f"no matching {rtype} record at {to_text(labels)}"
                )
            for rr in matches:
                by_type[rtype].remove(rr)

        self._prune(labels)

    def remove_record(self, record: Record) -> None:
        if not isinstance(record, Record):
            raise ZoneError("remove_record expects a Record instance")
        self.remove(record.name, record.rtype, record.value)

    def _prune(self, labels: Name) -> None:
        """Drop now-empty nodes from both indexes after a deletion."""
        if not self._rrsets.get(labels):
            self._rrsets.pop(labels, None)
            if labels != self._origin:
                self._existing.discard(labels)
        for ancestor in proper_ancestors(labels):
            if ancestor == self._origin:
                continue
            if not is_subdomain(ancestor, self._origin):
                continue
            if ancestor in self._rrsets:
                continue
            if any(
                other != ancestor and is_subdomain(other, ancestor)
                for other in self._existing
            ):
                continue
            self._existing.discard(ancestor)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------
    def lookup(self, qname: str, qtype: str) -> Response:
        """Resolve ``qname``/``qtype`` against the current zone state."""
        labels = self._require_in_zone(qname)
        rtype = _query_type(qtype)
        text = to_text(labels)

        delegation = self._closest_cut(labels)
        if delegation is not None:
            return Response(
                qname=text,
                qtype=rtype,
                status=AnswerStatus.REFERRAL,
                records=tuple(self._rrsets[delegation]["NS"]),
                source=to_text(delegation),
            )

        source = self._synthesis_source(labels, rtype)
        if source is None:
            return Response(qname=text, qtype=rtype, status=AnswerStatus.NXDOMAIN)

        owner, records = source
        if owner == labels:
            if not records:
                return Response(
                    qname=text,
                    qtype=rtype,
                    status=AnswerStatus.NODATA,
                    source=text,
                )
            return Response(
                qname=text,
                qtype=rtype,
                status=AnswerStatus.ANSWER,
                records=tuple(records),
                source=text,
            )

        # Synthesised from a wildcard: answer under the queried name.
        if not records:
            return Response(
                qname=text,
                qtype=rtype,
                status=AnswerStatus.NODATA,
                source=to_text(owner),
            )
        synthesised = tuple(
            Record(text, rr.rtype, rr.value, rr.ttl) for rr in records
        )
        return Response(
            qname=text,
            qtype=rtype,
            status=AnswerStatus.ANSWER,
            records=synthesised,
            source=to_text(owner),
            wildcard=True,
        )

    def _coerce(self, name) -> Name:
        try:
            labels = parse_name(name) if isinstance(name, str) else name
        except Exception as exc:
            raise ZoneError(f"invalid name {name!r}: {exc}") from exc
        if not isinstance(labels, tuple):
            raise ZoneError(f"invalid name {name!r}")
        return labels

    def _require_in_zone(self, name: str) -> Name:
        try:
            labels = parse_name(name)
        except Exception as exc:
            raise ZoneError(f"invalid name {name!r}: {exc}") from exc
        if not is_subdomain(labels, self._origin):
            raise OutOfZoneName(
                f"{to_text(labels)} is outside zone {self.origin_text}"
            )
        return labels

    def _closest_cut(self, labels: Name) -> Optional[Name]:
        """Return the delegation point covering *labels*, if any.

        A name that *is* a delegation point is itself covered by the cut, so
        queries at and below the delegation point both yield referrals.
        """
        for ancestor in (labels,) + tuple(proper_ancestors(labels)):
            if not is_subdomain(ancestor, self._origin):
                continue
            if self.is_delegation(ancestor):
                return ancestor
        return None

    def _synthesis_source(
        self, labels: Name, qtype: str
    ) -> Optional[Tuple[Name, List[Record]]]:
        """Pick the single node allowed to answer this query.

        Returns ``(owner, records)`` where ``owner`` is either ``labels`` or
        the direct wildcard child of the nearest existing ancestor, or
        ``None`` when the queried name does not exist.  ``records`` is empty
        when the selected node exists but owns no ``qtype`` data.
        """
        if labels in self._existing:
            if labels in self._rrsets:
                return labels, list(self._rrsets[labels].get(qtype, []))
            # Empty non-terminal: the name exists but owns no data at all.
            return labels, []

        nearest: Optional[Name] = None
        for ancestor in proper_ancestors(labels):
            if not is_subdomain(ancestor, self._origin):
                continue
            if ancestor in self._existing:
                nearest = ancestor
                break
        if nearest is None:
            return None

        wildcard = ("*",) + nearest
        if wildcard not in self._existing:
            return None
        return wildcard, list(self._rrsets.get(wildcard, {}).get(qtype, []))


def _query_type(qtype) -> str:
    """Validate a query type mnemonic.

    Any syntactically valid type is accepted; types without stored data simply
    produce a no-data response.  This mirrors DNS, where asking for an absent
    type at an existing name is a NOERROR/NODATA answer, not an error.
    """
    if not isinstance(qtype, str):
        raise TypeError(f"query type must be str, got {type(qtype).__name__}")
    text = qtype.strip().upper()
    if not text or not text.isalnum():
        raise ZoneError(f"invalid query type {qtype!r}")
    return text


def _record_type(rtype) -> str:
    """Validate a record type mnemonic for mutation helpers."""
    text = _query_type(rtype)
    if text not in RECORD_TYPES:
        raise ZoneError(
            f"unsupported record type {text!r}; "
            f"supported: {', '.join(sorted(RECORD_TYPES))}"
        )
    return text
